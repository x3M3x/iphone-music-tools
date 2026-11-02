import base64
import asyncio
import hashlib
import plistlib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from atc_protocol import ProtocolError, validate_batch_manifest
from atc_send_batch import build_operation, send_batch
from test_atc_protocol import Service, frame
from test_atc_staging import Afc


class LiveService(Service):
    """Send the terminal event only after all files are acknowledged."""
    def __init__(self, messages, expected):
        super().__init__(messages)
        self.expected=expected
        self.ready=asyncio.Event()
        self.pong=asyncio.Event()

    async def recvall(self, size):
        while len(self.incoming)<size:
            self.ready.clear()
            await self.ready.wait()
        return await super().recvall(size)

    async def sendall(self, data):
        await super().sendall(data)
        if self.outgoing[-1]['Command']=='Pong':
            self.pong.set()
        if sum(m['Command']=='FileComplete' for m in self.outgoing)==self.expected and self.outgoing[-1]['Command']=='FileComplete':
            self.incoming.extend(frame({'Session':2,'Command':'SyncFinished'}))
            self.ready.set()


class BatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.entries=[]
        for pid in (3,4):
            path=Path(self.tmp.name)/f'{pid}.mp3';path.write_bytes(f'song{pid}'.encode())
            own={'operation':'insert_track','pid':pid,'item':{},'item_stats':{},'avformat_info':{},'location':{}}
            operation=plistlib.dumps({'revision':8,'timestamp':datetime(2026,1,1),'operations':[
                {'operation':'update_db_info','pid':1,'db_info':{'primary_container_pid':2}},own]},fmt=plistlib.FMT_BINARY)
            plan={'operation':base64.b64encode(operation).decode(),'track_id':str(pid),'anchor':7,
                  'database_id':1,'library_id':2,'path':f'iTunes_Control/Music/F01/ABC{chr(65+pid)}.mp3',
                  'artwork_id':None}
            source=SimpleNamespace(path=path,cover=None)
            self.entries.append({'plan':plan,'source':source,'sha':hashlib.sha256(path.read_bytes()).hexdigest()})
        fixture=Path(__file__).resolve().parent/'tests-fixtures/grappa-fixed-vectors.plist'
        reply=plistlib.loads(fixture.read_bytes())[0]['reply']
        self.handshake=[{'Command':'Pong','Session':2},
          {'Command':'Capabilities','Params':{'GrappaSupportInfo':{'version':1,'deviceType':0,'protocolVersion':1}}},
          {'Command':'SyncAllowed','Params':{'ManualSync':True,'DataProtected':False,'EducationModeEnabled':False,'DisabledAssetTypes':[]}},
          {'Command':'ReadyForSync','Session':2,'Params':{'DataclassAnchors':{'Media':'7'},'DeviceInfo':{'Grappa':reply}}}]

    def asset(self,pid,parts=1):
        return {'AssetID':str(pid),'AssetType':'Music','IsDownload':True,'Variant':{'AssetParts':parts}}

    def test_plans_keep_order_and_reject_collisions(self):
        data=plistlib.loads(build_operation(self.entries,database_id=1,library_id=2,anchor=7))
        self.assertEqual([x['pid'] for x in data['operations'][1:]],[3,4])
        self.entries[1]['plan']['path']=self.entries[0]['plan']['path']
        with self.assertRaises(ProtocolError):
            build_operation(self.entries,database_id=1,library_id=2,anchor=7)

    async def test_reverse_manifest_validates_all_before_upload(self):
        manifest={'Session':2,'Command':'AssetManifest','Params':{'AssetManifest':{'Media':[self.asset(4),self.asset(3)]}}}
        service=LiveService(self.handshake+[manifest],2);afc=Afc()
        ids=await send_batch(service,afc,identity=(1,2),database_id=1,library_id=2,anchor=7,entries=self.entries)
        self.assertEqual(ids,['3','4'])
        self.assertEqual([x['Params']['AssetID'] for x in service.outgoing if x['Command']=='FileComplete'],['3','4'])
        self.assertEqual(afc.files['/Airlock/Media/3'],b'song3')

    async def test_phone_ping_is_answered_while_audio_is_still_uploading(self):
        manifest={'Session':2,'Command':'AssetManifest','Params':{'AssetManifest':{'Media':[self.asset(3)]}}}
        service=LiveService(self.handshake+[manifest,{'Session':2,'Command':'Ping'}],1)
        async def slow_upload(*args, **kwargs):
            await asyncio.wait_for(service.pong.wait(),1)
        with patch('atc_send_batch.stage_audio',side_effect=slow_upload):
            ids=await send_batch(service,Afc(),identity=(1,2),database_id=1,library_id=2,anchor=7,entries=self.entries[:1])
        self.assertEqual(ids,['3'])
        self.assertLess([m['Command'] for m in service.outgoing].index('Pong'),
                        [m['Command'] for m in service.outgoing].index('FileComplete'))

    async def test_audio_upload_sends_progress_before_completion(self):
        manifest={'Session':2,'Command':'AssetManifest','Params':{'AssetManifest':{'Media':[self.asset(3)]}}}
        service=LiveService(self.handshake+[manifest],1)
        async def staged(*args, **kwargs):
            await kwargs['on_progress'](2,4)
            await kwargs['on_progress'](4,4)
        with patch('atc_send_batch.stage_audio',side_effect=staged):
            await send_batch(service,Afc(),identity=(1,2),database_id=1,library_id=2,anchor=7,entries=self.entries[:1])
        progress=[m['Params'] for m in service.outgoing if m['Command']=='FileProgress']
        self.assertEqual(progress,[{'AssetProgress':.5,'OverallProgress':.5},{'AssetProgress':1.0,'OverallProgress':1.0}])
        commands=[m['Command'] for m in service.outgoing]
        self.assertLess(commands.index('FileProgress'),commands.index('FileComplete'))

    async def test_early_sync_finish_does_not_acknowledge_audio(self):
        manifest={'Session':2,'Command':'AssetManifest','Params':{'AssetManifest':{'Media':[self.asset(3)]}}}
        service=Service(self.handshake+[manifest,{'Session':2,'Command':'SyncFinished'}])
        async def slow_upload(*args, **kwargs): await asyncio.sleep(1)
        with patch('atc_send_batch.stage_audio',side_effect=slow_upload):
            with self.assertRaisesRegex(ProtocolError,'before the audio upload completed'):
                await send_batch(service,Afc(),identity=(1,2),database_id=1,library_id=2,anchor=7,entries=self.entries[:1])
        self.assertFalse(any(m['Command']=='FileComplete' for m in service.outgoing))

    async def test_manifest_with_missing_song_does_not_upload_any_audio(self):
        manifest={'Session':2,'Command':'AssetManifest','Params':{'AssetManifest':{'Media':[self.asset(3)]}}}
        service=Service(self.handshake+[manifest]);afc=Afc()
        with self.assertRaisesRegex(ProtocolError,'every planned song'):
            await send_batch(service,afc,identity=(1,2),database_id=1,library_id=2,anchor=7,entries=self.entries)
        self.assertNotIn('/Airlock/Media/3',afc.files)
        self.assertFalse(any(x['Command']=='FileComplete' for x in service.outgoing))

    def test_duplicate_manifest_and_unrelated_asset_are_rejected(self):
        make=lambda assets:{'Session':2,'Command':'AssetManifest','Params':{'AssetManifest':{'Media':assets}}}
        for assets in ([self.asset(3),self.asset(3),self.asset(4)], [self.asset(3),self.asset(4),self.asset(9)]):
            with self.assertRaises(ProtocolError):
                validate_batch_manifest(make(assets),{'3':{1,5},'4':{1,5}})
