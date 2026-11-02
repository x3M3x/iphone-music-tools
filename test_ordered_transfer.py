import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from music_importer import ImportErrorSafe, Song
from ordered_transfer import RunLock, run


class FakePhone:
    def __init__(self):
        self.calls = []
        self.files = {}
        self.lists = {}
        self.fail = False
        self.prepare_calls = 0

    async def validate_device(self, device, destinations, verified_tracks):
        self.calls.append(('validate', device, tuple(destinations)))
        return {d:tuple(self.lists.get(d,[])) for d in destinations}

    async def prepare_song(self, device, song, sha):
        raise AssertionError('per-song preparation is unused in batch mode')

    async def prepare_batch(self, device, songs_shas):
        self.prepare_calls += 1
        return [{'track_id':'id-' + song.title, 'sha':sha} for song, sha in songs_shas]

    async def upload_batch(self, device, entries, on_asset=None):
        identities = []
        for entry in entries:
            if on_asset is not None:
                on_asset(entry['plan']['track_id'], entry)
            identities.append(await self.upload_song(device, entry['song'], entry['sha'], entry['plan']))
        return identities

    async def upload_song(self, device, song, sha, prepared):
        self.calls.append(('upload', song.title))
        if self.fail:
            raise RuntimeError('connection lost')
        track = 'id-' + song.title
        self.files[track] = sha
        # The adapter contract requires upload to verify before returning.
        self.calls.append(('verify', song.title))
        return track

    async def verify_song(self, device, track, song, sha, plan):
        self.calls.append(('verify', song.title))
        return self.files.get(track) == sha

    async def verify_existing(self, device, entries, progress, stop):
        for index,(song,track,sha,plan) in enumerate(entries,1):
            if stop.is_set():
                raise ImportErrorSafe('Stopped')
            if not await self.verify_song(device,track,song,sha,plan):
                raise ImportErrorSafe('A previously uploaded song could not be verified on the phone.')
            progress(index,len(entries),song.path.name)

    async def validate_playlist_update(self, device, destination, desired):
        self.calls.append(('check-list', destination))

    async def update_playlist(self, device, destination, desired):
        self.calls.append(('update-list', destination, tuple(desired)))
        self.lists[destination] = list(desired)

    async def verify_playlist(self, device, destination, desired):
        return self.lists.get(destination) == desired

    async def resolve_playlist_targets(self,device,names):
        return [(name if name in self.lists else 'new:'+name,name not in self.lists) for name in names]

    async def create_playlist(self,device,destination,name,desired):
        self.calls.append(('create-list',name,tuple(desired)))
        self.lists[destination]=list(desired)


