"""Read a paired iPhone's music catalog directly on Windows, without iTunes.

This module never starts a sync or writes to the phone.
"""
import asyncio
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from pymobiledevice3.exceptions import AfcFileNotFoundError
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import AfcService
from pymobiledevice3.usbmux import list_devices
from atc_protocol import library_identity, ProtocolError

DATABASE = '/iTunes_Control/iTunes/MediaLibrary.sqlitedb'
MAX_BYTES = 32 * 1024 * 1024


class PhoneReadError(Exception):
    pass


@dataclass(frozen=True)
class Device:
    serial: str
    name: str
    model: str
    ios: str


@dataclass(frozen=True)
class Playlist:
    id: int
    name: str
    members: tuple[int, ...]
    created: int


@dataclass(frozen=True)
class Catalog:
    database_id: int
    library_id: int
    anchor: int
    playlists: tuple[Playlist, ...]
    song_count: int
    song_dates: tuple[tuple[int, int], ...]
    all_playlist_names: tuple[tuple[int | None, str], ...] = ()


async def devices():
    result = []
    for entry in await list_devices():
        if not entry.is_usb:
            continue
        client = await create_using_usbmux(serial=entry.serial, autopair=False, connection_type='USB')
        try:
            if not client.paired:
                continue
            result.append(Device(entry.serial, str(await client.get_value(key='DeviceName')),
                                 str(await client.get_value(key='ProductType')),
                                 str(await client.get_value(key='ProductVersion'))))
        finally:
            await client.close()
    return result


async def _stat_optional(afc, path):
    try:
        info = await afc.stat(path)
    except AfcFileNotFoundError:
        return None
    return tuple((key, info.get(key)) for key in ('st_size', 'st_ifmt', 'st_mtime', 'st_birthtime'))


async def _capture_once(afc):
    names = (DATABASE, DATABASE + '-wal', DATABASE + '-shm')
    before = tuple([await _stat_optional(afc, name) for name in names])
    if before[0] is None:
        raise PhoneReadError('The phone music database was not found.')
    contents = []
    for name, stats in zip(names[:2], before[:2]):
        if stats is None:
            contents.append(None)
            continue
        info = dict(stats)
        size = info['st_size']
        if info['st_ifmt'] != 'S_IFREG' or not isinstance(size, int) or size < 0 or size > MAX_BYTES:
            raise PhoneReadError('The music database is too large or is not a regular file.')
        # Bounded reads do not trust a file size that may change after stat.
        handle = await afc.fopen(name)
        try:
            data = await afc.fread(handle, size + 1)
        finally:
            await afc.fclose(handle)
        if len(data) != size:
            raise PhoneReadError('The music database changed while reading; retry.')
        contents.append(data)
    after = tuple([await _stat_optional(afc, name) for name in names])
    if before != after:
        raise PhoneReadError('The music database changed while reading; retry.')
    return before, tuple(contents)


async def stable_capture(afc):
    # A completed sync may checkpoint its WAL. Discard changed captures and
    # require two matching complete reads; never use a partial snapshot.
    for attempt in range(3):
        try:
            first = await _capture_once(afc)
            second = await _capture_once(afc)
            if first == second:
                return second[1]
        except PhoneReadError as exc:
            if 'changed' not in str(exc):
                raise
        if attempt < 2:
            await asyncio.sleep(0.5)
    raise PhoneReadError('The phone library changed during inspection; retry when it is idle.')


