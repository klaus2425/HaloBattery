"""Steam Controller (2026 / Triton), over USB, Bluetooth LE or Steam Puck.

Read-only input reports: 0x43 contains charge state, percent and six uint16
telemetry fields. Layout and charge states follow SDL's Steam Triton driver:
https://github.com/libsdl-org/SDL/blob/main/src/joystick/hidapi/steam/controller_structs.h
https://github.com/libsdl-org/SDL/blob/main/src/joystick/hidapi/SDL_hidapi_steam_triton.c

Battery reports arrive roughly every 3.5 seconds. Open the vendor collections
together and share one bounded read window, including empty receiver slots.
No feature/output reports are sent: Steam's input configuration is untouched.
"""
from __future__ import annotations

import time
from typing import List, Optional, Tuple

try:
    import hid
except ImportError:              # pragma: no cover
    hid = None

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log

VALVE_VID = 0x28DE
KNOWN = {0x1302: "USB", 0x1303: "Bluetooth LE",
         0x1304: "Steam Puck", 0x1305: "Steam Machine receiver"}
RECEIVERS = {0x1304, 0x1305}
READ_SECONDS = 4.5


def parse_battery(data) -> Optional[Tuple[int, bool]]:
    """A complete Triton battery report -> (percent, actively charging)."""
    if len(data) < 15 or data[0] != 0x43:
        return None
    state, level = data[1:3]
    if state not in range(5) or not 0 <= level <= 100:
        return None
    # Reset (0), source validation (3), and charge complete (4) are not charging.
    return level, state == 2


class SteamControllerProvider(Provider):
    name = "steam"

    def __init__(self):
        self._diag: List[str] = []
        self.pending = False

    def poll(self) -> List[DeviceStatus]:
        self._diag = []
        self.pending = False
        if hid is None:
            return []
        try:
            infos = hidlist.enumerate(VALVE_VID)
        except Exception as e:  # pragma: no cover
            log.warning("hid.enumerate(steam): %s", e)
            return []

        endpoints = []
        seen = set()
        try:
            for info in infos:
                pid = info["product_id"]
                path = info["path"]
                if pid not in KNOWN or info.get("usage_page") != 0xFF00 or path in seen:
                    continue
                # A puck has one controller per interface 2..5. Its other
                # collections are receiver management, mouse and keyboard.
                if pid in RECEIVERS and info.get("interface_number") not in range(2, 6):
                    continue
                seen.add(path)
                self._diag.append(f"[Steam] {KNOWN[pid]} pid={pid:04x} "
                                  f"iface={info.get('interface_number')}")
                dev = hid.device()
                try:
                    dev.open_path(path)
                    dev.set_nonblocking(True)
                except (OSError, ValueError) as e:
                    self._diag.append(f"  open/setup: {e}")
                    self._close(dev)
                    continue
                endpoints.append(dict(dev=dev, path=path, pid=pid, reading=None,
                                      connected=pid not in RECEIVERS, failed=False))

            deadline = time.monotonic() + READ_SECONDS
            while endpoints and time.monotonic() < deadline:
                active = False
                for endpoint in endpoints:
                    if endpoint["failed"]:
                        continue
                    active = True
                    # Drain a bounded batch so input traffic cannot starve other
                    # controllers or postpone the deadline indefinitely.
                    for _ in range(32):
                        try:
                            data = endpoint["dev"].read(64)
                        except (OSError, ValueError) as e:
                            self._diag.append(f"  read: {e}")
                            endpoint["failed"] = True
                            endpoint["connected"] = False
                            endpoint["reading"] = None
                            break
                        if not data:
                            break
                        self._consume(endpoint, data)
                if not active:
                    break
                time.sleep(0.002)
        finally:
            for endpoint in endpoints:
                self._close(endpoint["dev"])

        out = []
        for endpoint in endpoints:
            if not endpoint["connected"] or endpoint["failed"]:
                continue
            path = endpoint["path"]
            path_text = path.decode("utf-8", "replace") if isinstance(path, bytes) else str(path)
            # A receiver serial identifies the puck, not the controller. Keep
            # the interface path so its four controller slots never collapse.
            key = f"steam:{path_text}"
            reading = endpoint["reading"]
            level, charging = reading if reading is not None else (None, False)
            approx = ""
            if reading is None:
                self.pending = True
                approx = "connected, battery level not reported yet"
            out.append(DeviceStatus(key, "Steam Controller", level, charging,
                                    True, self.name, approx, kind="gamepad",
                                    via="bluetooth" if endpoint["pid"] == 0x1303 else ""))
        return out

    def _consume(self, endpoint: dict, data) -> None:
        reading = parse_battery(data)
        if reading is not None:
            endpoint["connected"] = True
            endpoint["reading"] = reading
            self._diag.append(f"  battery: {hexdump(data, 15)} -> {reading[0]}%"
                              f"{' charging' if reading[1] else ''}")
        elif data[0] in (0x46, 0x79) and len(data) >= 2:
            if data[1] == 1:
                endpoint["connected"] = False
                endpoint["reading"] = None
                self._diag.append("  controller disconnected")
            elif data[1] == 2:
                endpoint["connected"] = True
        elif data[0] in (0x42, 0x45, 0x47) and len(data) >= 54:
            endpoint["connected"] = True

    @staticmethod
    def _close(dev) -> None:
        try:
            dev.close()
        except Exception:
            pass

    def diagnostics(self) -> List[str]:
        return list(self._diag)
