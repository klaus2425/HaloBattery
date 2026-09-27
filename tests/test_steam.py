import os
import logging
from pathlib import Path
import runpy
import sys
import unittest
from unittest.mock import patch

from providers import steam


def battery(level=73, state=1):
    # Report ID, charge state, percentage, then six little-endian uint16 fields.
    return [0x43, state, level, 0xA0, 0x0F, 0xA0, 0x0F, 0, 0, 0, 0, 0, 0, 0, 0]


def interface(path=b"puck-slot-2", pid=0x1304, number=2, usage_page=0xFF00):
    return dict(path=path, vendor_id=0x28DE, product_id=pid,
                interface_number=number, usage_page=usage_page,
                serial_number="shared-receiver-serial")


class FakeDevice:
    def __init__(self, reports=(), error=None):
        self.reports = list(reports)
        self.error = error
        self.closed = False

    def open_path(self, path):
        if self.error == "open":
            raise OSError("busy")

    def set_nonblocking(self, value):
        if self.error == "setup":
            raise OSError("cannot set nonblocking")

    def read(self, size):
        if self.error == "read":
            raise OSError("unplugged")
        return self.reports.pop(0) if self.reports else []

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        self.now += 0.001
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class SteamTests(unittest.TestCase):
    def poll(self, infos, devices):
        provider = steam.SteamControllerProvider()
        with patch.object(steam.hidlist, "enumerate", return_value=infos), \
                patch.object(steam, "hid") as hid, patch.object(steam, "time", Clock()):
            hid.device.side_effect = devices
            result = provider.poll()
        return provider, result

    def test_battery_states_and_boundaries(self):
        for level in (0, 1, 73, 100):
            for state in range(5):
                with self.subTest(level=level, state=state):
                    self.assertEqual(steam.parse_battery(battery(level, state)),
                                     (level, state == 2))

    def test_live_puck_capture(self):
        report = bytes.fromhex("43 02 5e 3c 10 40 10 c0 12 18 01 8f 01 39 5f")
        self.assertEqual(steam.parse_battery(report), (94, True))

    def test_malformed_reports_are_ignored(self):
        for report in ([], [0x43, 2, 50], battery()[:-1], [0x42] + battery()[1:],
                       battery(101), battery(255), battery(state=255)):
            with self.subTest(report=report):
                self.assertIsNone(steam.parse_battery(report))

    def test_usb_bluetooth_and_puck(self):
        for pid in (0x1302, 0x1303, 0x1304, 0x1305):
            with self.subTest(pid=pid):
                dev = FakeDevice([[0x42] + [0] * 53, battery(42, 2)])
                provider, result = self.poll([interface(pid=pid)], [dev])
                self.assertEqual(len(result), 1)
                st = result[0]
                self.assertEqual((st.level, st.charging, st.kind, st.source),
                                 (42, True, "gamepad", "steam"))
                self.assertEqual(st.via, "bluetooth" if pid == 0x1303 else "")
                self.assertFalse(provider.pending)
                self.assertTrue(dev.closed)

    def test_empty_puck_has_no_controller(self):
        provider, result = self.poll([interface()], [FakeDevice()])
        self.assertEqual(result, [])
        self.assertFalse(provider.pending)

    def test_multiple_slots_with_same_serial_stay_separate(self):
        infos = [interface(b"slot2"), interface(b"slot3", number=3)]
        _, result = self.poll(infos, [FakeDevice([battery(20)]), FakeDevice([battery(80)])])
        self.assertEqual([s.level for s in result], [20, 80])
        self.assertEqual(len({s.key for s in result}), 2)

    def test_unrelated_devices_and_interfaces_are_not_opened(self):
        infos = [interface(pid=0x1142), interface(usage_page=1), interface(number=0)]
        _, result = self.poll(infos, [])
        self.assertEqual(result, [])

    def test_present_controller_without_battery_is_pending(self):
        for info, reports in ((interface(pid=0x1302), []),
                              (interface(), [[0x46, 2]])):
            provider, result = self.poll([info], [FakeDevice(reports)])
            self.assertTrue(provider.pending)
            self.assertEqual(len(result), 1)
            self.assertIsNone(result[0].level)
            self.assertIn("not reported", result[0].approx)

    def test_disconnect_clears_reading(self):
        for report_id in (0x46, 0x79):
            provider, result = self.poll([interface()],
                                         [FakeDevice([battery(), [report_id, 1]])])
            self.assertEqual(result, [])
            self.assertFalse(provider.pending)

    def test_reconnect_does_not_reuse_old_battery(self):
        provider, result = self.poll([interface()],
                                     [FakeDevice([battery(), [0x46, 1], [0x46, 2]])])
        self.assertTrue(provider.pending)
        self.assertIsNone(result[0].level)

    def test_io_failures_close_handles_and_do_not_hide_other_devices(self):
        for error in ("open", "setup", "read"):
            with self.subTest(error=error):
                bad, good = FakeDevice(error=error), FakeDevice([battery()])
                _, result = self.poll([interface(b"bad"), interface(b"good")], [bad, good])
                self.assertEqual([s.level for s in result], [73])
                self.assertTrue(bad.closed)
                self.assertTrue(good.closed)

    def test_all_slots_share_one_timeout(self):
        clock = Clock()
        infos = [interface(str(n).encode(), number=n) for n in range(2, 6)]
        devices = [FakeDevice() for _ in infos]
        with patch.object(steam.hidlist, "enumerate", return_value=infos), \
                patch.object(steam, "hid") as hid, patch.object(steam, "time", clock):
            hid.device.side_effect = devices
            self.assertEqual(steam.SteamControllerProvider().poll(), [])
        self.assertLess(clock.now, 5)
        self.assertTrue(all(d.closed for d in devices))

    def test_missing_hid_is_optional(self):
        with patch.object(steam, "hid", None):
            self.assertEqual(steam.SteamControllerProvider().poll(), [])

    def test_duplicate_enumeration_does_not_open_same_path_twice(self):
        info = interface()
        _, result = self.poll([info, info], [FakeDevice([battery()])])
        self.assertEqual(len(result), 1)

    def test_continuous_input_cannot_prevent_timeout(self):
        class BusyDevice(FakeDevice):
            def read(self, size):
                return [0x42] + [0] * 53

        provider, result = self.poll([interface()], [BusyDevice()])
        self.assertTrue(provider.pending)
        self.assertIsNone(result[0].level)


