import unittest
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from direct_adapter import DirectAdapter
from direct_phone import Catalog, Playlist, PhoneReadError


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_metadata_cache_reuses_entries_and_invalidates_changed_files(self):
        adapter=DirectAdapter('new-phone',object(),object())
        with tempfile.TemporaryDirectory() as tmp:
            first=Path(tmp)/'one.mp3'; second=Path(tmp)/'two.mp3'
            first.write_bytes(b'a'); second.write_bytes(b'b')
            songs=[SimpleNamespace(path=path) for path in (first,second)]
            with patch('direct_adapter.inspect', side_effect=lambda p: p.read_bytes()) as inspect_file:
                self.assertEqual([adapter._inspect(s) for s in songs], [b'a',b'b'])
                self.assertEqual([adapter._inspect(s) for s in songs], [b'a',b'b'])
                self.assertEqual(inspect_file.call_count,2)
                first.write_bytes(b'changed')
                self.assertEqual(adapter._inspect(songs[0]), b'changed')
                self.assertEqual(inspect_file.call_count,3)

    async def test_empty_destination_is_rejected(self):
        with self.assertRaisesRegex(PhoneReadError, 'destination'):
            DirectAdapter('', object(), object())

    async def test_unrecognized_existing_music_is_not_overwritten_or_duplicated(self):
        adapter = DirectAdapter('new-phone', object(), object())
        async def fake_work(): return object()
        catalog = Catalog(1,2,7,(Playlist(5,'Arabic',(),100),),1,((3,10),))
        with patch('direct_adapter.identity_from_afc', AsyncMock(return_value=(1,2))), \
                patch.object(adapter, 'work_afc', new=AsyncMock(side_effect=fake_work)), \
                patch.object(adapter, '_snapshot', AsyncMock(return_value=(catalog,None))):
            with self.assertRaisesRegex(PhoneReadError, 'not verified'):
                await adapter.validate_device('new-phone', ['5'], [])
            await adapter.validate_device('new-phone', ['5'], ['3'])

    async def test_selected_phone_must_match_adapter(self):
        adapter = DirectAdapter('new-phone', object(), object())
        with self.assertRaisesRegex(PhoneReadError, 'changed'):
            await adapter.validate_device('different-phone', [], [])

    async def test_existing_metadata_checks_overlap_and_use_one_phone_verification(self):
        from music_importer import Song
        from direct_audio import SourceAudio
        adapter=DirectAdapter('new-phone',object(),object())
        async def fake_work(): return object()
        active=0
        peak=0
        gate=threading.Lock()
        with tempfile.TemporaryDirectory() as tmp:
            entries=[]
            for index in range(4):
                path=Path(tmp)/f'{index}.mp3';path.write_bytes(b'audio')
                stat=path.stat()
                song=Song(path,str(index),'','',stat.st_size,stat.st_mtime_ns,None,9)
                entries.append((song,str(index+1),'sha',{'track_id':str(index+1),'sha':'sha',
                                'path':'remote','artwork_id':None}))
            def inspect_file(path):
                nonlocal active,peak
                with gate:
                    active+=1;peak=max(peak,active)
                time.sleep(.05)
                with gate:
                    active-=1
                return SourceAudio(path,'mpeg1-layer3',44100,2,128,1.0,44100,
                                   path.stem,'','',None,None,0,())
            verify=AsyncMock(return_value={})
            with patch('direct_adapter.file_date_ns',return_value=9), \
                 patch('direct_adapter.inspect',side_effect=inspect_file), \
                 patch.object(adapter,'work_afc',new=AsyncMock(side_effect=fake_work)), \
                 patch('direct_adapter.verify_inserts',verify):
                await adapter.verify_existing('new-phone',entries,lambda *args,**kwargs:None,threading.Event())
            self.assertGreater(peak,1)
            verify.assert_awaited_once()
            self.assertEqual(len(verify.await_args.args[1]),4)

    async def test_fast_mode_is_only_passed_to_existing_song_check(self):
        from music_importer import Song
        from direct_audio import SourceAudio
        adapter=DirectAdapter('new-phone',object(),object(),fast_existing=True)
        async def fake_work(): return object()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'a.mp3';path.write_bytes(b'audio');st=path.stat()
            song=Song(path,'a','','',st.st_size,st.st_mtime_ns,None,9)
            source=SourceAudio(path,'mpeg1-layer3',44100,2,128,1.0,44100,'a','','',None,None,0,())
            entry=(song,'1','sha',{'track_id':'1','sha':'sha','path':'remote','artwork_id':None})
            with patch('direct_adapter.file_date_ns',return_value=9), \
                 patch('direct_adapter.inspect',return_value=source), \
                 patch.object(adapter,'work_afc',new=AsyncMock(side_effect=fake_work)), \
                 patch('direct_adapter.verify_inserts',new=AsyncMock(return_value={})) as verify:
                await adapter.verify_existing('new-phone',[entry],lambda *a,**k:None,threading.Event())
            self.assertTrue(verify.await_args.kwargs['fast_existing'])

    async def test_upload_batch_starts_on_a_fresh_work_connection(self):
        from music_importer import Song
        from direct_audio import SourceAudio
        class FakeAfcService:
            def __init__(self, client): pass
            async def connect(self): return None
            async def close(self): pass
        service=SimpleNamespace(close=AsyncMock())
        client=SimpleNamespace(start_lockdown_service=AsyncMock(return_value=service))
        adapter=DirectAdapter('new-phone',client,object())
        adapter.expected_identity=(1,2);adapter.before_batch_rows={}
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'a.mp3';path.write_bytes(b'audio');st=path.stat()
            song=Song(path,'a','','',st.st_size,st.st_mtime_ns,None,9)
            source=SourceAudio(path,'mpeg1-layer3',44100,2,128,1.0,44100,'a','','',None,None,0,())
            plan={'track_id':'1','sha':'sha','path':'remote','artwork_id':None,
                  'anchor':3,'database_id':1,'library_id':2}
            adapter.current_batch=[plan]
            entry={'song':song,'sha':'sha','plan':plan}
            after=SimpleNamespace(anchor=4,song_dates=((1,10),))
            async def fake_refresh(): return object()
            with patch('direct_adapter.file_date_ns',return_value=9), \
                 patch.object(adapter,'_inspect',return_value=source), \
                 patch('direct_adapter.AfcService',FakeAfcService), \
                 patch.object(adapter,'refresh_work_afc',new=AsyncMock(side_effect=fake_refresh)) as refresh, \
                 patch('direct_adapter.identity_from_afc',new=AsyncMock(return_value=(1,2))), \
                 patch('direct_adapter.send_batch',new=AsyncMock(return_value=['1'])), \
                 patch('direct_adapter.stable_capture',new=AsyncMock(return_value=(b'db',None))), \
                 patch('direct_adapter.parse_catalog',return_value=after), \
                 patch('direct_adapter.parse_inventory'), \
                 patch('direct_adapter.preserved_rows',return_value={}), \
                 patch('direct_adapter.verify_inserts',new=AsyncMock(return_value={})):
                await adapter.upload_batch('new-phone',[entry])
            refresh.assert_awaited_once()

    async def test_fast_existing_mode_still_fully_verifies_new_upload(self):
        from music_importer import Song
        from direct_audio import SourceAudio
        service=SimpleNamespace(close=AsyncMock())
        client=SimpleNamespace(start_lockdown_service=AsyncMock(return_value=service))
        adapter=DirectAdapter('new-phone',client,object(),fast_existing=True)
        adapter.expected_identity=(1,2);adapter.before_batch_rows={}
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'a.mp3';path.write_bytes(b'audio');st=path.stat()
            song=Song(path,'a','','',st.st_size,st.st_mtime_ns,None,9)
            source=SourceAudio(path,'mpeg1-layer3',44100,2,128,1.0,44100,'a','','',None,None,0,())
            plan={'track_id':'1','sha':'sha','path':'remote','artwork_id':None,
                  'anchor':3,'database_id':1,'library_id':2}
            adapter.current_batch=[plan]
            entry={'song':song,'sha':'sha','plan':plan}
            after=SimpleNamespace(anchor=4,song_dates=((1,10),))
            with patch('direct_adapter.file_date_ns',return_value=9), \
                 patch.object(adapter,'_inspect',return_value=source), \
                 patch('direct_adapter.identity_from_afc',new=AsyncMock(return_value=(1,2))), \
                 patch('direct_adapter.send_batch',new=AsyncMock(return_value=['1'])), \
                 patch('direct_adapter.stable_capture',new=AsyncMock(return_value=(b'db',None))), \
                 patch('direct_adapter.parse_catalog',return_value=after), \
                 patch('direct_adapter.parse_inventory'), \
                 patch('direct_adapter.preserved_rows',return_value={}), \
                 patch('direct_adapter.verify_inserts',new=AsyncMock(return_value={})) as verify:
                await adapter.upload_batch('new-phone',[entry])
            self.assertFalse(verify.await_args.kwargs.get('fast_existing',False))


if __name__ == '__main__':
    unittest.main()
