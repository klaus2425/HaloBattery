import unittest
from unittest.mock import patch

from providers import razer
from providers.razer import RazerProvider, STATUS_FAILURE, STATUS_OK, STATUS_TIMEOUT


class RazerWakeTests(unittest.TestCase):
    def test_dock_retries_cached_interface_after_mouse_wakes(self):
        provider = RazerProvider()
        interface = {
            "path": b"mouse-dock",
            "product_id": 0x00A4,
            "serial_number": "",
            "product_string": "Razer Mouse Dock Pro",
            "usage_page": 0x0001,
            "interface_number": 0,
        }

        with patch.object(razer.hidlist, "enumerate", return_value=[interface]):
            with patch.object(provider, "_read") as read:
                read.return_value = (STATUS_OK, 40, False)
                awake = provider.poll()
                self.assertEqual(len(awake), 1)
                self.assertTrue(awake[0].online)

                read.return_value = (None, None, None)
                asleep = provider.poll()
                self.assertEqual(len(asleep), 1)
                self.assertFalse(asleep[0].online)

                read.return_value = (STATUS_OK, 42, False)
                resumed = provider.poll()
                self.assertEqual(len(resumed), 1)
                self.assertTrue(resumed[0].online)
                self.assertEqual(resumed[0].level, 42)

    def test_dock_reopens_new_interface_after_receiver_reenumerates(self):
        provider = RazerProvider()
        before_sleep = {
            "path": b"mouse-dock-before-sleep",
            "product_id": 0x00A4,
            "serial_number": "",
            "product_string": "Razer Mouse Dock Pro",
            "usage_page": 0x0001,
            "interface_number": 0,
        }
        after_sleep = {**before_sleep, "path": b"mouse-dock-after-sleep"}

        with patch.object(razer.hidlist, "enumerate",
                          side_effect=[[before_sleep], [after_sleep], [after_sleep]]):
            with patch.object(provider, "_read", side_effect=[
                (STATUS_OK, 40, False),
                (STATUS_TIMEOUT, None, None),
                (STATUS_OK, 42, False),
            ]) as read:
                awake = provider.poll()
                self.assertTrue(awake[0].online)

                asleep = provider.poll()
                self.assertFalse(asleep[0].online)
                self.assertEqual(read.call_args_list[-1].args[0], after_sleep["path"])

                resumed = provider.poll()
                self.assertEqual(len(resumed), 1)
                self.assertTrue(resumed[0].online)
                self.assertEqual(resumed[0].level, 42)

    def test_dock_keeps_preferred_transaction_id_after_sleep_failure(self):
        provider = RazerProvider()
        interface = {
            "path": b"mouse-dock",
            "product_id": 0x00A4,
            "serial_number": "",
            "product_string": "Razer Mouse Dock Pro",
            "usage_page": 0x0001,
            "interface_number": 0,
        }
        phase = {"sleeping": False}

        def read(path, tid):
            if tid == 0x1F and not phase["sleeping"]:
                return STATUS_OK, 91, False
            if phase["sleeping"] and tid == 0x1F:
                return STATUS_FAILURE, None, None
            return STATUS_TIMEOUT, None, None

        with patch.object(razer.hidlist, "enumerate", return_value=[interface]):
            with patch.object(provider, "_read", side_effect=read) as read_mock:
                awake = provider.poll()
                self.assertTrue(awake[0].online)

                phase["sleeping"] = True
                asleep = provider.poll()
                self.assertFalse(asleep[0].online)

                phase["sleeping"] = False
                resumed = provider.poll()
                self.assertTrue(resumed[0].online)
                self.assertEqual(resumed[0].level, 91)
                self.assertEqual([call.args[1] for call in read_mock.call_args_list],
                                 [0x1F, 0x1F, 0x1F])

    def test_dock_retries_preferred_id_after_fallback_timeout(self):
        provider = RazerProvider()
        interface = {
            "path": b"mouse-dock",
            "product_id": 0x00A4,
            "serial_number": "",
            "product_string": "Razer Mouse Dock Pro",
            "usage_page": 0x0001,
            "interface_number": 0,
        }
        phase = {"sleeping": True}

        def read(path, tid):
            if phase["sleeping"] and tid == 0x1F:
                return STATUS_FAILURE, None, None
            if tid != 0x1F:
                return STATUS_TIMEOUT, None, None
            return STATUS_OK, 92, False

        with patch.object(razer.hidlist, "enumerate", return_value=[interface]):
            with patch.object(provider, "_read", side_effect=read) as read_mock:
                provider.poll()
                phase["sleeping"] = False
                resumed = provider.poll()

        self.assertEqual(len(resumed), 1)
        self.assertTrue(resumed[0].online)
        self.assertEqual(resumed[0].level, 92)
        self.assertEqual([call.args[1] for call in read_mock.call_args_list],
                         [0x1F, 0x3F, 0x1F])