@unittest.skipUnless(sys.platform == "win32", "Windows tray integration")
class TrayIntegrationTests(unittest.TestCase):
    def test_registered_provider_reaches_tray_formatting(self):
        appdata = str(Path(__file__).parent / "unused-appdata")
        with patch("os.makedirs"), \
                patch("logging.handlers.RotatingFileHandler", return_value=logging.NullHandler()):
            with patch.dict(os.environ, {"APPDATA": appdata}):
                app_module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "halo_battery.pyw"))
            try:
                app_class = app_module["App"]
                with patch.object(app_class, "compute_light", return_value=False):
                    app = app_class()
                self.assertTrue(any(isinstance(p, steam.SteamControllerProvider) for p in app.providers))
                app.providers = [p for p in app.providers if isinstance(p, steam.SteamControllerProvider)]
                app.cfg["bluetooth"] = True
                from providers.base import DeviceStatus
                app.bt_cache = [DeviceStatus("bt:steam", "Steam Controller", 90, source="bluetooth")]
                with patch.object(steam.hidlist, "enumerate", return_value=[interface()]), \
                        patch.object(steam, "hid") as hid, patch.object(steam, "time", Clock()):
                    hid.device.return_value = FakeDevice([battery(94, 2)])
                    statuses = app.poll_once()
                self.assertEqual(len(statuses), 1)
                self.assertEqual(app_module["badge_for"](statuses[0]), "gamepad")
                self.assertEqual(app_module["describe"](statuses[0]), "Steam Controller: 94%, charging")
            finally:
                handler = app_module["_fh"]
                app_module["log"].removeHandler(handler)
                handler.close()


if __name__ == "__main__":
    unittest.main()
