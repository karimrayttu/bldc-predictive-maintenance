"""Session manager: turns validated frames into an organized, labelled, uniformly
recorded dataset.
"""

import os
import csv
import json
import shutil
import datetime

from .schema import ALL_COLUMNS, STREAM_COLUMNS, DERIVED_COLUMNS, FUTURE_COLUMNS

# Fallback field order if no taxonomy field list is supplied.
DEFAULT_META_FIELDS = [
    "stage", "label", "condition", "severity", "speed_step", "speed_rpm_nominal",
    "orientation", "mount", "load", "fixture", "warmup_min", "case_temp_C",
    "ambient_temp_C", "supply_V", "supply_A", "operator", "notes",
]

FLUSH_EVERY = 10      # flush CSV to disk every N rows (~1 s @ 10 Hz) so a crash loses < 1 s


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


class SessionManager:
    def __init__(self, data_dir, meta_fields=None, subroot=""):
        self.root = data_dir
        self.meta_fields = list(meta_fields) if meta_fields else list(DEFAULT_META_FIELDS)
        self._fixture_state = "mounted"
        self._valid_for_classifier = "true"
        self.active = False
        self.set_subroot(subroot)
        self.session_id = None
        self._csv_file = None
        self._csv_writer = None
        self._meta = None
        self._folder = None
        self._n_rows = 0

    def set_subroot(self, subroot):
        """Point all output at <root>/<subroot>/ so commissioning vs mounted data never mix."""
        if self.active:
            raise RuntimeError("cannot switch data root while recording")
        self.subroot = subroot or ""
        data_dir = os.path.join(self.root, self.subroot) if self.subroot else self.root
        self.data_dir = data_dir
        # Only the raw per-run source of truth is kept on disk; the 2 fixture workbooks are
        # the deliverable (built by consolidate_fixture). No RUN_LOG / combined_data / extra dirs.
        self.sessions_dir = os.path.join(data_dir, "sessions")
        self.baselines_dir = os.path.join(data_dir, "baselines")   # path only (created on demand)
        os.makedirs(self.sessions_dir, exist_ok=True)

    def next_session_id(self):
        n = 0
        for _root, dirs, _files in os.walk(self.sessions_dir):   # recurse: now nested by cond/rpm
            for name in dirs:
                if name.startswith("S") and name[1:4].isdigit():
                    n = max(n, int(name[1:4]))
        return f"S{n + 1:03d}"

    def start(self, meta):
        if self.active:
            raise RuntimeError("a session is already recording")
        now = _utc_now()
        self.session_id = self.next_session_id()
        label = (meta.get("label") or "").strip().replace(" ", "-")
        if not label:
            cond = (meta.get("condition") or "run").strip().replace(" ", "-")
            label = f"{cond}_{meta.get('speed_rpm_nominal', '')}"
        # fixture tagging: so unmounted data is obvious and never mixes with mounted
        self._fixture_state = meta.get("fixture_state", "mounted")
        self._valid_for_classifier = meta.get("valid_for_classifier", "true")
        stamp = now.strftime("%Y%m%d-%H%M%S")          # date + time to the second
        # organise by condition (healthy / each fault) then by rpm; the path carries the
        # condition+rpm, so the folder name stays short:  sessions/<cond>/<NNNN>rpm/S###_<stamp>/
        cond = (meta.get("condition") or "run").strip().replace(" ", "-")
        rpm_raw = str(meta.get("speed_rpm_nominal", "")).strip()
        try:
            rpm_dir = f"{int(float(rpm_raw)):04d}rpm"
        except (TypeError, ValueError):
            rpm_dir = "unknown_rpm"
        folder_name = f"{self.session_id}_{stamp}"
        self._folder = os.path.join(self.sessions_dir, cond, rpm_dir, folder_name)
        os.makedirs(self._folder, exist_ok=True)
        # per-row labels so every line of data is self-describing (easy to combine)
        self._row_meta = {
            "condition": meta.get("condition", ""),
            "severity": meta.get("severity", ""),
            "speed_setpoint_rpm": meta.get("speed_rpm_nominal", ""),
            "label": label,
        }

        self._meta = {
            "session_id": self.session_id,
            "created_utc": now.isoformat(),
            "schema_columns": ALL_COLUMNS,
            "stream_columns": STREAM_COLUMNS,
            **meta,
        }
        # IN-PROGRESS marker (2026-07-30): the start-time meta used to claim
        # valid_for_classifier="true" before a single frame was recorded, so a crash
        # mid-run left a fragment the scheduler counted as complete and the workbook
        # consolidated as a full run. On disk the run is invalid until stop() closes
        # it; the in-memory meta keeps the caller's value for stop().
        _disk = dict(self._meta)
        _disk["status"] = "in_progress"
        _disk["valid_for_classifier"] = "false"
        _mp0 = os.path.join(self._folder, "meta.json")
        with open(_mp0 + ".tmp", "w") as f:
            json.dump(_disk, f, indent=2)
        os.replace(_mp0 + ".tmp", _mp0)

        self._csv_file = open(os.path.join(self._folder, "data.csv"), "w", newline="")
        self._csv_writer = csv.DictWriter(self._csv_file, fieldnames=ALL_COLUMNS,
                                          extrasaction="ignore")
        self._csv_writer.writeheader()
        self._n_rows = 0
        self.active = True
        return self.session_id, folder_name

    def write_frame(self, frame):
        if not self.active:
            return
        row = {c: "" for c in ALL_COLUMNS}
        row["iso_time"] = _utc_now().isoformat()
        row["session_id"] = self.session_id
        row["fixture_state"] = self._fixture_state
        row["valid_for_classifier"] = self._valid_for_classifier
        for k, v in getattr(self, "_row_meta", {}).items():
            row[k] = v
        for c in STREAM_COLUMNS + DERIVED_COLUMNS + FUTURE_COLUMNS:
            if c in frame:
                row[c] = frame[c]
        self._csv_writer.writerow(row)
        self._n_rows += 1
        if self._n_rows % FLUSH_EVERY == 0:
            self._csv_file.flush()

    def discard(self):
        """Abort the current run: close and DELETE its folder, write NO manifest row."""
        if not self.active:
            return None
        try:
            self._csv_file.close()
        except Exception:
            pass
        folder = self._folder
        self.active = False
        self._csv_file = self._csv_writer = self._meta = None
        shutil.rmtree(folder, ignore_errors=True)
        return folder

    def stop(self, stats, extra=None):
        if not self.active:
            return None
        self._csv_file.close()
        m = self._meta
        if extra:                              # merge verified fields (data_source, authenticity, ...)
            m.update(extra)
        now = _utc_now()
        good_pct = round(stats.good_pct, 1) if stats else ""
        validated = "PASS" if (stats and stats.good_pct > 99 and stats.bad_shape == 0
                               and stats.bad_range == 0) else "REVIEW"

        m["closed_utc"] = now.isoformat()
        m["status"] = "closed"
        m["n_rows"] = self._n_rows
        m["frame_stats"] = stats.as_text() if stats else ""
        m["good_pct"] = good_pct          # was computed and thrown away; the numeric
                                          # integrity of a run belongs in its metadata
        m["validated"] = validated
        # ATOMIC (2026-07-30): a crash mid-write here would leave a truncated
        # meta.json that count_official silently skips; the run would vanish from
        # the scheduler while its data sat on disk.
        _mp = os.path.join(self._folder, "meta.json")
        with open(_mp + ".tmp", "w") as f:
            json.dump(m, f, indent=2)
        os.replace(_mp + ".tmp", _mp)

        # No RUN_LOG / combined_data: the per-run data.csv+meta.json ARE the source of truth;
        # the app rebuilds the fixture workbook (consolidate_fixture) after each official run.
        result = {"session_id": self.session_id, "folder": self._folder,
                  "n_rows": self._n_rows, "validated": validated}
        self.active = False
        self._csv_file = self._csv_writer = self._meta = None
        return result
