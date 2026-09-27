import unittest
from unittest.mock import patch

from providers import razer
from providers.razer import RazerProvider, STATUS_OK


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

