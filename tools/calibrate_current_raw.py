"""Raw, provenance-preserving current-sensor calibration for the BLDC bench."""

from __future__ import annotations

import argparse
import array
import base64
import csv
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import threading
import time
import traceback
import uuid
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bldc_phm.schema import STREAM_COLUMNS  # noqa: E402


FORMAT_VERSION = 1
BAUD = 115200
from bldc_phm.instruments import DMM_RESOURCE as DEFAULT_DMM  # noqa: E402
from bldc_phm.instruments import SCOPE_HOST as DEFAULT_SCOPE_HOST  # noqa: E402
DEFAULT_SCOPE_PORT = 5025
DEFAULT_SETTLE_S = 10.0
DEFAULT_DURATION_S = 8.0
EVIDENCE_ROOT = ROOT / "data" / "verification" / "current_raw_calibration"
ACTIVE_POINTER = EVIDENCE_ROOT / "ACTIVE_SESSION.txt"

RAW_REQUIRED = (
    "t_ms",
    "rpm",
    "i_adc_raw_sum",
    "i_adc_raw_count",
    "i_adc_raw_min",
    "i_adc_raw_max",
    "vref_adc_raw_sum",
    "vref_adc_raw_count",
    "vref_factory_cal_raw",
    "frame_seq",
    "pwm_ticks",
    "i_adc_window_start_us",
    "i_adc_window_end_us",
    "i_adc_fail_count",
    "vref_window_start_us",
    "vref_window_end_us",
    "i_adc_raw_sumsq",
    "vref_adc_raw_sumsq",
)
IDX = {name: i for i, name in enumerate(STREAM_COLUMNS)}

# These are declared component specifications, not empirical calibration data.
# A session records the values explicitly and permits overrides at creation.
DEFAULT_SHUNT_OHM = 0.005
DEFAULT_GAIN_V_V = 50.0
FACTORY_CAL_VDDA_MV = 3300.0
ADC_FULL_SCALE = 4095.0


def utc_now_ns() -> int:
    return time.time_ns()


def utc_iso_from_ns(value: int) -> str:
    return (
        dt.datetime.fromtimestamp(value / 1e9, tz=dt.timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def finite_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def fsync_file(handle: Any) -> None:
    handle.flush()
    os.fsync(handle.fileno())


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append one durable, immutable event."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json_compact(record) + "\n")
        fsync_file(handle)


def append_csv_row(path: Path, fieldnames: list[str], row: dict[str, Any]) -> None:
    """Append one CSV row, creating its header once."""
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerow(row)
        fsync_file(handle)


def write_unique_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
        fsync_file(handle)


def write_unique_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(value)
        fsync_file(handle)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git_revision() -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=3,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=3,
                check=True,
            ).stdout.strip()
        )
        return {"commit": commit, "worktree_dirty": dirty}
    except Exception as exc:
        return {"commit": None, "worktree_dirty": None, "error": str(exc)}


def require_raw_schema() -> None:
    missing = [name for name in RAW_REQUIRED if name not in IDX]
    if missing:
        raise RuntimeError(
            "firmware schema lacks required untouched ADC fields: " + ", ".join(missing)
        )


def parse_target(value: str) -> int:
    if value.strip().lower() == "off":
        return 0
    try:
        rpm = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("target must be 'off' or an integer RPM") from exc
    if rpm < 0:
        raise argparse.ArgumentTypeError("RPM cannot be negative")
    return rpm


def session_manifest(session: Path) -> dict[str, Any]:
    path = session / "session.json"
    if not path.is_file():
        raise FileNotFoundError(f"not a raw-calibration session: {session}")
    with path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("format_version") != FORMAT_VERSION:
        raise RuntimeError(
            f"unsupported session format {manifest.get('format_version')!r}; "
            f"tool supports {FORMAT_VERSION}"
        )
    locked = manifest.get("stream_columns")
    if locked != list(STREAM_COLUMNS):
        raise RuntimeError(
            "STREAM_COLUMNS changed after this session was created. Start a new "
            "session so CSV columns cannot be misinterpreted."
        )
    return manifest


def resolve_session(value: str | None) -> Path:
    if value:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = (ROOT / candidate).resolve()
        return candidate
    try:
        text = ACTIVE_POINTER.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise RuntimeError("no active session; run the 'new' command first") from exc
    if not text:
        raise RuntimeError("active-session pointer is empty; use --session explicitly")
    return Path(text).resolve()


def set_active_session(session: Path) -> None:
    """Update only the convenience pointer; evidence inside sessions stays immutable."""
    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = EVIDENCE_ROOT / f".active-{uuid.uuid4().hex}.tmp"
    temporary.write_text(str(session.resolve()) + "\n", encoding="utf-8")
    os.replace(temporary, ACTIVE_POINTER)


def load_events(session: Path) -> list[dict[str, Any]]:
    path = session / "events.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"corrupt event log at line {line_number}: {exc}") from exc
            value["_event_line"] = line_number
            events.append(value)
    return events


def event_base(session_id: str, event_type: str) -> dict[str, Any]:
    wall = utc_now_ns()
    return {
        "format_version": FORMAT_VERSION,
        "session_id": session_id,
        "event_type": event_type,
        "event_id": f"evt_{wall}_{uuid.uuid4().hex[:10]}",
        "host_time_ns": wall,
        "host_utc": utc_iso_from_ns(wall),
        "monotonic_ns": time.monotonic_ns(),
        "real_data_only": True,
        "simulated": False,
        "synthetic": False,
    }


def find_stlink_port(explicit: str | None) -> tuple[str, list[dict[str, Any]]]:
    if explicit:
        return explicit, [{"device": explicit, "selected": True, "selection": "explicit"}]
    try:
        import serial.tools.list_ports
    except ImportError as exc:
        raise RuntimeError("pyserial is required for hardware capture") from exc
    observed = []
    selected = None
    for port in serial.tools.list_ports.comports():
        description = port.description or ""
        hwid = port.hwid or ""
        is_stlink = (
            "STLINK" in description.upper()
            or "ST-LINK" in description.upper()
            or "0483" in hwid.upper()
        )
        observed.append(
            {
                "device": port.device,
                "description": description,
                "hwid": hwid,
                "vid": port.vid,
                "pid": port.pid,
                "serial_number": port.serial_number,
                "is_stlink": is_stlink,
                "selected": False,
            }
        )
        if is_stlink and selected is None:
            selected = port.device
    if selected is None:
        raise RuntimeError("no ST-LINK virtual COM port detected")
    for item in observed:
        if item["device"] == selected:
            item["selected"] = True
            item["selection"] = "auto_detected"
    return selected, observed


class Phase:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = "opening"

    def set(self, value: str) -> None:
        with self._lock:
            self._value = value

    def get(self) -> str:
        with self._lock:
            return self._value


SERIAL_FRAME_FIELDS = [
    "session_id",
    "capture_id",
    "host_receive_time_ns",
    "host_receive_utc",
    "host_receive_monotonic_ns",
    "phase",
    "serial_port",
    "schema_sha256",
    "raw_line_sha256",
    "raw_line",
] + list(STREAM_COLUMNS) + [
    "pa0_raw_mean_code",
    "pa0_raw_sd_code",
    "vref_raw_mean_code",
    "vref_raw_sd_code",
    "pa0_vref_ratio",
    "pa0_factory_uv",
]


class SerialRecorder(threading.Thread):
    """Own the serial reads and preserve every newline-delimited record."""

    def __init__(
        self,
        serial_handle: Any,
        serial_port: str,
        session: Path,
        session_id: str,
        capture_id: str,
        phase: Phase,
        stop_event: threading.Event,
        schema_sha256: str,
    ) -> None:
        super().__init__(name=f"serial-{capture_id}", daemon=True)
        self.serial = serial_handle
        self.serial_port = serial_port
        self.session = session
        self.session_id = session_id
        self.capture_id = capture_id
        self.phase = phase
        self.stop_event = stop_event
        self.schema_sha256 = schema_sha256
        self.measurement_frames: list[dict[str, Any]] = []
        self.valid_count = 0
        self.rejected_count = 0
        self.error: str | None = None

    def _record_line(self, raw: bytes, wall: int, mono: int) -> None:
        phase = self.phase.get()
        decoded = raw.decode("ascii", errors="replace").strip("\r")
        exact_sha = sha256_bytes(raw)
        parts = decoded.strip().split(",")
        parsed: dict[str, int] | None = None
        reason = None
        if len(parts) != len(STREAM_COLUMNS):
            reason = f"field_count={len(parts)},expected={len(STREAM_COLUMNS)}"
        else:
            try:
                parsed = {name: int(parts[i]) for i, name in enumerate(STREAM_COLUMNS)}
            except ValueError as exc:
                reason = f"non_integer:{exc}"

        raw_event = {
            "format_version": FORMAT_VERSION,
            "session_id": self.session_id,
            "capture_id": self.capture_id,
            "host_receive_time_ns": wall,
            "host_receive_utc": utc_iso_from_ns(wall),
            "host_receive_monotonic_ns": mono,
            "phase": phase,
            "serial_port": self.serial_port,
            "raw_b64": base64.b64encode(raw).decode("ascii"),
            "raw_sha256": exact_sha,
            "decoded_ascii": decoded,
            "schema_match": parsed is not None,
            "reject_reason": reason,
            "real_data_only": True,
            "simulated": False,
            "synthetic": False,
        }
        append_jsonl(self.session / "serial_lines.jsonl", raw_event)

        if parsed is None:
            self.rejected_count += 1
            return

        i_count = parsed["i_adc_raw_count"]
        v_count = parsed["vref_adc_raw_count"]
        factory = parsed["vref_factory_cal_raw"]
        i_mean = parsed["i_adc_raw_sum"] / i_count if i_count > 0 else None
        v_mean = parsed["vref_adc_raw_sum"] / v_count if v_count > 0 else None
        i_variance = (
            max(0.0, parsed["i_adc_raw_sumsq"] / i_count - i_mean * i_mean)
            if i_mean is not None
            else None
        )
        v_variance = (
            max(0.0, parsed["vref_adc_raw_sumsq"] / v_count - v_mean * v_mean)
            if v_mean is not None
            else None
        )
        i_sd = math.sqrt(i_variance) if i_variance is not None else None
        v_sd = math.sqrt(v_variance) if v_variance is not None else None
        ratio = i_mean / v_mean if i_mean is not None and v_mean not in (None, 0) else None
        factory_uv = (
            ratio * FACTORY_CAL_VDDA_MV * 1000.0 * factory / ADC_FULL_SCALE
            if ratio is not None and factory > 0
            else None
        )
        row: dict[str, Any] = {
            "session_id": self.session_id,
            "capture_id": self.capture_id,
            "host_receive_time_ns": wall,
            "host_receive_utc": utc_iso_from_ns(wall),
            "host_receive_monotonic_ns": mono,
            "phase": phase,
            "serial_port": self.serial_port,
            "schema_sha256": self.schema_sha256,
            "raw_line_sha256": exact_sha,
            "raw_line": decoded,
            **parsed,
            "pa0_raw_mean_code": i_mean,
            "pa0_raw_sd_code": i_sd,
            "vref_raw_mean_code": v_mean,
            "vref_raw_sd_code": v_sd,
            "pa0_vref_ratio": ratio,
            "pa0_factory_uv": factory_uv,
        }
        append_csv_row(self.session / "frames.csv", SERIAL_FRAME_FIELDS, row)
        self.valid_count += 1
        if phase == "measurement":
            self.measurement_frames.append(row)

    def run(self) -> None:
        buffer = bytearray()
        try:
            while not self.stop_event.is_set():
                waiting = getattr(self.serial, "in_waiting", 0)
                chunk = self.serial.read(waiting if waiting else 1)
                if not chunk:
                    continue
                # Timestamp receipt immediately after the OS read. If one USB
                # transfer contains multiple lines they deliberately share this
                # transport timestamp; MCU window timestamps disambiguate the
                # actual acquisition intervals.
                chunk_wall = utc_now_ns()
                chunk_mono = time.monotonic_ns()
                buffer.extend(chunk)
                while True:
                    newline = buffer.find(b"\n")
                    if newline < 0:
                        break
                    raw = bytes(buffer[:newline])
                    del buffer[: newline + 1]
                    self._record_line(raw, chunk_wall, chunk_mono)
            # A partial line is evidence too, but it is never parsed as a frame.
            if buffer:
                raw_event = {
                    "format_version": FORMAT_VERSION,
                    "session_id": self.session_id,
                    "capture_id": self.capture_id,
                    "host_receive_time_ns": utc_now_ns(),
                    "host_receive_monotonic_ns": time.monotonic_ns(),
                    "phase": self.phase.get(),
                    "serial_port": self.serial_port,
                    "raw_b64": base64.b64encode(bytes(buffer)).decode("ascii"),
                    "raw_sha256": sha256_bytes(bytes(buffer)),
                    "decoded_ascii": bytes(buffer).decode("ascii", errors="replace"),
                    "schema_match": False,
                    "reject_reason": "partial_line_at_close",
                    "real_data_only": True,
                    "simulated": False,
                    "synthetic": False,
                }
                append_jsonl(self.session / "serial_lines.jsonl", raw_event)
                self.rejected_count += 1
        except Exception:
            self.error = traceback.format_exc()


