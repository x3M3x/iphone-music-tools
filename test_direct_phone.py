import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import AsyncMock, patch
from pathlib import Path

from direct_phone import PhoneReadError, parse_catalog, stable_capture


class CatalogTests(unittest.TestCase):
    def test_reads_ordered_existing_playlists_from_local_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'music.sqlite'
            with closing(sqlite3.connect(path)) as db:
                db.executescript('''
                    CREATE TABLE db_info(db_pid INTEGER, primary_container_pid INTEGER);
                    INSERT INTO db_info VALUES(1,2);
                    CREATE TABLE _MLDatabaseProperties(key TEXT,value TEXT);
                    INSERT INTO _MLDatabaseProperties VALUES('MLSyncClientSyncAnchor','8');
                    CREATE TABLE container(container_pid INTEGER,sync_id INTEGER,name TEXT,is_hidden INTEGER,distinguished_kind INTEGER,
                      date_created INTEGER,smart_is_folder INTEGER,smart_is_dynamic INTEGER,smart_is_filtered INTEGER,
                      smart_is_genius INTEGER,is_external_vendor_playlist INTEGER,is_collaborative INTEGER,is_ignorable_itunes_playlist INTEGER);
                    INSERT INTO container VALUES(10,11,'Arabic',0,0,1,0,0,0,0,0,0,0),(20,22,'Hidden',1,0,1,0,0,0,0,0,0,0);
                    CREATE TABLE container_item(container_pid INTEGER,item_pid INTEGER,position INTEGER,container_item_pid INTEGER);
                    INSERT INTO container_item VALUES(10,1,1,2),(10,2,0,1);
                    CREATE TABLE item_store(item_pid INTEGER,sync_id INTEGER);
                    INSERT INTO item_store VALUES(1,101),(2,102);
                    CREATE TABLE item(item_pid INTEGER,date_added INTEGER);
                    INSERT INTO item VALUES(1,20),(2,30);
                    CREATE TABLE item_extra(item_pid INTEGER,media_kind INTEGER);
                    INSERT INTO item_extra VALUES(1,1),(2,1);
                ''')
            catalog = parse_catalog(path.read_bytes(), None)
            self.assertEqual((catalog.database_id, catalog.library_id, catalog.anchor, catalog.song_count), (1,2,8,2))
            self.assertEqual(catalog.song_dates, ((101,20),(102,30)))
            self.assertEqual([(p.name, p.members) for p in catalog.playlists], [('Arabic', (102, 101))])
            with closing(sqlite3.connect(path)) as db:
                db.execute('DELETE FROM item_store WHERE item_pid=2')
                db.commit()
            with self.assertRaisesRegex(PhoneReadError,'cannot be identified safely'):
                parse_catalog(path.read_bytes(),None)

    def test_rejects_invalid_or_incomplete_database(self):
        with self.assertRaises(PhoneReadError):
            parse_catalog(b'not sqlite', None)


class FakeAfc:
    def __init__(self, changing=False):
        self.changing = changing
        self.reads = 0

    async def stat(self, path):
        from pymobiledevice3.exceptions import AfcFileNotFoundError
        if path.endswith('-wal') or path.endswith('-shm'):
            raise AfcFileNotFoundError('absent', 8)
        return {'st_size': 3, 'st_ifmt': 'S_IFREG', 'st_mtime': 1, 'st_birthtime': 1}

    async def fopen(self, path):
        return 1

    async def fread(self, handle, length):
        self.reads += 1
        return b'abc' if not self.changing or self.reads % 2 else b'abd'

    async def fclose(self, handle):
        pass


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_requires_two_matching_copies(self):
        self.assertEqual(await stable_capture(FakeAfc()), (b'abc', None))
        with self.assertRaisesRegex(PhoneReadError, 'changed'):
            await stable_capture(FakeAfc(changing=True))

    async def test_checkpoint_during_first_read_retries_complete_matching_snapshots(self):
        snapshot = (('stable',), (b'db', None))
        with patch('direct_phone._capture_once', AsyncMock(side_effect=[
                PhoneReadError('The music database changed while reading; retry.'), snapshot, snapshot])) as capture, \
                patch('direct_phone.asyncio.sleep', AsyncMock()):
            self.assertEqual(await stable_capture(FakeAfc()), (b'db', None))
        self.assertEqual(capture.await_count, 3)

    async def test_other_database_errors_are_not_retried(self):
        with patch('direct_phone._capture_once', AsyncMock(side_effect=PhoneReadError('invalid database'))) as capture:
            with self.assertRaisesRegex(PhoneReadError, 'invalid database'):
                await stable_capture(FakeAfc())
        self.assertEqual(capture.await_count, 1)


if __name__ == '__main__':
    unittest.main()