class OrderedTransferTests(unittest.IsolatedAsyncioTestCase):
    async def test_hash_worker_limit_is_configurable_and_order_is_preserved(self):
        import time
        from ordered_transfer import ordered_hashes
        active=0;peak=0;guard=threading.Lock()
        def work(song):
            nonlocal active,peak
            with guard: active+=1;peak=max(peak,active)
            time.sleep(.03)
            with guard: active-=1
            return song
        with patch('ordered_transfer.content_hash',side_effect=work):
            result=[x async for x in ordered_hashes(['a','b','c','d'],threading.Event(),workers=2)]
        self.assertEqual(peak,2)
        self.assertEqual(result,['a','b','c','d'])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.songs = []
        for label, date in [('old', 1), ('middle', 2), ('new', 3)]:
            path = self.root / (label + '.mp3')
            path.write_bytes(label.encode())
            stat = path.stat()
            self.songs.append(Song(path, label, '', '', stat.st_size, stat.st_mtime_ns, None, date))
        self.phone = FakePhone()
        self.journal = self.root / 'journal.sqlite'
        self.stop = threading.Event()

    async def execute(self):
        return await run(self.phone, 'phone-1', self.songs,
                         [('Arabic', [self.songs[0], self.songs[2]])],
                         self.journal, lambda *args: None, self.stop)

    async def test_uploads_are_serial_oldest_first_then_playlist_newest_first(self):
        await self.execute()
        self.assertEqual(self.phone.calls, [
            ('validate', 'phone-1', ('Arabic',)),
            ('upload', 'old'), ('verify', 'old'),
            ('upload', 'middle'), ('verify', 'middle'),
            ('upload', 'new'), ('verify', 'new'),
            ('check-list', 'Arabic'), ('update-list', 'Arabic', ('id-new', 'id-old'))])
        await self.execute()
        self.assertEqual(sum(c[0] == 'upload' for c in self.phone.calls), 3)
        self.assertEqual(sum(c[0] == 'update-list' for c in self.phone.calls), 1)

    async def test_uncertain_upload_is_never_retried_automatically(self):
        self.phone.fail = True
        with self.assertRaisesRegex(RuntimeError, 'connection lost'):
            await self.execute()
        self.phone.fail = False
        with self.assertRaisesRegex(ImportErrorSafe, 'uncertain result'):
            await self.execute()
        self.assertEqual(sum(c[0] == 'upload' for c in self.phone.calls), 1)
        self.assertFalse(self.phone.lists)

    async def test_invalid_playlist_plan_is_rejected_before_any_upload(self):
        with self.assertRaisesRegex(ImportErrorSafe, 'different existing playlist'):
            await run(self.phone, 'phone-1', self.songs,
                      [('Arabic', self.songs), ('Arabic', self.songs)],
                      self.journal, lambda *args: None, self.stop)
        self.assertFalse(self.phone.calls)

    async def test_stop_after_first_verified_song_does_not_link_or_upload_more(self):
        def progress(phase, index, total, filename):
            if phase == 'verified':
                self.stop.set()
        result = await run(self.phone, 'phone-1', self.songs,
                           [('Arabic', self.songs)], self.journal, progress, self.stop)
        self.assertTrue(result['stopped'])
        self.assertEqual([c for c in self.phone.calls if c[0] == 'upload'], [('upload', 'old')])
        self.assertFalse(self.phone.lists)

    async def test_uncertain_playlist_update_is_never_retried_automatically(self):
        async def uncertain(device, destination, desired):
            self.phone.calls.append(('update-list', destination, tuple(desired)))
            raise RuntimeError('lost during playlist update')
        self.phone.update_playlist = uncertain
        with self.assertRaisesRegex(RuntimeError, 'lost during playlist update'):
            await self.execute()
        with self.assertRaisesRegex(ImportErrorSafe, 'uncertain result'):
            await self.execute()
        self.assertEqual(sum(c[0] == 'update-list' for c in self.phone.calls), 1)

    async def test_another_import_cannot_use_same_journal(self):
        with RunLock(self.journal):
            with self.assertRaisesRegex(ImportErrorSafe, 'already running'):
                await self.execute()
        self.assertFalse(self.phone.calls)

    async def test_identical_files_are_reused_once_in_a_playlist(self):
        duplicate=self.root/'duplicate.mp3'
        duplicate.write_bytes(b'old')
        stat=duplicate.stat()
        from music_importer import Song
        copy=Song(duplicate,'copy','','',stat.st_size,stat.st_mtime_ns,None,4)
        await run(self.phone,'phone-1',self.songs+[copy], [('Arabic',[self.songs[0],copy])],
                  self.journal,lambda *args:None,self.stop)
        self.assertEqual(sum(c[0]=='upload' for c in self.phone.calls),3)
        self.assertEqual(self.phone.lists['Arabic'],['id-old'])

    async def test_batch_mode_prepares_and_uploads_each_group_once(self):
        self.phone.calls.clear()
        def progress(phase,index,total,name):
            if phase=='verified' and index==3:
                self.stop.set()
        result=await run(self.phone,'phone-1',self.songs,[('Arabic',self.songs)],
                         self.journal,progress,self.stop,batch_size=3)
        self.assertTrue(result['stopped'])
        self.assertEqual([c[0] for c in self.phone.calls],
                         ['validate','upload','verify','upload','verify','upload','verify'])
        self.assertEqual(self.phone.prepare_calls,1)

    async def test_failed_batch_marks_all_members_pending_and_never_retries(self):
        self.phone.fail=True
        with self.assertRaisesRegex(RuntimeError,'connection lost'):
            await run(self.phone,'phone-1',self.songs,[],self.journal,lambda *x:None,self.stop,batch_size=3)
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.journal)) as db:
            self.assertEqual(db.execute('SELECT status,COUNT(*) FROM uploads GROUP BY status').fetchall(), [('pending',3)])
        self.phone.fail=False
        with self.assertRaisesRegex(ImportErrorSafe,'uncertain result'):
            await run(self.phone,'phone-1',self.songs,[],self.journal,lambda *x:None,self.stop,batch_size=3)
        self.assertEqual(sum(c[0]=='upload' for c in self.phone.calls),1)

    async def test_unrelated_existing_playlist_song_stops_before_upload(self):
        self.phone.lists['Arabic']=['unknown']
        with self.assertRaisesRegex(ImportErrorSafe,'unrelated songs'):
            await self.execute()
        self.assertFalse(any(c[0]=='upload' for c in self.phone.calls))

    async def test_auto_uses_existing_and_creates_missing_after_uploads(self):
        self.phone.lists['Arabic']=[]
        await run(self.phone,'phone-1',self.songs,[],self.journal,lambda *x:None,self.stop,
                  batch_size=3,automatic_playlists=[('Arabic',[self.songs[0]],'Arabic'),
                                                    ('New',[self.songs[1],self.songs[2]],None)])
        self.assertEqual(self.phone.lists['Arabic'],['id-old'])
        self.assertEqual(self.phone.lists['new:New'],['id-new','id-middle'])
        self.assertEqual(sum(c[0]=='create-list' for c in self.phone.calls),1)

    async def test_auto_name_changed_after_confirmation_stops_before_upload(self):
        self.phone.lists['Arabic']=[]
        with self.assertRaisesRegex(ImportErrorSafe,'changed since confirmation'):
            await run(self.phone,'phone-1',self.songs,[],self.journal,lambda *x:None,self.stop,
                      automatic_playlists=[('Arabic',self.songs,None)])
        self.assertFalse(any(c[0]=='upload' for c in self.phone.calls))

    async def test_uncertain_auto_create_is_not_retried(self):
        async def uncertain(device,destination,name,desired):
            self.phone.calls.append(('create-list',name,tuple(desired)))
            raise RuntimeError('connection lost during creation')
        self.phone.create_playlist=uncertain
        args=(self.phone,'phone-1',self.songs,[],self.journal,lambda *x:None,self.stop)
        with self.assertRaisesRegex(RuntimeError,'lost during creation'):
            await run(*args,automatic_playlists=[('New',self.songs,None)])
        with self.assertRaisesRegex(ImportErrorSafe,'uncertain result'):
            await run(*args,automatic_playlists=[('New',self.songs,None)])
        self.assertEqual(sum(c[0]=='create-list' for c in self.phone.calls),1)

    async def test_parallel_local_checks_keep_upload_order_and_stop_safely(self):
        import time
        from music_importer import content_hash as real_hash
        active=0
        peak=0
        guard=threading.Lock()
        def slow_hash(song):
            nonlocal active,peak
            with guard:
                active+=1
                peak=max(peak,active)
            try:
                if song.title=='old':
                    time.sleep(.08)
                else:
                    time.sleep(.02)
                return real_hash(song)
            finally:
                with guard:
                    active-=1
        with patch('ordered_transfer.content_hash',side_effect=slow_hash):
            await run(self.phone,'phone-1',self.songs,[],self.journal,
                      lambda *args:None,self.stop,batch_size=3)
        self.assertGreater(peak,1)
        self.assertEqual([call[1] for call in self.phone.calls if call[0]=='upload'],
                         ['old','middle','new'])

    async def test_stop_during_parallel_check_prevents_any_upload(self):
        import time
        from music_importer import content_hash as real_hash
        def slow_hash(song):
            time.sleep(.03)
            return real_hash(song)
        def progress(phase,index,total,name):
            if phase=='checking_local':
                self.stop.set()
        with patch('ordered_transfer.content_hash',side_effect=slow_hash):
            result=await run(self.phone,'phone-1',self.songs,[],self.journal,progress,self.stop,batch_size=3)
        self.assertTrue(result['stopped'])
        self.assertFalse(any(call[0]=='upload' for call in self.phone.calls))


if __name__ == '__main__':
    unittest.main()