DMM_FIELDS = [
    "session_id",
    "capture_id",
    "phase",
    "query_start_time_ns",
    "query_start_utc",
    "query_start_monotonic_ns",
    "query_end_time_ns",
    "query_end_utc",
    "query_end_monotonic_ns",
    "query_midpoint_time_ns",
    "query_duration_ns",
    "value_v",
    "value_mv",
    "nplc",
    "line_frequency_hz",
    "nominal_aperture_s",
    "visa_resource",
    "status",
    "error",
]


class Dmm:
    def __init__(self, resource: str) -> None:
        try:
            import pyvisa
        except ImportError as exc:
            raise RuntimeError("pyvisa is required for Keysight DMM capture") from exc
        self.resource_name = resource
        self.rm = pyvisa.ResourceManager()
        self.instrument = self.rm.open_resource(resource, open_timeout=6000)
        self.instrument.timeout = 20000
        self.commands = [
            "*CLS",
            "CONF:VOLT:DC 10",
            "SENS:VOLT:DC:RANG:AUTO OFF",
            "SENS:VOLT:DC:RANG 10",
            "SENS:VOLT:DC:NPLC 10",
            "SENS:VOLT:DC:ZERO:AUTO ON",
            "SENS:VOLT:DC:IMP:AUTO ON",
            "TRIG:SOUR IMM",
            "SAMP:COUN 1",
        ]
        for command in self.commands:
            self.instrument.write(command)
        self.idn = self.instrument.query("*IDN?").strip()
        self.nplc = self._float_query("SENS:VOLT:DC:NPLC?", 10.0)
        self.line_frequency_hz = self._float_query("SYST:LFR?", 60.0)
        self.range_v = self._float_query("SENS:VOLT:DC:RANG?", 10.0)
        self.autozero = self.instrument.query("SENS:VOLT:DC:ZERO:AUTO?").strip()
        self.input_impedance_auto = self.instrument.query(
            "SENS:VOLT:DC:IMP:AUTO?"
        ).strip()
        self.configuration_error = self.instrument.query("SYST:ERR?").strip()
        if not self.configuration_error.lstrip("+").startswith("0,"):
            raise RuntimeError(
                f"DMM rejected a configuration command: {self.configuration_error}"
            )

    def _float_query(self, command: str, fallback: float) -> float:
        try:
            return float(self.instrument.query(command))
        except Exception:
            return fallback

    @property
    def identity(self) -> dict[str, Any]:
        return {
            "resource": self.resource_name,
            "idn": self.idn,
            "configuration_commands": self.commands,
            "nplc": self.nplc,
            "line_frequency_hz": self.line_frequency_hz,
            "range_v": self.range_v,
            "autozero_readback": self.autozero,
            "input_impedance_auto_readback": self.input_impedance_auto,
            "configuration_error_queue": self.configuration_error,
            "nominal_aperture_s": self.nplc / self.line_frequency_hz,
            "timing_note": (
                "query bounds contain the conversion; nominal aperture is NPLC/line "
                "frequency. Host timestamps are not an instrument timebase."
            ),
        }

    def read_once(self) -> tuple[dict[str, Any], float | None]:
        start_wall = utc_now_ns()
        start_mono = time.monotonic_ns()
        value = None
        status = "ok"
        error = ""
        try:
            candidate = float(self.instrument.query("READ?"))
            if not math.isfinite(candidate) or abs(candidate) >= 1e30:
                raise ValueError(f"non-finite/overflow reading {candidate!r}")
            value = candidate
        except Exception as exc:
            status = "error"
            error = repr(exc)
        end_mono = time.monotonic_ns()
        end_wall = utc_now_ns()
        record = {
            "query_start_time_ns": start_wall,
            "query_start_utc": utc_iso_from_ns(start_wall),
            "query_start_monotonic_ns": start_mono,
            "query_end_time_ns": end_wall,
            "query_end_utc": utc_iso_from_ns(end_wall),
            "query_end_monotonic_ns": end_mono,
            "query_midpoint_time_ns": (start_wall + end_wall) // 2,
            "query_duration_ns": end_mono - start_mono,
            "value_v": value,
            "value_mv": value * 1000.0 if value is not None else None,
            "nplc": self.nplc,
            "line_frequency_hz": self.line_frequency_hz,
            "nominal_aperture_s": self.nplc / self.line_frequency_hz,
            "visa_resource": self.resource_name,
            "status": status,
            "error": error,
        }
        return record, value

    def close(self) -> None:
        try:
            self.instrument.close()
        finally:
            try:
                self.rm.close()
            except Exception:
                pass


class DmmRecorder(threading.Thread):
    def __init__(
        self,
        dmm: Dmm,
        session: Path,
        session_id: str,
        capture_id: str,
        phase: Phase,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name=f"dmm-{capture_id}", daemon=True)
        self.dmm = dmm
        self.session = session
        self.session_id = session_id
        self.capture_id = capture_id
        self.phase = phase
        self.stop_event = stop_event
        self.measurement_values: list[dict[str, Any]] = []
        self.error: str | None = None

    def run(self) -> None:
        try:
            while not self.stop_event.is_set():
                phase = self.phase.get()
                record, value = self.dmm.read_once()
                record.update(
                    {
                        "session_id": self.session_id,
                        "capture_id": self.capture_id,
                        "phase": phase,
                    }
                )
                append_csv_row(self.session / "dmm_readings.csv", DMM_FIELDS, record)
                if phase == "measurement" and value is not None:
                    self.measurement_values.append(record)
        except Exception:
            self.error = traceback.format_exc()


class ScpiSocket:
    """Minimal definite-length-block SCPI client for the scope's raw TCP port."""

    def __init__(self, host: str, port: int, timeout_s: float = 15.0) -> None:
        addresses = socket.getaddrinfo(
            host, port, socket.AF_INET6, socket.SOCK_STREAM
        )
        if not addresses:
            raise RuntimeError(f"cannot resolve IPv6 scope address {host}:{port}")
        family, socktype, proto, _canon, address = addresses[0]
        self.socket = socket.socket(family, socktype, proto)
        self.socket.settimeout(timeout_s)
        self.socket.connect(address)
        self.host = host
        self.port = port

    def write(self, command: str) -> None:
        self.socket.sendall((command.rstrip("\n") + "\n").encode("ascii"))

    def _readline(self) -> bytes:
        output = bytearray()
        while True:
            byte = self.socket.recv(1)
            if not byte:
                raise ConnectionError("scope closed the SCPI socket")
            output.extend(byte)
            if byte == b"\n":
                return bytes(output)

    def query(self, command: str) -> str:
        self.write(command)
        return self._readline().decode("ascii", errors="replace").strip()

    def query_binary_block(self, command: str) -> bytes:
        self.write(command)
        prefix = bytearray()
        while True:
            byte = self.socket.recv(1)
            if not byte:
                raise ConnectionError("scope closed before IEEE block header")
            if byte == b"#":
                break
            prefix.extend(byte)
            if len(prefix) > 1024:
                raise RuntimeError(f"scope did not return an IEEE block: {prefix!r}")
        digits_raw = self.socket.recv(1)
        if len(digits_raw) != 1 or not digits_raw.isdigit():
            raise RuntimeError(f"invalid IEEE block digit count: {digits_raw!r}")
        digits = int(digits_raw)
        if digits == 0:
            raise RuntimeError("indefinite-length IEEE blocks are not supported")
        length_bytes = bytearray()
        while len(length_bytes) < digits:
            chunk = self.socket.recv(digits - len(length_bytes))
            if not chunk:
                raise ConnectionError("scope closed inside IEEE length header")
            length_bytes.extend(chunk)
        try:
            payload_length = int(bytes(length_bytes))
        except ValueError as exc:
            raise RuntimeError(f"invalid IEEE payload length {length_bytes!r}") from exc
        payload = bytearray()
        while len(payload) < payload_length:
            chunk = self.socket.recv(min(1024 * 1024, payload_length - len(payload)))
            if not chunk:
                raise ConnectionError("scope closed inside waveform payload")
            payload.extend(chunk)
        # The optional line terminator can remain buffered safely because this
        # capture performs no further textual query after the binary transfer.
        return bytes(payload)

    def close(self) -> None:
        try:
            self.socket.close()
        except Exception:
            pass


