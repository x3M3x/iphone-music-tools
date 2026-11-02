import plistlib
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from atc_protocol import ProtocolError
from atc_send_playlist import update_playlist
from direct_phone import Catalog, Playlist
from test_atc_staging import Afc
from test_atc_protocol import Service


class PlaylistSendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.catalog = Catalog(1,2,7,(Playlist(5,'Arabic',(),100),Playlist(6,'Quran',(),100)),
                               2,((3,10),(4,20)))
        fixture = Path(__file__).resolve().parent / 'tests-fixtures/grappa-fixed-vectors.plist'
        reply = plistlib.loads(fixture.read_bytes())[0]['reply']
        self.service = Service([
            {'Command':'Pong','Session':2},
            {'Command':'Capabilities','Params':{'GrappaSupportInfo':{'version':1,'deviceType':0,'protocolVersion':1}}},
            {'Command':'SyncAllowed','Params':{'ManualSync':True,'DataProtected':False,'EducationModeEnabled':False,'DisabledAssetTypes':[]}},
            {'Command':'ReadyForSync','Session':2,'Params':{'DataclassAnchors':{'Media':'7'},'DeviceInfo':{'Grappa':reply}}},
            {'Command':'SyncFinished','Session':2}])
        self.afc = Afc()

    async def test_playlist_update_verifies_order_without_changing_dates(self):
        after = Catalog(1,2,8,(Playlist(5,'Arabic',(4,3),100),Playlist(6,'Quran',(),100)),
                        2,((3,10),(4,20)))
        with patch('atc_send_playlist.stable_capture', AsyncMock(return_value=(b'',None))), \
                patch('atc_send_playlist.parse_catalog', return_value=after):
            self.assertTrue(await update_playlist(self.service,self.afc,identity=(1,2),catalog=self.catalog,
                                                 playlist_id=5,desired_ids=['4','3']))
        self.assertEqual([x['Command'] for x in self.service.outgoing],
                         ['Ping','RequestingSync','FinishedSyncingMetadata'])

    async def test_existing_members_cannot_be_removed(self):
        previous = Catalog(1,2,7,(Playlist(5,'Arabic',(3,),100),),2,((3,10),(4,20)))
        with self.assertRaisesRegex(ProtocolError, 'remove'):
            await update_playlist(self.service,self.afc,identity=(1,2),catalog=previous,
                                  playlist_id=5,desired_ids=['4'])
        self.assertEqual(self.service.outgoing, [])
        self.assertEqual(self.afc.files, {})

    async def test_changed_library_dates_are_not_marked_successful(self):
        after = Catalog(1,2,8,(Playlist(5,'Arabic',(4,3),100),Playlist(6,'Quran',(),100)),
                        2,((3,10),(4,99)))
        with patch('atc_send_playlist.stable_capture', AsyncMock(return_value=(b'',None))), \
                patch('atc_send_playlist.parse_catalog', return_value=after):
            with self.assertRaisesRegex(ProtocolError, 'song dates changed'):
                await update_playlist(self.service,self.afc,identity=(1,2),catalog=self.catalog,
                                      playlist_id=5,desired_ids=['4','3'])


if __name__ == '__main__':
    unittest.main()
