import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from direct_lock import locked_phone
from direct_phone import PhoneReadError
from pymobiledevice3.services.afc import AFC_LOCK_EX, AFC_LOCK_UN


class LockTests(unittest.IsolatedAsyncioTestCase):
    async def test_exclusive_lock_and_finish_are_released_on_error(self):
        client = SimpleNamespace(paired=True, close=AsyncMock())
        afc = SimpleNamespace(connect=AsyncMock(), close=AsyncMock(),
                              fopen=AsyncMock(return_value=7), fclose=AsyncMock(), lock=AsyncMock())
        notify = SimpleNamespace(connect=AsyncMock(), close=AsyncMock(), notify_post=AsyncMock())
        with patch('direct_lock.list_devices', AsyncMock(return_value=[SimpleNamespace(is_usb=True,serial='phone')])), \
             patch('direct_lock.create_using_usbmux', AsyncMock(return_value=client)), \
             patch('direct_lock.AfcService', return_value=afc), \
             patch('direct_lock.NotificationProxyService', return_value=notify):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                async with locked_phone('phone'):
                    raise RuntimeError('interrupted')
        self.assertEqual([c.args for c in afc.lock.await_args_list], [(7,AFC_LOCK_EX),(7,AFC_LOCK_UN)])
        self.assertEqual(notify.notify_post.await_args_list[-1].args,
                         ('com.apple.itunes-mobdev.syncDidFinish',))
        afc.fclose.assert_awaited_once_with(7)
        client.close.assert_awaited_once()

    async def test_wrong_device_fails_before_connection(self):
        with patch('direct_lock.list_devices', AsyncMock(return_value=[])), \
             patch('direct_lock.create_using_usbmux', AsyncMock()) as connect:
            with self.assertRaises(PhoneReadError):
                async with locked_phone('phone'):
                    pass
            connect.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