def float_or_none(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and abs(result) < 1e30 else None


def parse_scope_preamble(text: str) -> dict[str, Any]:
    keys = [
        "format",
        "type",
        "points",
        "count",
        "x_increment_s",
        "x_origin_s",
        "x_reference",
        "y_increment_v",
        "y_origin_v",
        "y_reference",
    ]
    parts = [part.strip() for part in text.split(",")]
    result: dict[str, Any] = {"raw": text}
    for i, key in enumerate(keys):
        if i >= len(parts):
            result[key] = None
            continue
        value = float_or_none(parts[i])
        result[key] = value
    return result


def decode_scope_stats(payload: bytes, preamble: dict[str, Any]) -> dict[str, Any]:
    """Derived summary only; the immutable binary payload remains primary evidence."""
    if len(payload) < 2 or len(payload) % 2:
        return {"decoded": False, "reason": "WORD payload length is odd or empty"}
    samples = array.array("H")
    samples.frombytes(payload)
    if sys.byteorder != "little":
        samples.byteswap()
    y_increment = preamble.get("y_increment_v")
    y_origin = preamble.get("y_origin_v")
    y_reference = preamble.get("y_reference")
    if not all(finite_number(v) for v in (y_increment, y_origin, y_reference)):
        return {"decoded": False, "reason": "incomplete vertical preamble"}
    yi = float(y_increment)
    yo = float(y_origin)
    yr = float(y_reference)
    count = len(samples)
    code_mean = sum(samples) / count
    return {
        "decoded": True,
        "sample_count": count,
        "code_min": min(samples),
        "code_max": max(samples),
        "code_mean": code_mean,
        "voltage_min_v": (min(samples) - yr) * yi + yo,
        "voltage_max_v": (max(samples) - yr) * yi + yo,
        "voltage_mean_v": (code_mean - yr) * yi + yo,
        "note": "derived from the saved raw WORD block and saved preamble",
    }


class Scope:
    def __init__(self, host: str, port: int) -> None:
        self.scpi = ScpiSocket(host, port)
        self.host = host
        self.port = port
        self.idn = self.scpi.query("*IDN?")
        self.configuration_commands = [
            "*CLS",
            ":CHANnel1:DISPlay ON",
            ":CHANnel1:COUPling DC",
            ":CHANnel1:IMPedance ONEM",
            ":CHANnel1:BWLimit ON",
            ":CHANnel1:SCALe 0.2",
            ":CHANnel1:OFFSet 1.65",
            ":TIMebase:SCALe 1e-3",
            ":TRIGger:SWEep AUTO",
            ":ACQuire:TYPE NORMal",
            ":WAVeform:SOURce CHANnel1",
            ":WAVeform:FORMat WORD",
            ":WAVeform:BYTeorder LSBFirst",
            ":WAVeform:UNSigned 1",
            ":WAVeform:POINts:MODE RAW",
            ":WAVeform:POINts 100000",
        ]
        for command in self.configuration_commands:
            self.scpi.write(command)
        self.scpi.query("*OPC?")
        configuration_error = self.scpi.query(":SYSTem:ERRor?")
        if not configuration_error.lstrip("+").startswith("0,"):
            raise RuntimeError(
                f"scope rejected a configuration command: {configuration_error}"
            )
        self.configuration_error = configuration_error
        self.settings = self._read_settings()

    def _safe_query(self, command: str) -> str | None:
        try:
            return self.scpi.query(command)
        except Exception:
            return None

    def _read_settings(self) -> dict[str, Any]:
        commands = {
            "probe_ratio": ":CHANnel1:PROBe?",
            "coupling": ":CHANnel1:COUPling?",
            "input_impedance": ":CHANnel1:IMPedance?",
            "bandwidth_limit": ":CHANnel1:BWLimit?",
            "vertical_scale_v_div": ":CHANnel1:SCALe?",
            "vertical_offset_v": ":CHANnel1:OFFSet?",
            "time_scale_s_div": ":TIMebase:SCALe?",
            "acquisition_type": ":ACQuire:TYPE?",
            "sample_rate_hz": ":ACQuire:SRATe?",
            "waveform_points": ":WAVeform:POINts?",
        }
        return {name: self._safe_query(command) for name, command in commands.items()}

    @property
    def identity(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "idn": self.idn,
            "configuration_commands": self.configuration_commands,
            "configuration_error_queue": self.configuration_error,
            "settings": self.settings,
            "role": (
                "waveform/ripple and settling evidence; not the precision DC or "
                "series-current reference"
            ),
        }

    def start(self) -> None:
        self.scpi.write(":RUN")

    def stop_and_capture(
        self, session: Path, capture_id: str
    ) -> dict[str, Any]:
        start_wall = utc_now_ns()
        start_mono = time.monotonic_ns()
        self.scpi.write(":STOP")
        self.scpi.query("*OPC?")
        measurements = {}
        queries = {
            "vaverage_v": ":MEASure:VAVerage? DISPlay,CHANnel1",
            "vpp_v": ":MEASure:VPP? CHANnel1",
            "vrms_ac_v": ":MEASure:VRMS? DISPlay,AC,CHANnel1",
            "frequency_hz": ":MEASure:FREQuency? CHANnel1",
        }
        for name, command in queries.items():
            measurements[name] = float_or_none(self._safe_query(command))

        preamble_text = self.scpi.query(":WAVeform:PREamble?")
        preamble = parse_scope_preamble(preamble_text)
        waveform_start_wall = utc_now_ns()
        waveform_start_mono = time.monotonic_ns()
        payload = self.scpi.query_binary_block(":WAVeform:DATA?")
        waveform_end_mono = time.monotonic_ns()
        waveform_end_wall = utc_now_ns()
        binary_name = f"scope/{capture_id}_ch1_word_lsb.bin"
        write_unique_bytes(session / binary_name, payload)
        result = {
            "capture_id": capture_id,
            "capture_start_time_ns": start_wall,
            "capture_start_utc": utc_iso_from_ns(start_wall),
            "capture_start_monotonic_ns": start_mono,
            "capture_end_time_ns": waveform_end_wall,
            "capture_end_utc": utc_iso_from_ns(waveform_end_wall),
            "capture_end_monotonic_ns": waveform_end_mono,
            "waveform_query_start_time_ns": waveform_start_wall,
            "waveform_query_start_monotonic_ns": waveform_start_mono,
            "waveform_query_end_time_ns": waveform_end_wall,
            "waveform_query_end_monotonic_ns": waveform_end_mono,
            "measurements": measurements,
            "settings": self.settings,
            "preamble": preamble,
            "binary_file": binary_name,
            "binary_bytes": len(payload),
            "binary_sha256": sha256_bytes(payload),
            "binary_encoding": "unsigned 16-bit little-endian WORD samples",
            "derived_waveform_stats": decode_scope_stats(payload, preamble),
            "real_data_only": True,
            "simulated": False,
            "synthetic": False,
        }
        metadata_name = f"scope/{capture_id}_metadata.json"
        result["metadata_file"] = metadata_name
        write_unique_json(session / metadata_name, result)
        return result

    def close(self) -> None:
        self.scpi.close()


def numeric_summary(values: Iterable[float]) -> dict[str, Any]:
    data = [float(value) for value in values if finite_number(value)]
    if not data:
        return {
            "count": 0,
            "mean": None,
            "median": None,
            "sd": None,
            "min": None,
            "max": None,
        }
    return {
        "count": len(data),
        "mean": statistics.fmean(data),
        "median": statistics.median(data),
        "sd": statistics.pstdev(data) if len(data) > 1 else 0.0,
        "min": min(data),
        "max": max(data),
    }


def summarize_board(frames: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [
        row
        for row in frames
        if finite_number(row.get("pa0_vref_ratio"))
        and finite_number(row.get("pa0_factory_uv"))
        and int(row.get("i_adc_raw_count", 0)) > 0
        and int(row.get("vref_adc_raw_count", 0)) > 0
    ]
    i_sum = sum(int(row["i_adc_raw_sum"]) for row in valid)
    i_sumsq = sum(int(row["i_adc_raw_sumsq"]) for row in valid)
    i_count = sum(int(row["i_adc_raw_count"]) for row in valid)
    v_sum = sum(int(row["vref_adc_raw_sum"]) for row in valid)
    v_sumsq = sum(int(row["vref_adc_raw_sumsq"]) for row in valid)
    v_count = sum(int(row["vref_adc_raw_count"]) for row in valid)
    factory_values = sorted({int(row["vref_factory_cal_raw"]) for row in valid})
    ratios = [float(row["pa0_vref_ratio"]) for row in valid]
    factory_mv = [float(row["pa0_factory_uv"]) / 1000.0 for row in valid]
    rpms = [float(row["rpm"]) for row in valid]
    vrefs = [float(row["vref_raw_mean_code"]) for row in valid]
    i_frame_sds = [float(row["pa0_raw_sd_code"]) for row in valid]
    vref_frame_sds = [float(row["vref_raw_sd_code"]) for row in valid]
    mins = [int(row["i_adc_raw_min"]) for row in valid]
    maxes = [int(row["i_adc_raw_max"]) for row in valid]
    frame_sequences = [int(row["frame_seq"]) & 0xFFFFFFFF for row in valid]
    sequence_steps = [
        (after - before) & 0xFFFFFFFF
        for before, after in zip(frame_sequences, frame_sequences[1:])
    ]
    sequence_nonadvancing = sum(
        step == 0 or step >= 0x80000000 for step in sequence_steps
    )
    sequence_gaps = sum(step != 1 for step in sequence_steps)
    adc_fail_counts = [int(row["i_adc_fail_count"]) for row in valid]
    pwm_ticks = [int(row["pwm_ticks"]) for row in valid]
    adc_windows = [
        (int(row["i_adc_window_end_us"]) - int(row["i_adc_window_start_us"]))
        & 0xFFFFFFFF
        for row in valid
    ]
    vref_windows = [
        (int(row["vref_window_end_us"]) - int(row["vref_window_start_us"]))
        & 0xFFFFFFFF
        for row in valid
    ]
    i_aggregate_variance = (
        max(0.0, i_sumsq / i_count - (i_sum / i_count) ** 2) if i_count else None
    )
    v_aggregate_variance = (
        max(0.0, v_sumsq / v_count - (v_sum / v_count) ** 2) if v_count else None
    )
    sumsq_consistency = [
        int(row["i_adc_raw_sumsq"]) * int(row["i_adc_raw_count"])
        >= int(row["i_adc_raw_sum"]) ** 2
        and int(row["vref_adc_raw_sumsq"]) * int(row["vref_adc_raw_count"])
        >= int(row["vref_adc_raw_sum"]) ** 2
        for row in valid
    ]
    adc_window_starts = [
        int(row["i_adc_window_start_us"]) & 0xFFFFFFFF for row in valid
    ]
    vref_window_starts = [
        int(row["vref_window_start_us"]) & 0xFFFFFFFF for row in valid
    ]

    def strictly_advancing_u32(values: list[int]) -> bool:
        return bool(values) and all(
            0 < ((after - before) & 0xFFFFFFFF) < 0x80000000
            for before, after in zip(values, values[1:])
        )

    return {
        "measurement_frame_count": len(frames),
        "valid_raw_frame_count": len(valid),
        "exact_pa0_raw_sum": i_sum,
        "exact_pa0_raw_sumsq": i_sumsq,
        "exact_pa0_raw_count": i_count,
        "exact_vref_raw_sum": v_sum,
        "exact_vref_raw_sumsq": v_sumsq,
        "exact_vref_raw_count": v_count,
        "pa0_mean_code_from_exact_sums": i_sum / i_count if i_count else None,
        "vref_mean_code_from_exact_sums": v_sum / v_count if v_count else None,
        "frame_pa0_vref_ratio": numeric_summary(ratios),
        "frame_pa0_factory_mv": numeric_summary(factory_mv),
        "rpm": numeric_summary(rpms),
        "vref_mean_code": numeric_summary(vrefs),
        "pa0_per_window_raw_sd_code": numeric_summary(i_frame_sds),
        "vref_per_window_raw_sd_code": numeric_summary(vref_frame_sds),
        "pa0_all_conversions_population_sd_code": (
            math.sqrt(i_aggregate_variance)
            if i_aggregate_variance is not None
            else None
        ),
        "vref_all_conversions_population_sd_code": (
            math.sqrt(v_aggregate_variance)
            if v_aggregate_variance is not None
            else None
        ),
        "raw_sumsq_consistent_all": bool(sumsq_consistency)
        and all(sumsq_consistency),
        "vref_factory_cal_raw_values": factory_values,
        "raw_conversion_min": min(mins) if mins else None,
        "raw_conversion_max": max(maxes) if maxes else None,
        "frame_seq_first": frame_sequences[0] if frame_sequences else None,
        "frame_seq_last": frame_sequences[-1] if frame_sequences else None,
        "frame_seq_nonadvancing_count": sequence_nonadvancing,
        "frame_seq_gap_count": sequence_gaps,
        "frame_seq_strictly_advancing": bool(frame_sequences)
        and sequence_nonadvancing == 0,
        "frame_seq_contiguous": bool(frame_sequences) and sequence_gaps == 0,
        "pwm_ticks": numeric_summary(pwm_ticks),
        "pwm_ticks_note": (
            "TIM3_CCR1 speed-command register captured by firmware in each frame; "
            "this is command provenance, not an ADC sample count"
        ),
        "i_adc_fail_count_total": sum(adc_fail_counts),
        "i_adc_fail_count_max": max(adc_fail_counts) if adc_fail_counts else None,
        "i_adc_window_duration_us": numeric_summary(adc_windows),
        "vref_window_duration_us": numeric_summary(vref_windows),
        "i_adc_window_starts_strictly_advancing": strictly_advancing_u32(
            adc_window_starts
        ),
        "vref_window_starts_strictly_advancing": strictly_advancing_u32(
            vref_window_starts
        ),
        "mcu_window_note": (
            "frame_seq and the *_window_*_us fields come from the MCU clock and "
            "identify the actual aggregate acquisition windows; host receipt times "
            "remain transport timestamps."
        ),
        "derivation": (
            "per frame: (i_adc_raw_sum/i_adc_raw_count) / "
            "(vref_adc_raw_sum/vref_adc_raw_count); PA0_mV = ratio * 3300 * "
            "vref_factory_cal_raw / 4095. No i_dc_mv/filter/zero/current "
            "calibration is used."
        ),
    }


def summarize_dmm(records: list[dict[str, Any]]) -> dict[str, Any]:
    volts = [
        float(record["value_v"])
        for record in records
        if record.get("status") == "ok" and finite_number(record.get("value_v"))
    ]
    bounds = [
        {
            "query_start_time_ns": record["query_start_time_ns"],
            "query_end_time_ns": record["query_end_time_ns"],
            "query_start_monotonic_ns": record["query_start_monotonic_ns"],
            "query_end_monotonic_ns": record["query_end_monotonic_ns"],
        }
        for record in records
        if record.get("status") == "ok"
    ]
    return {
        "measurement_read_count": len(volts),
        "volts": numeric_summary(volts),
        "millivolts": numeric_summary(value * 1000.0 for value in volts),
        "aperture_bounds": bounds,
        "timing_note": (
            "Each bound is the host interval around READ?. It contains the "
            "instrument conversion but is not a hardware trigger timestamp."
        ),
    }


def command_motor(serial_handle: Any, rpm: int) -> dict[str, Any]:
    payload = f"S{int(rpm)}\n".encode("ascii")
    start_wall = utc_now_ns()
    start_mono = time.monotonic_ns()
    written = serial_handle.write(payload)
    serial_handle.flush()
    end_mono = time.monotonic_ns()
    end_wall = utc_now_ns()
    return {
        "requested_rpm": int(rpm),
        "payload_ascii": payload.decode("ascii"),
        "payload_b64": base64.b64encode(payload).decode("ascii"),
        "bytes_written": written,
        "write_start_time_ns": start_wall,
        "write_start_utc": utc_iso_from_ns(start_wall),
        "write_start_monotonic_ns": start_mono,
        "write_end_time_ns": end_wall,
        "write_end_utc": utc_iso_from_ns(end_wall),
        "write_end_monotonic_ns": end_mono,
    }


def make_capture_id(target_rpm: int) -> str:
    wall = utc_now_ns()
    target = "off" if target_rpm == 0 else f"{target_rpm}rpm"
    return f"cap_{wall}_{target}_{uuid.uuid4().hex[:8]}"


def wait_interval(seconds: float) -> None:
    end = time.monotonic() + seconds
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.25, remaining))


