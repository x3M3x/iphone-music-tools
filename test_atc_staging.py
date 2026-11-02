import plistlib
import hashlib
import io
import unittest
from datetime import datetime
from pathlib import Path

from pymobiledevice3.exceptions import AfcFileNotFoundError
from atc_staging import StagingError, stage_insert, stage_audio, METADATA, SIGNATURE


class Afc:
    def __init__(self):
        self.files = {}
        self.handles = {}
        self.positions = {}
        self.next_handle = 0

    async def listdir(self, path):
        return ['.', '..'] + [p.rsplit('/',1)[-1] for p in self.files if p.startswith(path + '/')]

    async def stat(self, path):
        if path in ('/Airlock/Media','/Airlock/Media/Artwork','/iTunes_Control/Music/F01'):
            return {'st_ifmt':'S_IFDIR'}
        if path not in self.files:
            raise AfcFileNotFoundError('missing', 8)
        return {'st_size':len(self.files[path]), 'st_ifmt':'S_IFREG'}

    async def fopen(self, path, mode='r'):
        if mode == 'r+':
            self.files.setdefault(path, b'')
        elif path not in self.files:
            raise AfcFileNotFoundError('missing', 8)
        self.next_handle += 1
        self.handles[self.next_handle] = path
        self.positions[self.next_handle] = 0
        return self.next_handle

    async def fwrite(self, handle, data):
        path = self.handles[handle]
        self.files[path] += data

    async def fread(self, handle, length):
        data=self.files[self.handles[handle]]
        start=self.positions[handle]
        self.positions[handle] += length
        return data[start:start+length]

    async def fclose(self, handle):
        del self.handles[handle]
        del self.positions[handle]

    async def rename(self, source, destination):
        self.files[destination] = self.files.pop(source)


class StageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.afc = Afc()
        fixture = Path(__file__).resolve().parent / 'tests-fixtures/grappa-fixed-vectors.plist'
        self.reply = plistlib.loads(fixture.read_bytes())[0]['reply']
        self.operation = plistlib.dumps({'revision':8, 'timestamp':datetime(2026,1,1), 'operations':[
            {'operation':'update_db_info','pid':1,'db_info':{'primary_container_pid':2}},
            {'operation':'insert_track','pid':3,'item':{},'item_stats':{},'avformat_info':{},'location':{}}]}, fmt=plistlib.FMT_BINARY)

    async def test_stages_one_matching_signed_operation(self):
        await stage_insert(self.afc,self.operation,self.reply,database_id=1,library_id=2,track_id=3,anchor=7)
        self.assertEqual(self.afc.files[METADATA],self.operation)
        self.assertEqual(len(self.afc.files[SIGNATURE]),21)
        with self.assertRaisesRegex(StagingError, 'pending'):
            await stage_insert(self.afc,self.operation,self.reply,database_id=1,library_id=2,track_id=3,anchor=7)

    async def test_rejects_wrong_operation_before_any_write(self):
        with self.assertRaisesRegex(StagingError, 'does not match'):
            await stage_insert(self.afc,self.operation,self.reply,database_id=1,library_id=2,track_id=4,anchor=7)
        self.assertFalse(self.afc.files)

    async def test_stages_one_audio_and_its_cover_without_overwriting(self):
        args = dict(asset_id='3', extension='.mp3', stream=io.BytesIO(b'audio'), size=5,
                    expected_sha=hashlib.sha256(b'audio').hexdigest(),
                    permanent_path='iTunes_Control/Music/F01/ABCD.mp3', cover=b'cover')
        await stage_audio(self.afc, **args)
        self.assertEqual(self.afc.files['/Airlock/Media/3'],b'audio')
        self.assertEqual(self.afc.files['/Airlock/Media/Artwork/3'],b'cover')
        before = self.afc.files.copy()
        with self.assertRaisesRegex(StagingError, 'already exists'):
            await stage_audio(self.afc, **args)
        self.assertEqual(self.afc.files,before)

    async def test_missing_destination_folder_fails_before_any_write(self):
        with self.assertRaisesRegex(StagingError, 'directory is missing'):
            await stage_audio(self.afc, asset_id='3', extension='.mp3', stream=io.BytesIO(b'audio'), size=5,
                              expected_sha=hashlib.sha256(b'audio').hexdigest(),
                              permanent_path='iTunes_Control/Music/F02/ABCD.mp3', cover=b'cover')
        self.assertFalse(self.afc.files)

    async def test_large_audio_is_streamed_and_read_back_in_chunks(self):
        data=b'x'*(2*1024*1024+17)
        class Bounded(io.BytesIO):
            def read(self, size=-1):
                if size < 0 or size > 1024*1024:
                    raise AssertionError('unbounded read')
                return super().read(size)
        await stage_audio(self.afc,asset_id='3',extension='.mp3',stream=Bounded(data),size=len(data),
                          expected_sha=hashlib.sha256(data).hexdigest(),
                          permanent_path='iTunes_Control/Music/F01/ABCD.mp3')
        self.assertEqual(self.afc.files['/Airlock/Media/3'],data)

    async def test_audio_is_not_read_back_at_staging_but_metadata_is(self):
        reads=[]
        original=self.afc.fread
        async def counting(handle,length):
            reads.append(length)
            return await original(handle,length)
        self.afc.fread=counting
        await stage_audio(self.afc,asset_id='3',extension='.mp3',stream=io.BytesIO(b'audio'),size=5,
                          expected_sha=hashlib.sha256(b'audio').hexdigest(),
                          permanent_path='iTunes_Control/Music/F01/ABCD.mp3')
        self.assertEqual(reads,[])  # audio readback deferred to the permanent-file check
        await stage_insert(self.afc,self.operation,self.reply,database_id=1,library_id=2,track_id=3,anchor=7)
        self.assertTrue(reads)      # signed metadata pair is still verified in place

    async def test_source_change_during_stream_stops_before_rename(self):
        with self.assertRaisesRegex(StagingError,'source changed'):
            await stage_audio(self.afc,asset_id='3',extension='.mp3',stream=io.BytesIO(b'wrong'),size=5,
                              expected_sha=hashlib.sha256(b'audio').hexdigest(),
                              permanent_path='iTunes_Control/Music/F01/ABCD.mp3')
        self.assertNotIn('/Airlock/Media/3',self.afc.files)

    async def test_file_larger_than_previous_limit_uses_bounded_reads(self):
        size=65*1024*1024+1
        class Source:
            remaining=size
            def read(self,n):
                if n < 0 or n > 1024*1024:
                    raise AssertionError('unbounded source read')
                n=min(n,self.remaining)
                self.remaining-=n
                return b'x'*n
        class CountingAfc(Afc):
            def __init__(self):
                super().__init__()
                self.lengths={}
            async def stat(self,path):
                if path in self.lengths:
                    return {'st_size':self.lengths[path],'st_ifmt':'S_IFREG'}
                return await super().stat(path)
            async def fopen(self,path,mode='r'):
                if mode=='r+':
                    self.lengths[path]=0
                self.next_handle+=1
                self.handles[self.next_handle]=path
                self.positions[self.next_handle]=0
                return self.next_handle
            async def fwrite(self,handle,data):
                assert len(data)<=1024*1024
                self.lengths[self.handles[handle]]+=len(data)
            async def fread(self,handle,length):
                assert length<=1024*1024
                path=self.handles[handle]
                n=min(length,self.lengths[path]-self.positions[handle])
                self.positions[handle]+=n
                return b'x'*n
            async def rename(self,source,destination):
                self.lengths[destination]=self.lengths.pop(source)
        digest=hashlib.sha256()
        remaining=size
        while remaining:
            block=b'x'*min(remaining,1024*1024)
            digest.update(block)
            remaining-=len(block)
        phone=CountingAfc()
        await stage_audio(phone,asset_id='3',extension='.mp3',stream=Source(),size=size,
                          expected_sha=digest.hexdigest(),permanent_path='iTunes_Control/Music/F01/ABCD.mp3')
        self.assertEqual(phone.lengths['/Airlock/Media/3'],size)


if __name__ == '__main__':
    unittest.main()
