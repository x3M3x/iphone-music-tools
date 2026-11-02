import plistlib
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from atc_create_test_playlist import create
from atc_protocol import ProtocolError
from direct_phone import Catalog, Playlist
from test_atc_staging import Afc
from test_atc_protocol import Service


class CreateTestPlaylist(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.before = Catalog(1,2,7,(Playlist(5,'Arabic',(),100),),1,((3,10),))
        self.after = Catalog(1,2,8,(Playlist(5,'Arabic',(),100),
                                    Playlist(1001,'Codex Import Test',(3,),200)),1,((3,10),))
        fixture = Path(__file__).resolve().parent/'tests-fixtures/grappa-fixed-vectors.plist'
        reply = plistlib.loads(fixture.read_bytes())[0]['reply']
        self.service = Service([
            {'Command':'Pong','Session':2},
            {'Command':'Capabilities','Params':{'GrappaSupportInfo':{'version':1,'deviceType':0,'protocolVersion':1}}},
            {'Command':'SyncAllowed','Params':{'ManualSync':True,'DataProtected':False,'EducationModeEnabled':False,'DisabledAssetTypes':[]}},
            {'Command':'ReadyForSync','Session':2,'Params':{'DataclassAnchors':{'Media':'7'},'DeviceInfo':{'Grappa':reply}}},
            {'Command':'SyncFinished','Session':2}])
        self.afc = Afc()

    async def test_creation_requires_readback_and_keeps_existing_playlist(self):
        with patch('atc_create_test_playlist.stable_capture', AsyncMock(return_value=(b'',None))), \
             patch('atc_create_test_playlist.parse_catalog', side_effect=[self.before,self.after]):
            self.assertEqual(await create(self.service,self.afc,identity=(1,2),catalog=self.before,
                                          playlist_id=1001,name='Codex Import Test',track_id=3),1001)
        self.assertEqual([m['Command'] for m in self.service.outgoing],
                         ['Ping','RequestingSync','FinishedSyncingMetadata'])

    async def test_same_name_is_refused_before_network_or_staging(self):
        with self.assertRaisesRegex(ProtocolError,'already exists'):
            await create(self.service,self.afc,identity=(1,2),catalog=self.before,
                         playlist_id=1001,name='Arabic',track_id=3)
        self.assertFalse(self.service.outgoing)
        self.assertFalse(self.afc.files)

    async def test_creates_ordered_playlist_from_multiple_existing_tracks(self):
        self.before=Catalog(1,2,7,(Playlist(5,'Arabic',(),100),),2,((3,10),(4,20)))
        self.after=Catalog(1,2,8,(Playlist(5,'Arabic',(),100),
                                  Playlist(1001,'New',(4,3),200)),2,((3,10),(4,20)))
        with patch('atc_create_test_playlist.stable_capture',AsyncMock(return_value=(b'',None))), \
             patch('atc_create_test_playlist.parse_catalog',side_effect=[self.before,self.after]):
            self.assertEqual(await create(self.service,self.afc,identity=(1,2),catalog=self.before,
                             playlist_id=1001,name='New',track_ids=[4,3]),1001)

    async def test_changed_catalog_before_staging_refuses(self):
        changed=Catalog(1,2,7,(Playlist(5,'Arabic',(),100),Playlist(6,'Other',(),100)),1,((3,10),))
        with patch('atc_create_test_playlist.stable_capture',AsyncMock(return_value=(b'',None))), \
             patch('atc_create_test_playlist.parse_catalog',return_value=changed):
            with self.assertRaisesRegex(ProtocolError,'library changed'):
                await create(self.service,self.afc,identity=(1,2),catalog=self.before,
                             playlist_id=1001,name='New',track_id=3)
        self.assertFalse(self.afc.files)