def cmd_new(args: argparse.Namespace) -> int:
    require_raw_schema()
    if not finite_number(args.shunt_ohm) or args.shunt_ohm <= 0:
        raise RuntimeError("--shunt-ohm must be finite and positive")
    if not finite_number(args.gain) or args.gain <= 0:
        raise RuntimeError("--gain must be finite and positive")
    if args.series_meter_uncertainty is not None and (
        not finite_number(args.series_meter_uncertainty)
        or args.series_meter_uncertainty < 0
    ):
        raise RuntimeError("--series-meter-uncertainty must be finite and nonnegative")
    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    wall = utc_now_ns()
    session_id = (
        f"rawcal_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_"
        f"{uuid.uuid4().hex[:8]}"
    )
    session = EVIDENCE_ROOT / session_id
    session.mkdir(parents=False, exist_ok=False)
    schema_text = json_compact(list(STREAM_COLUMNS)).encode("utf-8")
    manifest = {
        "format_version": FORMAT_VERSION,
        "session_id": session_id,
        "created_time_ns": wall,
        "created_utc": utc_iso_from_ns(wall),
        "host": {
            "platform": sys.platform,
            "python": sys.version,
            "cwd": str(ROOT),
        },
        "operator": args.operator,
        "series_ammeter": {
            "identity": args.series_meter,
            "range": args.series_meter_range,
            "declared_uncertainty_a": args.series_meter_uncertainty,
            "interface": "human_read; no computer timestamp is claimed",
        },
        "circuit_declaration": {
            "shunt_ohm": args.shunt_ohm,
            "ina_gain_v_v": args.gain,
            "parts_derived_mv_per_a": args.shunt_ohm * args.gain * 1000.0,
            "factory_cal_vdda_mv": FACTORY_CAL_VDDA_MV,
            "adc_full_scale_code": ADC_FULL_SCALE,
        },
        "stream_columns": list(STREAM_COLUMNS),
        "stream_schema_sha256": sha256_bytes(schema_text),
        "tool": {
            "path": str(Path(__file__).resolve()),
            "sha256_at_session_creation": sha256_file(Path(__file__).resolve()),
            "git": git_revision(),
        },
        "provenance_policy": {
            "real_data_only": True,
            "simulated": False,
            "synthetic": False,
            "current_truth": (
                "fresh human-entered series-ammeter value attached to one capture ID"
            ),
            "forbidden_inputs": [
                "historical current tables",
                "interpolated current references",
                "i_dc_mv",
                "firmware/application current values",
                "simulated or synthetic samples",
            ],
            "evidence_mutability": (
                "session.json is write-once; JSONL/CSV logs append; waveform and "
                "finalization files have unique names; corrections append events"
            ),
        },
        "notes": args.note,
    }
    write_unique_json(session / "session.json", manifest)
    created = event_base(session_id, "session_created")
    created["manifest_sha256"] = sha256_file(session / "session.json")
    append_jsonl(session / "events.jsonl", created)
    set_active_session(session)
    print(f"New raw calibration session:\n  {session}")
    print(f"Session ID: {session_id}")
    if not args.series_meter:
        print(
            "WARNING: series-meter identity is blank. Captures can be collected, "
            "but the provenance quality gate will fail."
        )
    print("Next: capture OFF, read the series ammeter, then attach that reading.")
    return 0


def validate_capture_options(args: argparse.Namespace) -> None:
    if args.settle <= 0 or args.duration <= 0:
        raise RuntimeError("--settle and --duration must be positive")
    if args.target > 100000:
        raise RuntimeError("requested RPM is implausibly large")


