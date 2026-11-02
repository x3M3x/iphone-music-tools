"""Offline tests: no physical device or USB service is contacted."""
import asyncio
import unittest
from unittest.mock import patch

from pymobiledevice3.usbmux import MuxDevice
from phone_music_pull import DeviceSelectionError, choose_device


class SelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_usb_device(self):
        with patch("pymobiledevice3.usbmux.list_devices", return_value=[MuxDevice(1, "abc", "USB")]):
            self.assertEqual(await choose_device(None), "abc")

    async def test_multiple_devices_require_choice(self):
        devices = [MuxDevice(1, "abc", "USB"), MuxDevice(2, "def", "USB")]
        with patch("pymobiledevice3.usbmux.list_devices", return_value=devices):
            with self.assertRaisesRegex(DeviceSelectionError, "Choose"):
                await choose_device(None)
            self.assertEqual(await choose_device("def"), "def")
            with self.assertRaisesRegex(DeviceSelectionError, "not found"):
                await choose_device("missing")

    async def test_no_usb_device(self):
        with patch("pymobiledevice3.usbmux.list_devices", return_value=[]):
            with self.assertRaisesRegex(DeviceSelectionError, "No iPhone or iPad was detected over USB"):
                await choose_device(None)
        with patch("pymobiledevice3.usbmux.list_devices", return_value=[MuxDevice(1, "abc", "Network")]):
            with self.assertRaisesRegex(DeviceSelectionError, "detected over the network"):
                await choose_device(None)


if __name__ == "__main__":
    unittest.main()
