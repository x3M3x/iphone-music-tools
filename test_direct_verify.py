import hashlib
import asyncio
import threading
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from direct_phone import PhoneReadError
from direct_verify import parse_track, parse_tracks, _hash_remote, verify_preserved
from collections import Counter


class ReadbackTests(unittest.TestCase):
    def test_existing_rows_must_be_preserved(self):
        before={'item':Counter({(1,'old'):1}), 'container':Counter({('Arabic',):1})}
        verify_preserved(before,before)
        after={'item':Counter({(1,'changed'):1}), 'container':before['container']}
        with self.assertRaisesRegex(PhoneReadError,'Existing phone item changed'):
            verify_preserved(before,after)

    def test_track_path_and_artwork_are_bound_to_wire_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.sqlite'
            with closing(sqlite3.connect(path)) as db:
                db.executescript('''
                    CREATE TABLE item(item_pid INTEGER,item_artist_pid INTEGER,album_pid INTEGER,base_location_id INTEGER,in_my_library INTEGER,date_added INTEGER,track_number INTEGER,disc_number INTEGER,album_artist_pid INTEGER,genre_id INTEGER,composer_pid INTEGER,is_compilation INTEGER);
                    CREATE TABLE item_store(item_pid INTEGER,sync_id INTEGER);
                    CREATE TABLE item_extra(item_pid INTEGER,location TEXT,file_size INTEGER,title TEXT,media_kind INTEGER,track_count INTEGER,disc_count INTEGER,year INTEGER,copyright TEXT,comment TEXT);
                    CREATE TABLE item_artist(item_artist_pid INTEGER,item_artist TEXT);
                    CREATE TABLE album(album_pid INTEGER,album TEXT);
                    CREATE TABLE album_artist(album_artist_pid INTEGER,album_artist TEXT);
                    CREATE TABLE genre(genre_id INTEGER,genre TEXT);
                    CREATE TABLE composer(composer_pid INTEGER,composer TEXT);
                    CREATE TABLE base_location(base_location_id INTEGER,path TEXT);
                    CREATE TABLE artwork_token(entity_pid INTEGER,entity_type INTEGER,artwork_source_type INTEGER,artwork_token TEXT);
                    CREATE TABLE artwork(artwork_token TEXT,artwork_source_type INTEGER,relative_path TEXT);
                    INSERT INTO item VALUES(1,2,3,4,1,123,1,1,NULL,NULL,NULL,0);
                    INSERT INTO item_store VALUES(1,77);
                    INSERT INTO item_extra VALUES(1,'ABCD.mp3',5,'Title',1,0,0,0,NULL,NULL);
                    INSERT INTO item_artist VALUES(2,'Artist');
                    INSERT INTO album VALUES(3,'Album');
                    INSERT INTO base_location VALUES(4,'F01');
                    INSERT INTO artwork_token VALUES(1,0,300,'9');
                    INSERT INTO artwork VALUES('9',300,'ab/cover');
                ''')
            data = path.read_bytes()
            track = parse_track(data, None, 77, 9)
            self.assertEqual(parse_tracks(data,None,[(77,9),(77,9)]),[track,track])
            self.assertEqual(track.path, 'iTunes_Control/Music/F01/ABCD.mp3')
            self.assertEqual(track.date_added, 123)
            self.assertEqual((track.title,track.artist,track.album),('Title','Artist','Album'))
            self.assertEqual(track.artwork_path,'iTunes_Control/iTunes/Artwork/Originals/ab/cover')
            with self.assertRaisesRegex(PhoneReadError, 'missing or has an ambiguous'):
                parse_track(data, None, 78, 9)
            with self.assertRaisesRegex(PhoneReadError, 'not registered'):
                parse_track(data, None, 77, 10)


class Remote:
    def __init__(self, data):
        self.data = data
        self.offset = 0
    async def stat(self, path):
        return {'st_ifmt':'S_IFREG','st_size':len(self.data),'st_mtime':1,'st_birthtime':1}
    async def fopen(self, path):
        return 1
    async def fread(self, handle, size):
        block = self.data[self.offset:self.offset+size]
        self.offset += len(block)
        return block
    async def fclose(self, handle):
        pass


class RemoteReadbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_existing_checks_metadata_and_sizes_without_reading_bytes(self):
        from direct_verify import TrackReadback, _check_fast, _check_and_hash
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'song.mp3';path.write_bytes(b'audio')
            source=SimpleNamespace(path=path,title='Title',artist='Artist',album='Album',
                tags=(),cover=b'cover',cover_format='JPEG')
            metadata={k:None for k in ('genre','albumartist','composer','copyright','comment',
                     'track_number','track_count','disc_number','disc_count','year','compilation')}
            track=TrackReadback('music/song',5,'Title','Artist','Album','art/cover',123,metadata)
            afc=SimpleNamespace(stat=AsyncMock(side_effect=lambda p: {'st_ifmt':'S_IFREG',
                'st_size':5,'st_mtime':1,'st_birthtime':1}))
            with patch('direct_verify.file_date_ns',return_value=(123+978307200)*1_000_000_000), \
                 patch('direct_verify.phone_cover',return_value=b'cover'), \
                 patch('direct_verify._hash_remote',new=AsyncMock(return_value='wrong')) as readback:
                self.assertTrue(await _check_fast(afc,track,source,'music/song','expected'))
                readback.assert_not_awaited()
                self.assertEqual(afc.stat.await_count,2)
                with self.assertRaisesRegex(PhoneReadError,'bytes do not match'):
                    await _check_and_hash(afc,track,source,'music/song','expected')
                afc.stat.side_effect=lambda p: {'st_ifmt':'S_IFREG','st_size':4}
                with self.assertRaisesRegex(PhoneReadError,'unexpected size'):
                    await _check_fast(afc,track,source,'music/song','expected')
                with self.assertRaisesRegex(PhoneReadError,'path or file size'):
                    await _check_fast(afc,track,source,'music/other','expected')

    async def test_verification_overlaps_reads_and_keeps_progress_order(self):
        from direct_verify import verify_inserts
        active=0; peak=0; progress=[]
        async def read(_afc,_track,source,_path,_sha):
            nonlocal active,peak
            active+=1; peak=max(peak,active)
            await asyncio.sleep(.01 if source.path.name == '1' else .03)
            active-=1
        items=[(i,SimpleNamespace(path=Path(str(i))),'path','hash',None) for i in range(1,6)]
        with patch('direct_verify.stable_capture',new=AsyncMock(return_value=(b'db',None))), \
             patch('direct_verify.parse_tracks',return_value=[object()]*5), \
             patch('direct_verify._check_and_hash',side_effect=read):
            result=await verify_inserts(object(),items,lambda n,total,name: progress.append((n,name)),threading.Event(),workers=2)
        self.assertEqual(peak,2)
        self.assertEqual(progress,[(i,str(i)) for i in range(1,6)])
        self.assertEqual(len(result),5)

    async def test_verification_failure_cancels_outstanding_reads(self):
        from direct_verify import verify_inserts
        stopped=asyncio.Event()
        async def read(_afc,_track,source,_path,_sha):
            if source.path.name == 'bad':
                await asyncio.sleep(.01)
                raise PhoneReadError('bad read')
            try:
                await asyncio.sleep(1)
            finally:
                stopped.set()
        items=[(i,SimpleNamespace(path=Path(name)),'path','hash',None) for i,name in ((1,'bad'),(2,'other'))]
        with patch('direct_verify.stable_capture',new=AsyncMock(return_value=(b'db',None))), \
             patch('direct_verify.parse_tracks',return_value=[object()]*2), \
             patch('direct_verify._check_and_hash',side_effect=read):
            with self.assertRaisesRegex(PhoneReadError,'bad read'):
                await verify_inserts(object(),items)
        self.assertTrue(stopped.is_set())

    async def test_hashes_file_contents_and_checks_size(self):
        self.assertEqual(await _hash_remote(Remote(b'track'), 'music/file', 5), hashlib.sha256(b'track').hexdigest())
        with self.assertRaisesRegex(PhoneReadError, 'unexpected size'):
            await _hash_remote(Remote(b'track'), 'music/file', 6)


if __name__ == '__main__':
    unittest.main()