def cmd_capture(args: argparse.Namespace) -> int:
    require_raw_schema()
    validate_capture_options(args)
    session = resolve_session(args.session)
    manifest = session_manifest(session)
    session_id = manifest["session_id"]
    capture_id = make_capture_id(args.target)
    phase = Phase()
    serial_stop = threading.Event()
    dmm_stop = threading.Event()
    serial_recorder: SerialRecorder | None = None
    dmm_recorder: DmmRecorder | None = None
    serial_handle = None
    dmm: Dmm | None = None
    scope: Scope | None = None
    scope_result: dict[str, Any] | None = None
    port = None
    command_record: dict[str, Any] | None = None
    safe_off_record: dict[str, Any] | None = None

    requested = event_base(session_id, "capture_requested")
    requested.update(
        {
            "capture_id": capture_id,
            "target_rpm": args.target,
            "state": "off" if args.target == 0 else "on",
            "settle_s": args.settle,
            "duration_s": args.duration,
            "leave_running_on_success": args.target > 0 and not args.motor_off_after,
            "dmm_resource": args.dmm,
            "scope_address": f"[{args.scope_host}]:{args.scope_port}",
            "tool_path": str(Path(__file__).resolve()),
            "tool_sha256": sha256_file(Path(__file__).resolve()),
        }
    )
    append_jsonl(session / "events.jsonl", requested)
    print(f"Capture ID: {capture_id}")

    try:
        port, observed_ports = find_stlink_port(args.port)
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError("pyserial is required for hardware capture") from exc
        serial_handle = serial.Serial(port, BAUD, timeout=0.1, write_timeout=2.0)
        serial_handle.reset_input_buffer()
        connected = event_base(session_id, "board_connected")
        connected.update(
            {
                "capture_id": capture_id,
                "serial_port": port,
                "baud": BAUD,
                "observed_ports": observed_ports,
            }
        )
        append_jsonl(session / "events.jsonl", connected)

        serial_recorder = SerialRecorder(
            serial_handle,
            port,
            session,
            session_id,
            capture_id,
            phase,
            serial_stop,
            manifest["stream_schema_sha256"],
        )
        serial_recorder.start()

        try:
            dmm = Dmm(args.dmm)
            dmm_event = event_base(session_id, "dmm_connected")
            dmm_event.update({"capture_id": capture_id, "instrument": dmm.identity})
            append_jsonl(session / "events.jsonl", dmm_event)
            dmm_recorder = DmmRecorder(
                dmm, session, session_id, capture_id, phase, dmm_stop
            )
            dmm_recorder.start()
        except Exception as exc:
            missing = event_base(session_id, "dmm_unavailable")
            missing.update({"capture_id": capture_id, "error": repr(exc)})
            append_jsonl(session / "events.jsonl", missing)
            if not args.allow_missing_dmm:
                raise RuntimeError(f"required DMM is unavailable: {exc}") from exc
            print(f"WARNING: DMM unavailable: {exc}")

        try:
            scope = Scope(args.scope_host, args.scope_port)
            scope_event = event_base(session_id, "scope_connected")
            scope_event.update({"capture_id": capture_id, "instrument": scope.identity})
            append_jsonl(session / "events.jsonl", scope_event)
            scope.start()
        except Exception as exc:
            missing = event_base(session_id, "scope_unavailable")
            missing.update({"capture_id": capture_id, "error": repr(exc)})
            append_jsonl(session / "events.jsonl", missing)
            if not args.allow_missing_scope:
                raise RuntimeError(f"required scope is unavailable: {exc}") from exc
            print(f"WARNING: scope unavailable: {exc}")
            scope = None

        phase.set("settle")
        command_record = command_motor(serial_handle, args.target)
        commanded = event_base(session_id, "motor_commanded")
        commanded.update({"capture_id": capture_id, "command": command_record})
        append_jsonl(session / "events.jsonl", commanded)
        label = "OFF" if args.target == 0 else f"{args.target} RPM"
        print(f"Commanded {label}; recording settle data for {args.settle:.1f} s...")
        wait_interval(args.settle)

        phase.set("measurement")
        measure_start_wall = utc_now_ns()
        measure_start_mono = time.monotonic_ns()
        started = event_base(session_id, "measurement_started")
        started.update(
            {
                "capture_id": capture_id,
                "measurement_start_time_ns": measure_start_wall,
                "measurement_start_monotonic_ns": measure_start_mono,
            }
        )
        append_jsonl(session / "events.jsonl", started)
        print(
            f"Measurement window {args.duration:.1f} s is live now. "
            "Read the physical series ammeter during this stable window."
        )
        wait_interval(args.duration)
        measure_end_mono = time.monotonic_ns()
        measure_end_wall = utc_now_ns()
        phase.set("post")

        if scope is not None:
            try:
                scope_result = scope.stop_and_capture(session, capture_id)
                scoped = event_base(session_id, "scope_capture_completed")
                scoped.update({"capture_id": capture_id, "scope": scope_result})
                append_jsonl(session / "events.jsonl", scoped)
            except Exception as exc:
                failed_scope = event_base(session_id, "scope_capture_failed")
                failed_scope.update(
                    {
                        "capture_id": capture_id,
                        "error": repr(exc),
                        "traceback": traceback.format_exc(),
                    }
                )
                append_jsonl(session / "events.jsonl", failed_scope)
                print(f"WARNING: scope waveform transfer failed: {exc}")

        dmm_stop.set()
        if dmm_recorder is not None:
            dmm_recorder.join(timeout=25.0)
        wait_interval(0.25)
        serial_stop.set()
        if serial_recorder is not None:
            serial_recorder.join(timeout=5.0)

        if serial_recorder is None:
            raise RuntimeError("serial recorder was not started")
        if serial_recorder.is_alive():
            raise RuntimeError("serial recorder did not stop cleanly")
        if serial_recorder.error:
            raise RuntimeError(f"serial recorder failed:\n{serial_recorder.error}")
        if dmm_recorder is not None and dmm_recorder.error:
            raise RuntimeError(f"DMM recorder failed:\n{dmm_recorder.error}")

        if args.target > 0 and args.motor_off_after:
            safe_off_record = command_motor(serial_handle, 0)
            off_event = event_base(session_id, "motor_commanded_off_after_capture")
            off_event.update({"capture_id": capture_id, "command": safe_off_record})
            append_jsonl(session / "events.jsonl", off_event)

        board_summary = summarize_board(serial_recorder.measurement_frames)
        dmm_summary = summarize_dmm(
            dmm_recorder.measurement_values if dmm_recorder is not None else []
        )
        if board_summary["valid_raw_frame_count"] == 0:
            raise RuntimeError("no valid raw NUCLEO frames in the measurement window")
        if dmm is not None and dmm_summary["measurement_read_count"] == 0:
            raise RuntimeError("no valid DMM readings in the measurement window")
        completed = event_base(session_id, "capture_completed")
        completed.update(
            {
                "capture_id": capture_id,
                "target_rpm": args.target,
                "state": "off" if args.target == 0 else "on",
                "measurement_start_time_ns": measure_start_wall,
                "measurement_start_utc": utc_iso_from_ns(measure_start_wall),
                "measurement_start_monotonic_ns": measure_start_mono,
                "measurement_end_time_ns": measure_end_wall,
                "measurement_end_utc": utc_iso_from_ns(measure_end_wall),
                "measurement_end_monotonic_ns": measure_end_mono,
                "command": command_record,
                "board": board_summary,
                "dmm": dmm_summary,
                "scope": scope_result,
                "serial_valid_total": serial_recorder.valid_count,
                "serial_rejected_total": serial_recorder.rejected_count,
                "left_running": args.target > 0 and not args.motor_off_after,
                "manual_current_a": None,
                "manual_current_note": (
                    "No current is inferred here. Attach the physical series-meter "
                    "reading with the manual command."
                ),
            }
        )
        append_jsonl(session / "events.jsonl", completed)

        rpm_mean = board_summary["rpm"]["mean"]
        board_mv = board_summary["frame_pa0_factory_mv"]["mean"]
        dmm_mv = dmm_summary["millivolts"]["mean"]
        print(f"Capture complete: {capture_id}")
        print(f"  measured RPM mean: {rpm_mean if rpm_mean is not None else 'NO DATA'}")
        print(f"  board raw/VREF PA0: {board_mv if board_mv is not None else 'NO DATA'} mV")
        print(f"  DMM PA0 mean: {dmm_mv if dmm_mv is not None else 'NO DATA'} mV")
        print(
            f"  raw board frames: {board_summary['valid_raw_frame_count']}; "
            f"DMM reads: {dmm_summary['measurement_read_count']}"
        )
        if args.target > 0 and not args.motor_off_after:
            print(f"Motor remains commanded at {args.target} RPM.")
        else:
            print("Motor is commanded OFF.")
        print(
            "Attach the value seen during the window:\n"
            f"  py -3 tools\\calibrate_current_raw.py manual {capture_id} AMPS --stable"
        )
        return 0
    except BaseException as exc:
        phase.set("exception")
        if serial_handle is not None:
            try:
                safe_off_record = command_motor(serial_handle, 0)
            except Exception as off_exc:
                safe_off_record = {"error": repr(off_exc)}
        failed = event_base(session_id, "capture_failed")
        failed.update(
            {
                "capture_id": capture_id,
                "target_rpm": args.target,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "safe_motor_off_attempt": safe_off_record,
            }
        )
        append_jsonl(session / "events.jsonl", failed)
        print(f"Capture failed; motor-OFF command was attempted: {exc}", file=sys.stderr)
        return 2
    finally:
        dmm_stop.set()
        serial_stop.set()
        if dmm_recorder is not None and dmm_recorder.is_alive():
            dmm_recorder.join(timeout=25.0)
        if serial_recorder is not None and serial_recorder.is_alive():
            serial_recorder.join(timeout=5.0)
        if dmm is not None:
            try:
                dmm.close()
            except Exception:
                pass
        if scope is not None:
            try:
                scope.close()
            except Exception:
                pass
        if serial_handle is not None:
            try:
                serial_handle.close()
            except Exception:
                pass


def capture_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event.get("event_type") == "capture_completed"]