def parse_catalog(database, wal):
    with tempfile.TemporaryDirectory(prefix='music-catalog-') as directory:
        path = Path(directory) / 'MediaLibrary.sqlitedb'
        path.write_bytes(database)
        if wal:
            Path(str(path) + '-wal').write_bytes(wal)
        try:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                db.execute('PRAGMA query_only=ON')
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise PhoneReadError('The copied music database failed its integrity check.')
                ident = db.execute('SELECT db_pid,primary_container_pid FROM db_info').fetchall()
                if len(ident) != 1 or not all(ident[0]):
                    raise PhoneReadError('The music database identity is missing or ambiguous.')
                anchor_rows = db.execute("SELECT value FROM _MLDatabaseProperties WHERE key='MLSyncClientSyncAnchor'").fetchall()
                if len(anchor_rows) != 1:
                    raise PhoneReadError('The music sync anchor is missing or ambiguous.')
                text = str(anchor_rows[0][0])
                if not text.isascii() or not text.isdecimal() or str(int(text)) != text or int(text) >= 2**32 - 1:
                    raise PhoneReadError('The music sync anchor has an unsupported value.')
                lists = []
                for pid, name, created in db.execute('''SELECT sync_id, name, date_created FROM container
                    WHERE COALESCE(is_hidden,0)=0 AND COALESCE(distinguished_kind,0)=0
                    AND COALESCE(smart_is_folder,0)=0 AND COALESCE(smart_is_dynamic,0)=0
                    AND COALESCE(smart_is_filtered,0)=0 AND COALESCE(smart_is_genius,0)=0
                    AND COALESCE(is_external_vendor_playlist,0)=0 AND COALESCE(is_collaborative,0)=0
                    AND COALESCE(is_ignorable_itunes_playlist,0)=0
                    ORDER BY name, container_pid''').fetchall():
                    if not pid:
                        continue
                    members = db.execute('''SELECT s.sync_id FROM container_item ci
                        JOIN container c ON ci.container_pid=c.container_pid
                        LEFT JOIN item_store s ON ci.item_pid=s.item_pid
                        WHERE c.sync_id=? ORDER BY ci.position,ci.container_item_pid''', (pid,)).fetchall()
                    if any(not row[0] for row in members):
                        raise PhoneReadError('A phone playlist contains a song that cannot be identified safely.')
                    if created is None or int(created) < 0:
                        continue
                    lists.append(Playlist(pid, name or '', tuple(row[0] for row in members), int(created)))
                count = db.execute('''SELECT COUNT(*) FROM item i JOIN item_extra e USING(item_pid)
                    WHERE e.media_kind=1''').fetchone()[0]
                dates = tuple(db.execute('''SELECT s.sync_id,i.date_added FROM item i
                    JOIN item_store s USING(item_pid) JOIN item_extra e USING(item_pid)
                    WHERE e.media_kind=1 ORDER BY s.sync_id,i.item_pid''').fetchall())
                names = tuple((pid,name or '') for pid,name in db.execute('SELECT sync_id,name FROM container'))
                return Catalog(*ident[0], int(text), tuple(lists), count, dates, names)
        except sqlite3.Error as exc:
            raise PhoneReadError('Could not read the copied music database: ' + str(exc)) from exc


async def read_snapshot(serial):
    entries = [d for d in await list_devices() if d.is_usb and d.serial == serial]
    if len(entries) != 1:
        raise PhoneReadError('The selected phone disconnected; refresh the list.')
    client = await create_using_usbmux(serial=serial, autopair=False, connection_type='USB')
    try:
        if not client.paired:
            raise PhoneReadError('Unlock and trust this computer on the phone.')
        afc = AfcService(client)
        await afc.connect()
        try:
            return await asyncio.wait_for(stable_capture(afc), timeout=120)
        finally:
            await afc.close()
    finally:
        await client.close()


async def read_catalog(serial):
    db, wal = await read_snapshot(serial)
    return parse_catalog(db, wal)


async def read_library_identity(serial):
    """Read and cross-check the phone's current library identity; never print it."""
    entries = [d for d in await list_devices() if d.is_usb and d.serial == serial]
    if len(entries) != 1:
        raise PhoneReadError('The selected phone disconnected; refresh the list.')
    client = await create_using_usbmux(serial=serial, autopair=False, connection_type='USB')
    try:
        if not client.paired:
            raise PhoneReadError('Unlock and trust this computer on the phone.')
        afc = AfcService(client)
        await afc.connect()
        try:
            return await identity_from_afc(afc)
        finally:
            await afc.close()
    finally:
        await client.close()


async def identity_from_afc(afc):
    identities = []
    for path in ('/iTunes_Control/iTunes/iTunesPrefs', '/iTunes_Control/iTunes/iTunesPrefs.plist'):
        stats = await _stat_optional(afc, path)
        if stats is None:
            continue
        info = dict(stats)
        size = info['st_size']
        if info['st_ifmt'] != 'S_IFREG' or not 0 < size <= 1024 * 1024:
            raise PhoneReadError('Unsupported phone music preferences.')
        handle = await afc.fopen(path)
        try:
            data = await afc.fread(handle, size + 1)
        finally:
            await afc.fclose(handle)
        if len(data) != size or await _stat_optional(afc, path) != stats:
            raise PhoneReadError('Music preferences changed while reading; retry.')
        try:
            identities.append(library_identity(data))
        except ProtocolError as exc:
            raise PhoneReadError(str(exc)) from exc
    if not identities or any(identity != identities[0] for identity in identities[1:]):
        raise PhoneReadError('Phone music preferences are missing or do not agree.')
    return identities[0]
