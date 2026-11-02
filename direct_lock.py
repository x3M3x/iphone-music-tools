"""Acquire the standard phone sync lock for an explicitly selected device.

The actual importer has not been wired to this context manager yet.
"""
from contextlib import asynccontextmanager

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import AfcService, AFC_LOCK_EX, AFC_LOCK_UN
from pymobiledevice3.services.notification_proxy import NotificationProxyService
from pymobiledevice3.usbmux import list_devices

from direct_phone import PhoneReadError


@asynccontextmanager
async def locked_phone(serial):
    devices = [d for d in await list_devices() if d.is_usb and d.serial == serial]
    if len(devices) != 1:
        raise PhoneReadError('The selected phone disconnected; refresh the list.')
    client = await create_using_usbmux(serial=serial, autopair=False, connection_type='USB')
    afc = notification = None
    lock_handle = None
    acquired = False
    notified_start = False
    try:
        if not client.paired:
            raise PhoneReadError('Unlock the phone and trust this computer.')
        afc = AfcService(client)
        await afc.connect()
        notification = NotificationProxyService(client)
        await notification.connect()
        await notification.notify_post('com.apple.itunes-mobdev.syncWillStart')
        lock_handle = await afc.fopen('/com.apple.itunes.lock_sync', 'r+')
        if not lock_handle:
            raise PhoneReadError('The phone sync lock could not be opened.')
        await notification.notify_post('com.apple.itunes-mobdev.syncLockRequest')
        try:
            await afc.lock(lock_handle, AFC_LOCK_EX)
        except Exception as exc:
            raise PhoneReadError('The phone is busy with another sync or backup.') from exc
        acquired = True
        await notification.notify_post('com.apple.itunes-mobdev.syncDidStart')
        notified_start = True
        yield client, afc
    finally:
        try:
            if afc and lock_handle:
                try:
                    if acquired:
                        await afc.lock(lock_handle, AFC_LOCK_UN)
                finally:
                    await afc.fclose(lock_handle)
        finally:
            try:
                if notification:
                    try:
                        if notified_start:
                            await notification.notify_post('com.apple.itunes-mobdev.syncDidFinish')
                    finally:
                        await notification.close()
            finally:
                try:
                    if afc:
                        await afc.close()
                finally:
                    await client.close()
