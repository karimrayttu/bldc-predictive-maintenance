"""Bench instrument addresses.

    python -c "import pyvisa; print(pyvisa.ResourceManager().list_resources())"
"""

from __future__ import annotations

import os

# Keysight 34460A over USB-TMC. The serial is the MY######## field.
DMM_RESOURCE = os.environ.get(
    "BLDC_DMM", "USB0::0x2A8D::0x1701::MY########::0::INSTR")

# Keysight MSO-X 3034T over raw-socket SCPI. A direct cable gives a link-local
# address, which encodes the scope's MAC, so prefer a routed IPv4 address.
SCOPE_HOST = os.environ.get("BLDC_SCOPE_HOST", "192.168.1.50")
SCOPE_PORT = int(os.environ.get("BLDC_SCOPE_PORT", "5025"))
SCOPE_ADDR = (SCOPE_HOST, SCOPE_PORT)


def configured() -> bool:
    """True when both instruments have been pointed at real hardware."""
    return "########" not in DMM_RESOURCE and SCOPE_HOST != "192.168.1.50"


def require(what: str = "instruments") -> None:
    """Fail with instructions rather than a confusing VISA timeout."""
    if not configured():
        raise SystemExit(
            f"{what} not configured. Set BLDC_DMM and BLDC_SCOPE_HOST for this "
            f"bench; see bldc_phm/instruments.py.")