def latest_manual_events(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("event_type") == "manual_current_recorded":
            latest[event["capture_id"]] = event
    return latest


def resolve_capture_id(events: list[dict[str, Any]], value: str) -> str:
    ids = [event["capture_id"] for event in capture_events(events)]
    exact = [capture_id for capture_id in ids if capture_id == value]
    if exact:
        return exact[0]
    matches = [capture_id for capture_id in ids if capture_id.startswith(value)]
    if not matches:
        raise RuntimeError(f"no completed capture matches {value!r}")
    if len(matches) > 1:
        raise RuntimeError(f"capture prefix {value!r} is ambiguous: {matches}")
    return matches[0]


def parse_observed_time(value: str | None) -> tuple[str | None, int | None]:
    if not value:
        return None, None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise RuntimeError("--observed-at must include a timezone or end in Z")
    parsed_utc = parsed.astimezone(dt.timezone.utc)
    return (
        parsed_utc.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        int(parsed_utc.timestamp() * 1e9),
    )


def cmd_manual(args: argparse.Namespace) -> int:
    if not math.isfinite(args.amps) or args.amps < 0:
        raise RuntimeError("manual current must be a finite, nonnegative value")
    if args.uncertainty is not None and (
        not math.isfinite(args.uncertainty) or args.uncertainty < 0
    ):
        raise RuntimeError("--uncertainty must be finite and nonnegative")
    session = resolve_session(args.session)
    manifest = session_manifest(session)
    events = load_events(session)
    capture_id = resolve_capture_id(events, args.capture_id)
    capture = next(
        event for event in capture_events(events) if event["capture_id"] == capture_id
    )
    observed_utc, observed_ns = parse_observed_time(args.observed_at)
    event = event_base(manifest["session_id"], "manual_current_recorded")
    event.update(
        {
            "capture_id": capture_id,
            "capture_target_rpm": capture["target_rpm"],
            "capture_state": capture["state"],
            "current_a": float(args.amps),
            "uncertainty_a": args.uncertainty,
            "stable_during_capture_attested": bool(args.stable),
            "state_and_reading_continuity_attested": bool(args.stable),
            "observed_at_utc": observed_utc,
            "observed_at_time_ns": observed_ns,
            "entered_after_capture": True,
            "entry_delay_after_capture_s": (
                event["host_time_ns"] - int(capture["host_time_ns"])
            )
            / 1e9,
            "motor_was_left_running_after_capture": bool(
                capture.get("left_running")
            ),
            "series_meter_identity": (
                args.series_meter
                if args.series_meter is not None
                else manifest["series_ammeter"].get("identity", "")
            ),
            "source_type": "human_read_physical_series_ammeter",
            "note": args.note,
            "timing_limitation": (
                "The human-read meter has no computer trigger. observed_at is "
                "operator supplied; --stable attests that the commanded state and "
                "meter value remained stable across the captured window and the "
                "operator reading. No exact hardware timestamp is invented."
            ),
        }
    )
    append_jsonl(session / "events.jsonl", event)
    previous = [
        old
        for old in events
        if old.get("event_type") == "manual_current_recorded"
        and old.get("capture_id") == capture_id
    ]
    print(
        f"Recorded real series-current event: {args.amps:.9g} A -> {capture_id}"
    )
    if previous:
        print(
            f"This is revision {len(previous) + 1}; {len(previous)} older event(s) "
            "remain in the append-only log. finalize uses the latest."
        )
    if not args.stable:
        print(
            "WARNING: --stable was not supplied. The reading is preserved, but the "
            "stable-window eligibility gate will fail."
        )
    return 0


def short_capture_id(value: str) -> str:
    if len(value) <= 34:
        return value
    return value[:25] + "…" + value[-8:]


def cmd_status(args: argparse.Namespace) -> int:
    session = resolve_session(args.session)
    manifest = session_manifest(session)
    events = load_events(session)
    captures = capture_events(events)
    manual = latest_manual_events(events)
    failures = [event for event in events if event.get("event_type") == "capture_failed"]
    print(f"Session: {manifest['session_id']}")
    print(f"Path: {session}")
    print(f"Created: {manifest['created_utc']}")
    print(
        "Series meter: "
        + (manifest["series_ammeter"].get("identity") or "MISSING IDENTITY")
    )
    print(
        f"Completed captures: {len(captures)}; failed attempts: {len(failures)}; "
        f"manual currents attached: {len(manual)}"
    )
    if not captures:
        print("No completed captures yet.")
        return 0
    print()
    print(
        f"{'#':>2} {'state':>5} {'cmd':>6} {'rpm mean':>10} {'board mV':>11} "
        f"{'DMM mV':>11} {'series A':>10} {'stable':>7} capture"
    )
    for index, capture in enumerate(captures, 1):
        capture_id = capture["capture_id"]
        reading = manual.get(capture_id)
        board = capture["board"]["frame_pa0_factory_mv"]["mean"]
        rpm = capture["board"]["rpm"]["mean"]
        dmm = capture["dmm"]["millivolts"]["mean"]
        amps = reading.get("current_a") if reading else None
        stable = reading.get("stable_during_capture_attested") if reading else None
        print(
            f"{index:>2} {capture['state']:>5} {capture['target_rpm']:>6} "
            f"{rpm if rpm is not None else float('nan'):>10.2f} "
            f"{board if board is not None else float('nan'):>11.4f} "
            f"{dmm if dmm is not None else float('nan'):>11.4f} "
            f"{amps if amps is not None else float('nan'):>10.6f} "
            f"{str(stable) if stable is not None else '-':>7} "
            f"{short_capture_id(capture_id)}"
        )
    missing = [capture["capture_id"] for capture in captures if capture["capture_id"] not in manual]
    if missing:
        print("\nManual readings still needed:")
        for capture_id in missing:
            print(
                f"  py -3 tools\\calibrate_current_raw.py manual {capture_id} "
                "AMPS --stable"
            )
    print(
        "\nEligible calibration cycles require the literal capture order OFF, ON, "
        "OFF and a stable manual current on all three."
    )
    return 0


def mean_or_none(*values: Any) -> float | None:
    numbers = [float(value) for value in values if finite_number(value)]
    return statistics.fmean(numbers) if len(numbers) == len(values) and numbers else None


def capture_quality(capture: dict[str, Any]) -> dict[str, Any]:
    board = capture.get("board", {})
    dmm = capture.get("dmm", {})
    rpm = board.get("rpm", {})
    factory_values = board.get("vref_factory_cal_raw_values", [])
    return {
        "enough_board_frames": board.get("valid_raw_frame_count", 0) >= 30,
        "enough_dmm_reads": dmm.get("measurement_read_count", 0) >= 5,
        "frame_sequence_advances": bool(board.get("frame_seq_strictly_advancing")),
        "frame_sequence_contiguous": bool(board.get("frame_seq_contiguous")),
        "no_adc_conversion_failures": board.get("i_adc_fail_count_total") == 0,
        "pwm_command_stable": (
            finite_number(board.get("pwm_ticks", {}).get("min"))
            and finite_number(board.get("pwm_ticks", {}).get("max"))
            and board["pwm_ticks"]["min"] == board["pwm_ticks"]["max"]
            and (
                board["pwm_ticks"]["max"] == 0
                if capture["target_rpm"] == 0
                else board["pwm_ticks"]["min"] > 0
            )
        ),
        "raw_sumsq_is_consistent": bool(board.get("raw_sumsq_consistent_all")),
        "mcu_adc_windows_present": (
            board.get("i_adc_window_duration_us", {}).get("count", 0)
            == board.get("valid_raw_frame_count", 0)
            and board.get("vref_window_duration_us", {}).get("count", 0)
            == board.get("valid_raw_frame_count", 0)
        ),
        "mcu_windows_advance": bool(
            board.get("i_adc_window_starts_strictly_advancing")
        )
        and bool(board.get("vref_window_starts_strictly_advancing"))
        and finite_number(board.get("i_adc_window_duration_us", {}).get("min"))
        and board["i_adc_window_duration_us"]["min"] > 0
        and finite_number(board.get("vref_window_duration_us", {}).get("min"))
        and board["vref_window_duration_us"]["min"] > 0,
        "one_factory_cal_word": len(factory_values) == 1
        and 1 <= factory_values[0] <= 4095,
        "raw_not_railed": (
            finite_number(board.get("raw_conversion_min"))
            and finite_number(board.get("raw_conversion_max"))
            and int(board["raw_conversion_min"]) > 2
            and int(board["raw_conversion_max"]) < 4093
        ),
        "rpm_tracks_command": (
            finite_number(rpm.get("mean"))
            and (
                float(rpm["mean"]) <= 30
                if capture["target_rpm"] == 0
                else abs(float(rpm["mean"]) - capture["target_rpm"])
                <= max(100.0, 0.05 * capture["target_rpm"])
            )
        ),
    }


def ordinary_fit(x: list[float], y: list[float]) -> dict[str, Any]:
    if len(x) != len(y) or len(x) < 2:
        return {"slope": None, "intercept": None, "r2": None}
    x_mean = statistics.fmean(x)
    y_mean = statistics.fmean(y)
    sxx = sum((value - x_mean) ** 2 for value in x)
    if sxx <= 0:
        return {"slope": None, "intercept": None, "r2": None}
    slope = sum((a - x_mean) * (b - y_mean) for a, b in zip(x, y)) / sxx
    intercept = y_mean - slope * x_mean
    residuals = [b - (slope * a + intercept) for a, b in zip(x, y)]
    sse = sum(value * value for value in residuals)
    sst = sum((value - y_mean) ** 2 for value in y)
    r2 = 1.0 - sse / sst if sst > 0 else None
    return {
        "slope": slope,
        "intercept": intercept,
        "r2": r2,
        "residuals": residuals,
    }


def through_origin_fit(x: list[float], y: list[float]) -> dict[str, Any]:
    denominator = sum(value * value for value in x)
    if len(x) < 1 or denominator <= 0:
        return {"slope": None, "residuals_mv": [], "predicted_current_a": []}
    slope = sum(a * b for a, b in zip(x, y)) / denominator
    residuals = [b - slope * a for a, b in zip(x, y)]
    predicted = [b / slope if slope else None for b in y]
    return {
        "slope": slope,
        "residuals_mv": residuals,
        "predicted_current_a": predicted,
    }


def rms(values: Iterable[float]) -> float | None:
    data = [float(value) for value in values if finite_number(value)]
    return math.sqrt(statistics.fmean(value * value for value in data)) if data else None


def build_cycles(
    captures: list[dict[str, Any]],
    manual: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cycles: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, on in enumerate(captures):
        if on.get("state") != "on":
            continue
        reasons = []
        if index == 0 or index == len(captures) - 1:
            reasons.append("not bracketed")
            before = after = None
        else:
            before, after = captures[index - 1], captures[index + 1]
            if before.get("state") != "off" or after.get("state") != "off":
                reasons.append("adjacent captures are not OFF/ON/OFF")
        trio = [value for value in (before, on, after) if value is not None]
        readings = []
        for capture in trio:
            reading = manual.get(capture["capture_id"])
            if reading is None:
                reasons.append(f"missing manual current for {capture['capture_id']}")
            else:
                readings.append(reading)
                if not reading.get("stable_during_capture_attested"):
                    reasons.append(f"manual current not stable-attested for {capture['capture_id']}")
        for capture in trio:
            quality = capture_quality(capture)
            failed = [name for name, passed in quality.items() if not passed]
            if failed:
                reasons.append(
                    f"{capture['capture_id']} capture quality: {','.join(failed)}"
                )
        if reasons or before is None or after is None or len(readings) != 3:
            rejected.append(
                {
                    "on_capture_id": on["capture_id"],
                    "target_rpm": on["target_rpm"],
                    "reasons": reasons,
                }
            )
            continue

        before_i = float(manual[before["capture_id"]]["current_a"])
        on_i = float(manual[on["capture_id"]]["current_a"])
        after_i = float(manual[after["capture_id"]]["current_a"])
        baseline_i = (before_i + after_i) / 2.0
        delta_i = on_i - baseline_i

        def board_mv(capture: dict[str, Any]) -> float:
            return float(capture["board"]["frame_pa0_factory_mv"]["mean"])

        def dmm_mv(capture: dict[str, Any]) -> float:
            return float(capture["dmm"]["millivolts"]["mean"])

        baseline_board = (board_mv(before) + board_mv(after)) / 2.0
        delta_board = board_mv(on) - baseline_board
        baseline_dmm = (dmm_mv(before) + dmm_mv(after)) / 2.0
        delta_dmm = dmm_mv(on) - baseline_dmm
        off_drift_board = board_mv(after) - board_mv(before)
        off_drift_dmm = dmm_mv(after) - dmm_mv(before)
        cycle = {
            "target_rpm": on["target_rpm"],
            "measured_rpm": on["board"]["rpm"]["mean"],
            "off_before_capture_id": before["capture_id"],
            "on_capture_id": on["capture_id"],
            "off_after_capture_id": after["capture_id"],
            "manual_current_a": {
                "off_before": before_i,
                "on": on_i,
                "off_after": after_i,
                "off_real_average": baseline_i,
                "delta": delta_i,
            },
            "board_factory_mv": {
                "off_before": board_mv(before),
                "on": board_mv(on),
                "off_after": board_mv(after),
                "off_real_average": baseline_board,
                "delta": delta_board,
                "off_drift": off_drift_board,
            },
            "dmm_mv": {
                "off_before": dmm_mv(before),
                "on": dmm_mv(on),
                "off_after": dmm_mv(after),
                "off_real_average": baseline_dmm,
                "delta": delta_dmm,
                "off_drift": off_drift_dmm,
            },
            "method": (
                "direct ON minus arithmetic mean of the two physically captured "
                "adjacent OFF states; no interpolated or table-derived current"
            ),
        }
        if delta_i <= 0:
            rejected.append(
                {
                    "on_capture_id": on["capture_id"],
                    "target_rpm": on["target_rpm"],
                    "reasons": ["ON current is not greater than averaged real OFF current"],
                    "computed_cycle": cycle,
                }
            )
        else:
            cycles.append(cycle)
    return cycles, rejected


def level_repeatability(cycles: list[dict[str, Any]]) -> dict[str, Any]:
    levels: dict[int, list[float]] = {}
    for cycle in cycles:
        current = cycle["manual_current_a"]["delta"]
        voltage = cycle["board_factory_mv"]["delta"]
        if current > 0:
            levels.setdefault(int(cycle["target_rpm"]), []).append(voltage / current)
    result = {}
    for rpm, slopes in sorted(levels.items()):
        mean = statistics.fmean(slopes)
        result[str(rpm)] = {
            "count": len(slopes),
            "sensitivities_mv_per_a": slopes,
            "mean_mv_per_a": mean,
            "sd_mv_per_a": statistics.pstdev(slopes) if len(slopes) > 1 else None,
            "cv": (
                statistics.pstdev(slopes) / abs(mean)
                if len(slopes) > 1 and mean != 0
                else None
            ),
        }
    return result


def gate(name: str, passed: bool, observed: Any, requirement: str) -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "observed": observed,
        "requirement": requirement,
    }


def build_final_report(
    session: Path,
    manifest: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    captures = capture_events(events)
    manual = latest_manual_events(events)
    cycles, rejected = build_cycles(captures, manual)

    # The application model is an absolute two-parameter fit.  OFF captures are
    # genuine total-current observations and participate directly; there is no
    # auto-zero and no fixed idle-current addback.  Chopped deltas below remain
    # valuable diagnostics because they cancel baseline drift.
    direct_points: list[dict[str, Any]] = []
    direct_rejected: list[dict[str, Any]] = []
    for capture in captures:
        reading = manual.get(capture["capture_id"])
        reasons = []
        ratio = capture.get("board", {}).get("frame_pa0_vref_ratio", {}).get("mean")
        if reading is None:
            reasons.append("missing manual series-current reading")
        elif not reading.get("stable_during_capture_attested"):
            reasons.append("manual reading lacks stable-window attestation")
        if not finite_number(ratio):
            reasons.append("missing raw PA0/VREF ratio")
        quality = capture_quality(capture)
        failed_quality = [name for name, passed in quality.items() if not passed]
        if failed_quality:
            reasons.append("capture quality: " + ",".join(failed_quality))
        if reasons:
            direct_rejected.append(
                {
                    "capture_id": capture["capture_id"],
                    "target_rpm": capture["target_rpm"],
                    "state": capture["state"],
                    "reasons": reasons,
                }
            )
            continue
        direct_points.append(
            {
                "capture_id": capture["capture_id"],
                "state": capture["state"],
                "target_rpm": capture["target_rpm"],
                "measured_rpm": capture["board"]["rpm"]["mean"],
                "raw_pa0_vref_ratio": float(ratio),
                "manual_total_current_a": float(reading["current_a"]),
                "manual_event_id": reading["event_id"],
                "model_role": (
                    "real OFF total-current point"
                    if capture["state"] == "off"
                    else "real ON total-current point"
                ),
            }
        )
    direct_ratio_x = [point["raw_pa0_vref_ratio"] for point in direct_points]
    direct_current_y = [point["manual_total_current_a"] for point in direct_points]
    direct_fit = ordinary_fit(direct_ratio_x, direct_current_y)
    direct_predictions = [
        direct_fit["slope"] * ratio + direct_fit["intercept"]
        for ratio in direct_ratio_x
    ] if finite_number(direct_fit.get("slope")) and finite_number(
        direct_fit.get("intercept")
    ) else []
    direct_errors = [
        predicted - actual
        for predicted, actual in zip(direct_predictions, direct_current_y)
    ]
    direct_error_rms = rms(direct_errors)
    direct_error_max = max((abs(value) for value in direct_errors), default=None)
    direct_current_span = (
        max(direct_current_y) - min(direct_current_y)
        if len(direct_current_y) >= 2
        else None
    )
    direct_has_off = any(point["state"] == "off" for point in direct_points)
    direct_has_on = any(point["state"] == "on" for point in direct_points)

    x = [float(cycle["manual_current_a"]["delta"]) for cycle in cycles]
    board_y = [float(cycle["board_factory_mv"]["delta"]) for cycle in cycles]
    dmm_y = [float(cycle["dmm_mv"]["delta"]) for cycle in cycles]
    board_fit = through_origin_fit(x, board_y)
    free_fit = ordinary_fit(x, board_y)
    dmm_fit = through_origin_fit(x, dmm_y)
    board_slope = board_fit.get("slope")
    dmm_slope = dmm_fit.get("slope")
    predicted = board_fit.get("predicted_current_a", [])
    current_errors = [
        pred - actual
        for pred, actual in zip(predicted, x)
        if pred is not None and finite_number(pred)
    ]
    delta_agreement = [b - d for b, d in zip(board_y, dmm_y)]
    levels = level_repeatability(cycles)
    repeat_counts = [level["count"] for level in levels.values()]
    repeat_cvs = [
        level["cv"] for level in levels.values() if finite_number(level.get("cv"))
    ]
    distinct_levels = len(levels)
    max_delta_current = max(x) if x else None
    current_span = max(x) - min(x) if len(x) >= 2 else None
    voltage_span = max(board_y) - min(board_y) if len(board_y) >= 2 else None
    max_voltage_rise = max(board_y) if board_y else None
    current_error_rms = rms(current_errors)
    current_error_max = max((abs(value) for value in current_errors), default=None)
    delta_agreement_rms = rms(delta_agreement)
    max_off_drift = max(
        (abs(cycle["board_factory_mv"]["off_drift"]) for cycle in cycles),
        default=None,
    )
    theory = float(manifest["circuit_declaration"]["parts_derived_mv_per_a"])
    theory_fraction = (
        abs(float(board_slope) - theory) / theory
        if finite_number(board_slope) and theory > 0
        else None
    )
    dmm_agreement_fraction = (
        abs(float(board_slope) - float(dmm_slope)) / abs(float(dmm_slope))
        if finite_number(board_slope) and finite_number(dmm_slope) and dmm_slope != 0
        else None
    )
    manual_identity_ok = bool(
        str(manifest["series_ammeter"].get("identity", "")).strip()
    ) and all(
        bool(str(manual[cap["capture_id"]].get("series_meter_identity", "")).strip())
        for cap in captures
        if cap["capture_id"] in manual
    )
    source_flags_ok = all(
        event.get("simulated") is False
        and event.get("synthetic") is False
        and event.get("real_data_only") is True
        for event in events
        if event.get("event_type")
        in {
            "session_created",
            "capture_requested",
            "capture_completed",
            "manual_current_recorded",
        }
    )
    scope_captures = sum(1 for capture in captures if capture.get("scope"))

    gates = [
        gate(
            "real-only provenance flags",
            source_flags_ok,
            source_flags_ok,
            "every primary evidence event explicitly real_data_only=true, "
            "simulated=false, synthetic=false",
        ),
        gate(
            "series ammeter identified",
            manual_identity_ok,
            manifest["series_ammeter"].get("identity"),
            "nonblank meter identity in session and readings",
        ),
        gate(
            "eligible chopped cycles",
            len(cycles) >= 15,
            len(cycles),
            "at least 15 eligible OFF/ON/OFF cycles",
        ),
        gate(
            "distinct RPM/load levels",
            distinct_levels >= 5,
            distinct_levels,
            "at least 5 distinct nonzero levels",
        ),
        gate(
            "three repeats at every level",
            bool(repeat_counts) and min(repeat_counts) >= 3,
            min(repeat_counts) if repeat_counts else 0,
            "at least 3 independent chopped cycles per level",
        ),
        gate(
            "current excitation",
            finite_number(max_delta_current) and max_delta_current >= 0.20,
            max_delta_current,
            "maximum ON-minus-OFF current at least 0.20 A",
        ),
        gate(
            "current span",
            finite_number(current_span) and current_span >= 0.10,
            current_span,
            "span of real delta-current levels at least 0.10 A",
        ),
        gate(
            "board voltage excitation",
            finite_number(max_voltage_rise) and max_voltage_rise >= 20.0,
            max_voltage_rise,
            "maximum raw/VREF-derived voltage rise at least 20 mV",
        ),
        gate(
            "absolute model includes real OFF and ON",
            direct_has_off and direct_has_on,
            {"off": direct_has_off, "on": direct_has_on},
            "absolute raw-ratio model contains fresh manual total-current points "
            "from both physical OFF and ON captures",
        ),
        gate(
            "absolute raw-ratio slope",
            finite_number(direct_fit.get("slope")) and direct_fit["slope"] > 0,
            direct_fit.get("slope"),
            "raw_ratio_slope_a_per_ratio greater than zero",
        ),
        gate(
            "absolute raw-ratio linearity",
            finite_number(direct_fit.get("r2")) and direct_fit["r2"] >= 0.995,
            direct_fit.get("r2"),
            "I_total versus untouched PA0/VREF ratio R^2 at least 0.995",
        ),
        gate(
            "absolute-model current RMS",
            finite_number(direct_error_rms)
            and direct_error_rms <= 0.01
            and (
                not finite_number(direct_current_span)
                or direct_error_rms <= 0.03 * direct_current_span
            ),
            direct_error_rms,
            "<=10 mA and <=3% of observed total-current span",
        ),
        gate(
            "absolute-model current worst case",
            finite_number(direct_error_max)
            and direct_error_max <= 0.015
            and (
                not finite_number(direct_current_span)
                or direct_error_max <= 0.05 * direct_current_span
            ),
            direct_error_max,
            "<=15 mA and <=5% of observed total-current span",
        ),
        gate(
            "positive sensitivity",
            finite_number(board_slope) and board_slope > 0,
            board_slope,
            "through-origin sensitivity greater than zero",
        ),
        gate(
            "linearity",
            finite_number(free_fit.get("r2")) and free_fit["r2"] >= 0.995,
            free_fit.get("r2"),
            "ordinary-fit R^2 at least 0.995",
        ),
        gate(
            "differenced intercept",
            finite_number(free_fit.get("intercept"))
            and abs(free_fit["intercept"])
            <= max(1.0, 0.02 * abs(voltage_span or 0.0)),
            free_fit.get("intercept"),
            "absolute free intercept <= max(1 mV, 2% of voltage span)",
        ),
        gate(
            "current prediction RMS",
            finite_number(current_error_rms)
            and current_error_rms <= 0.01
            and (
                not finite_number(max_delta_current)
                or current_error_rms <= 0.03 * max_delta_current
            ),
            current_error_rms,
            "<=10 mA and <=3% of maximum excitation",
        ),
        gate(
            "current prediction worst case",
            finite_number(current_error_max)
            and current_error_max <= 0.015
            and (
                not finite_number(max_delta_current)
                or current_error_max <= 0.05 * max_delta_current
            ),
            current_error_max,
            "<=15 mA and <=5% of maximum excitation",
        ),
        gate(
            "repeatability",
            bool(repeat_cvs) and max(repeat_cvs) <= 0.05,
            max(repeat_cvs) if repeat_cvs else None,
            "per-level sensitivity coefficient of variation <=5%",
        ),
        gate(
            "board-to-DMM delta RMS",
            finite_number(delta_agreement_rms) and delta_agreement_rms <= 2.0,
            delta_agreement_rms,
            "<=2.0 mV RMS between drift-cancelled board and DMM deltas",
        ),
        gate(
            "board-to-DMM fitted slope",
            finite_number(dmm_agreement_fraction) and dmm_agreement_fraction <= 0.02,
            dmm_agreement_fraction,
            "board and DMM fitted sensitivities agree within 2%",
        ),
        gate(
            "parts consistency",
            finite_number(theory_fraction) and theory_fraction <= 0.10,
            theory_fraction,
            "measured sensitivity within 10% of declared shunt*gain; larger "
            "discrepancy requires wiring/shunt investigation",
        ),
        gate(
            "bracketed OFF drift",
            finite_number(max_off_drift) and max_off_drift <= 5.0,
            max_off_drift,
            "absolute board baseline change across every bracket <=5 mV",
        ),
    ]
    calibration_valid = bool(gates) and all(item["passed"] for item in gates)
    calibration_status = (
        "verified_raw_series_ammeter_candidate"
        if calibration_valid
        else "invalid_raw_calibration"
    )
    proposed_current_config = {
        "calibration_valid": calibration_valid,
        "calibration_status": calibration_status,
        "calibration_session": manifest["session_id"],
        "raw_ratio_slope_a_per_ratio": (
            direct_fit.get("slope") if calibration_valid else None
        ),
        "raw_ratio_intercept_a": (
            direct_fit.get("intercept") if calibration_valid else None
        ),
        "raw_ratio_formula": (
            "I_total_A = raw_ratio_slope_a_per_ratio * "
            "((i_adc_raw_sum/i_adc_raw_count) / "
            "(vref_adc_raw_sum/vref_adc_raw_count)) + "
            "raw_ratio_intercept_a"
        ),
        "raw_ratio_reports_total_current": True,
        "uses_auto_zero": False,
        "uses_fixed_idle_addback": False,
        "calibration_quality": {
            "all_gates_passed": calibration_valid,
            "passed_gate_count": sum(item["passed"] for item in gates),
            "total_gate_count": len(gates),
            "eligible_chopped_cycles": len(cycles),
            "distinct_nonzero_levels": distinct_levels,
            "absolute_model_points": len(direct_points),
            "absolute_model_r2": direct_fit.get("r2"),
            "absolute_model_rms_error_a": direct_error_rms,
            "absolute_model_max_error_a": direct_error_max,
            "board_dmm_delta_rms_mv": delta_agreement_rms,
            "source": "fresh physical series ammeter + raw PA0/VREF ADC aggregates",
        },
    }
    warnings = []
    if scope_captures < len(captures):
        warnings.append(
            f"scope waveform evidence exists for {scope_captures}/{len(captures)} "
            "completed captures; scope is diagnostic, not current truth"
        )
    if rejected:
        warnings.append(f"{len(rejected)} ON capture(s) were ineligible; see rejected_cycles")
    if not calibration_valid:
        warnings.append(
            "calibration remains INVALID; diagnostic slopes must not be applied"
        )
    generated_ns = utc_now_ns()
    return {
        "format_version": FORMAT_VERSION,
        "session_id": manifest["session_id"],
        "session_path": str(session),
        "generated_time_ns": generated_ns,
        "generated_utc": utc_iso_from_ns(generated_ns),
        "calibration_valid": calibration_valid,
        "calibration_status": calibration_status,
        "application_authorized": False,
        "applied_to_application": False,
        "result_status": (
            "VALID_CANDIDATE_REQUIRES_REVIEW"
            if calibration_valid
            else "INVALID_INSUFFICIENT_OR_FAILED_QUALITY"
        ),
        "provenance": manifest["provenance_policy"],
        "component_declaration": manifest["circuit_declaration"],
        "counts": {
            "completed_captures": len(captures),
            "latest_manual_readings": len(manual),
            "absolute_model_points": len(direct_points),
            "absolute_model_rejected": len(direct_rejected),
            "eligible_cycles": len(cycles),
            "rejected_on_captures": len(rejected),
            "scope_waveform_captures": scope_captures,
            "distinct_levels": distinct_levels,
        },
        "fit": {
            "application_model": {
                "formula": (
                    "I_total_A = raw_ratio_slope_a_per_ratio * "
                    "(mean_PA0_raw/mean_VREF_raw) + raw_ratio_intercept_a"
                ),
                "raw_ratio_slope_a_per_ratio": direct_fit.get("slope"),
                "raw_ratio_intercept_a": direct_fit.get("intercept"),
                "r2": direct_fit.get("r2"),
                "residuals_a": direct_errors,
                "rms_error_a": direct_error_rms,
                "max_abs_error_a": direct_error_max,
                "current_span_a": direct_current_span,
                "off_is_direct_real_current_point": True,
                "uses_auto_zero": False,
                "uses_fixed_idle_addback": False,
                "note": (
                    "This absolute fit is the application model. It directly "
                    "includes physical OFF current readings; it does not derive "
                    "an idle offset from ON points."
                ),
            },
            "current_x_source": (
                "fresh manual series-ammeter delta: ON minus arithmetic mean of "
                "physically captured adjacent OFF readings"
            ),
            "board_y_source": (
                "untouched PA0 ADC sum/count divided by paired VREF sum/count and "
                "converted with this MCU's streamed factory VREFIN_CAL word"
            ),
            "through_origin_board_mv_per_a": board_slope,
            "through_origin_dmm_mv_per_a": dmm_slope,
            "ordinary_board_fit": free_fit,
            "board_residuals_mv": board_fit.get("residuals_mv"),
            "predicted_current_errors_a": current_errors,
            "current_error_rms_a": current_error_rms,
            "current_error_max_abs_a": current_error_max,
            "board_minus_dmm_delta_rms_mv": delta_agreement_rms,
            "board_dmm_slope_disagreement_fraction": dmm_agreement_fraction,
            "parts_disagreement_fraction": theory_fraction,
            "recommended_mv_per_amp": board_slope if calibration_valid else None,
            "diagnostic_mv_per_amp_not_for_use": (
                board_slope if not calibration_valid else None
            ),
        },
        "direct_model_points": direct_points,
        "direct_model_rejected": direct_rejected,
        "proposed_app_config": {"current_sensor": proposed_current_config},
        "level_repeatability": levels,
        "cycles": cycles,
        "rejected_cycles": rejected,
        "quality_gates": gates,
        "warnings": warnings,
    }


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Raw current-sensor calibration result",
        "",
        f"- Session: `{report['session_id']}`",
        f"- Generated: {report['generated_utc']}",
        f"- Status: **{report['result_status']}**",
        f"- Calibration valid: **{report['calibration_valid']}**",
        "- Applied to application: **False**",
        "",
        "The fit uses only fresh series-ammeter readings tied to capture IDs and "
        "untouched PA0/VREF ADC sums. It does not use `i_dc_mv`, a historical "
        "reference table, interpolation, firmware amps, or simulated data.",
        "",
        "## Fit",
        "",
        "- Application formula: "
        "`I_total_A = raw_ratio_slope_a_per_ratio * "
        "(mean_PA0_raw/mean_VREF_raw) + raw_ratio_intercept_a`",
        "- Raw-ratio slope: "
        f"{report['fit']['application_model']['raw_ratio_slope_a_per_ratio']} A/ratio",
        "- Raw-ratio intercept: "
        f"{report['fit']['application_model']['raw_ratio_intercept_a']} A",
        f"- Absolute-model R^2: {report['fit']['application_model']['r2']}",
        "- Absolute-model current RMS error: "
        f"{report['fit']['application_model']['rms_error_a']} A",
        "- OFF current is a direct real fit point; auto-zero: False; "
        "fixed idle addback: False",
        f"- Board sensitivity: {report['fit']['through_origin_board_mv_per_a']} mV/A",
        f"- DMM sensitivity: {report['fit']['through_origin_dmm_mv_per_a']} mV/A",
        f"- Chopped-delta ordinary-fit R^2: "
        f"{report['fit']['ordinary_board_fit'].get('r2')}",
        f"- Current error RMS: {report['fit']['current_error_rms_a']} A",
        f"- Current error maximum: {report['fit']['current_error_max_abs_a']} A",
        "",
        "## Proposed application keys (not applied)",
        "",
        "```json",
        json.dumps(report["proposed_app_config"], indent=2, sort_keys=True),
        "```",
        "",
        "## Quality gates",
        "",
        "| Gate | Result | Observed | Requirement |",
        "|---|---:|---:|---|",
    ]
    for item in report["quality_gates"]:
        observed = str(item["observed"]).replace("|", "\\|")
        requirement = str(item["requirement"]).replace("|", "\\|")
        lines.append(
            f"| {item['name']} | {'PASS' if item['passed'] else 'FAIL'} | "
            f"{observed} | {requirement} |"
        )
    lines.extend(["", "## Warnings", ""])
    if report["warnings"]:
        lines.extend(f"- {warning}" for warning in report["warnings"])
    else:
        lines.append("- None")
    lines.extend(
        [
            "",
            "## Provenance limitations",
            "",
            "Board and DMM records carry host receipt/query bounds plus the MCU "
            "clock. The human-read series ammeter has no electronic trigger, so "
            "the tool records the operator's stable-window attestation instead of "
            "claiming a nonexistent exact hardware timestamp.",
            "",
        ]
    )
    return "\n".join(lines)


def cmd_finalize(args: argparse.Namespace) -> int:
    session = resolve_session(args.session)
    manifest = session_manifest(session)
    events = load_events(session)
    report = build_final_report(session, manifest, events)
    suffix = (
        dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
        + "_"
        + uuid.uuid4().hex[:8]
    )
    json_path = session / f"finalize_{suffix}.json"
    md_path = session / f"finalize_{suffix}.md"
    write_unique_json(json_path, report)
    with md_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(markdown_report(report))
        fsync_file(handle)
    event = event_base(manifest["session_id"], "finalization_created")
    event.update(
        {
            "calibration_valid": report["calibration_valid"],
            "result_status": report["result_status"],
            "json_file": json_path.name,
            "json_sha256": sha256_file(json_path),
            "markdown_file": md_path.name,
            "markdown_sha256": sha256_file(md_path),
            "applied_to_application": False,
        }
    )
    append_jsonl(session / "events.jsonl", event)
    print(f"Result: {report['result_status']}")
    print(f"Eligible cycles: {report['counts']['eligible_cycles']}")
    print(
        "Absolute raw-ratio model: I_total_A = "
        f"{report['fit']['application_model']['raw_ratio_slope_a_per_ratio']} "
        "* ratio + "
        f"{report['fit']['application_model']['raw_ratio_intercept_a']}"
    )
    print(
        "Chopped diagnostic sensitivity: "
        f"{report['fit']['through_origin_board_mv_per_a']} mV/A"
    )
    print("Quality gates:")
    for item in report["quality_gates"]:
        print(
            f"  [{'PASS' if item['passed'] else 'FAIL'}] {item['name']}: "
            f"{item['observed']} ({item['requirement']})"
        )
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {md_path}")
    if report["calibration_valid"]:
        print(
            "All gates passed. This is a valid candidate, but it was NOT applied; "
            "review the evidence before changing application configuration."
        )
        return 0
    print(
        "Calibration remains INVALID. No application or firmware calibration was changed."
    )
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    new = subparsers.add_parser(
        "new", help="create a fresh immutable-evidence session"
    )
    new.add_argument("--operator", default="", help="operator name")
    new.add_argument(
        "--series-meter",
        default="",
        help="series ammeter make/model/serial and connection (required to pass gates)",
    )
    new.add_argument("--series-meter-range", default="", help="selected meter range")
    new.add_argument(
        "--series-meter-uncertainty",
        type=float,
        default=None,
        help="declared absolute uncertainty in amperes",
    )
    new.add_argument(
        "--shunt-ohm",
        type=float,
        default=DEFAULT_SHUNT_OHM,
        help=f"declared shunt resistance (default {DEFAULT_SHUNT_OHM:g} ohm)",
    )
    new.add_argument(
        "--gain",
        type=float,
        default=DEFAULT_GAIN_V_V,
        help=f"declared INA gain (default {DEFAULT_GAIN_V_V:g} V/V)",
    )
    new.add_argument("--note", default="", help="wiring/fixture notes")
    new.set_defaults(func=cmd_new)

    capture = subparsers.add_parser(
        "capture",
        help="command OFF or an exact integer RPM and capture real instruments",
    )
    capture.add_argument("target", type=parse_target, metavar="OFF|RPM")
    capture.add_argument("--session", help="session directory; default active session")
    capture.add_argument("--port", help="explicit NUCLEO COM port; default ST-LINK detect")
    capture.add_argument("--dmm", default=DEFAULT_DMM, help="VISA DMM resource")
    capture.add_argument("--scope-host", default=DEFAULT_SCOPE_HOST)
    capture.add_argument("--scope-port", type=int, default=DEFAULT_SCOPE_PORT)
    capture.add_argument("--settle", type=float, default=DEFAULT_SETTLE_S)
    capture.add_argument("--duration", type=float, default=DEFAULT_DURATION_S)
    capture.add_argument(
        "--motor-off-after",
        action="store_true",
        help="turn motor off after a successful ON capture; default leaves it running",
    )
    capture.add_argument(
        "--allow-missing-dmm",
        action="store_true",
        help="diagnostic capture only if DMM is unavailable; final gates will fail",
    )
    capture.add_argument(
        "--allow-missing-scope",
        action="store_true",
        help="continue without scope evidence; DMM and raw board remain required",
    )
    capture.set_defaults(func=cmd_capture)

    manual = subparsers.add_parser(
        "manual",
        aliases=["annotate"],
        help="append a physical series-ammeter value to one capture",
    )
    manual.add_argument("capture_id", help="full capture ID or unambiguous prefix")
    manual.add_argument("amps", type=float, help="series ammeter reading in amperes")
    manual.add_argument("--session", help="session directory; default active session")
    manual.add_argument(
        "--stable",
        action="store_true",
        help=(
            "attest that commanded state and meter reading remained stable across "
            "the capture window and this operator reading"
        ),
    )
    manual.add_argument(
        "--observed-at",
        help="operator-observed ISO-8601 time with timezone (optional, not invented)",
    )
    manual.add_argument(
        "--uncertainty", type=float, help="absolute uncertainty in amperes"
    )
    manual.add_argument(
        "--series-meter",
        help="override/complete series-meter identity for this reading",
    )
    manual.add_argument("--note", default="")
    manual.set_defaults(func=cmd_manual)

    status = subparsers.add_parser(
        "status", help="show captures, attached manual readings, and missing work"
    )
    status.add_argument("--session", help="session directory; default active session")
    status.set_defaults(func=cmd_status)

    finalize = subparsers.add_parser(
        "finalize",
        help="fit only eligible real data and write a versioned, unapplied report",
    )
    finalize.add_argument("--session", help="session directory; default active session")
    finalize.set_defaults(func=cmd_finalize)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print(
            "Interrupted. Capture mode attempts motor OFF in its exception handler.",
            file=sys.stderr,
        )
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if os.environ.get("BLDC_RAW_CAL_TRACEBACK") == "1":
            traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
