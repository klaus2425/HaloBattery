"""VXE R1 Pro Max battery reporting over its Compx/Nordic HID interface.

The public mouse_tray driver identifies the wireless and wired PIDs and documents
the shared 17-byte report-8 exchange on HID collection 0xFF02:0x0002. The report
returns battery percentage at byte 6 and a wired flag at byte 7. This provider
keeps the receiver and cable under one tray icon. The protocol has not yet been
confirmed against this project's own hardware.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

import hid

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log

VXE_VID = 0x3554
WIRELESS_PID = 0xF58A
WIRED_PID = 0xF58C
PIDS = (WIRELESS_PID, WIRED_PID)

BATTERY_PAGE = 0xFF02
BATTERY_USAGE = 0x0002
REPORT_ID = 0x08
BATTERY_COMMAND = 0x04
REQUEST = bytes((REPORT_ID, BATTERY_COMMAND)) + bytes(14) + bytes((0x49,))

READ_DELAY = 0.1
READ_TIMEOUT = 0.25
READ_GAP = 0.02
ASLEEP_KEEP = 300


def parse_status(resp) -> Optional[Tuple[int, bool]]:
    """Return (percent, wired) for a valid report-8 battery reply."""
    if not resp or len(resp) < 17:
        return None
    data = bytes(resp)
    if data[0] != REPORT_ID or data[1] != BATTERY_COMMAND:
        return None
    level, wired = data[6], data[7]
    if level > 100 or wired not in (0, 1):
        return None
    return level, bool(wired)


class VxeProvider(Provider):
    name = "vxe"

    def __init__(self):
        self._diag: List[str] = []
        self._last: Optional[Tuple[int, bool, float]] = None

    def _read(self, path: bytes) -> Optional[Tuple[int, bool]]:
        dev = hid.device()
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"    open: {e}")
            return None
        try:
            try:
                dev.set_nonblocking(True)
            except Exception:  # pragma: no cover - hidapi platform variation
                pass
            try:
                dev.write(REQUEST)
            except (OSError, ValueError) as e:
                self._diag.append(f"    write: {e}")
                return None

            time.sleep(READ_DELAY)
            deadline = time.monotonic() + READ_TIMEOUT
            while time.monotonic() < deadline:
                try:
                    resp = dev.read(17)
                except (OSError, ValueError):
                    resp = None
                status = parse_status(resp)
                if status is not None:
                    self._diag.append(f"    reply: {hexdump(resp, 17)}")
                    return status
                time.sleep(READ_GAP)
            self._diag.append("    no reply to report 0x08 command 0x04")
            return None
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def poll(self) -> List[DeviceStatus]:
        self._diag = []
        try:
            infos = hidlist.enumerate(VXE_VID)
        except Exception as e:  # pragma: no cover
            log.warning("hid.enumerate(vxe): %s", e)
            return []
        if not infos:
            return []

        groups: Dict[int, List[dict]] = {}
        for info in infos:
            pid = info.get("product_id")
            if pid in PIDS:
                groups.setdefault(pid, []).append(info)

        readings: List[Tuple[int, bool, int]] = []  # level, wired, pid
        for pid in (WIRED_PID, WIRELESS_PID):
            ifaces = groups.get(pid, [])
            if not ifaces:
                continue
            product = (ifaces[0].get("product_string") or "").strip()
            self._diag.append(f"[VXE R1 Pro Max] pid={pid:04x} product='{product}'")
            cols = [d for d in ifaces
                    if d.get("usage_page") == BATTERY_PAGE
                    and d.get("usage") == BATTERY_USAGE]
            for iface in cols:
                self._diag.append(f"  iface={iface.get('interface_number')} usage="
                                  f"{BATTERY_PAGE:04x}:{BATTERY_USAGE:04x}")
                status = self._read(iface["path"])
                if status is not None:
                    readings.append((status[0], status[1], pid))
                    break

        if readings:
            # A direct cable report wins over the receiver if both are connected.
            readings.sort(key=lambda reading: not reading[1])
            level, wired, pid = readings[0]
            charging = wired and level < 100
            self._diag.append(f"  -> using pid={pid:04x}: {level}%"
                              f"{' (wired)' if wired else ''}")
            self._last = (level, charging, time.monotonic())
            return [DeviceStatus("vxe:r1-pro-max", "VXE R1 Pro Max", level,
                                 charging, True, "vxe", kind="mouse")]

        if self._last and time.monotonic() - self._last[2] < ASLEEP_KEEP:
            return [DeviceStatus("vxe:r1-pro-max", "VXE R1 Pro Max", self._last[0],
                                 self._last[1], False, "vxe", kind="mouse")]
        return []

    def diagnostics(self) -> List[str]:
        return list(self._diag)
