import base64
import hashlib
import plistlib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from atc_protocol import ProtocolError, validate_batch_manifest
from atc_send_batch import build_operation, send_batch
from test_atc_protocol import Service
from test_atc_staging import Afc


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
        service=Service(self.handshake+[manifest,{'Session':2,'Command':'SyncFinished'}]);afc=Afc()
        ids=await send_batch(service,afc,identity=(1,2),database_id=1,library_id=2,anchor=7,entries=self.entries)
        self.assertEqual(ids,['3','4'])
        self.assertEqual([x['Params']['AssetID'] for x in service.outgoing if x['Command']=='FileComplete'],['3','4'])
        self.assertEqual(afc.files['/Airlock/Media/3'],b'song3')

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
