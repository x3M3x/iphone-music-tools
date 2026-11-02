import plistlib
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from atc_date_test import update_date
from direct_phone import Catalog, Playlist
from test_atc_protocol import Service
from test_atc_staging import Afc


class DateTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_requested_track_date_changes(self):
        before=Catalog(1,2,7,(Playlist(5,'Test',(3,),100),),2,((3,0),(4,0)))
        after=Catalog(1,2,8,before.playlists,2,((3,703074388),(4,0)))
        fixture=Path(__file__).resolve().parent/'tests-fixtures/grappa-fixed-vectors.plist'
        reply=plistlib.loads(fixture.read_bytes())[0]['reply']
        service=Service([
            {'Command':'Pong','Session':2},
            {'Command':'Capabilities','Params':{'GrappaSupportInfo':{'version':1,'deviceType':0,'protocolVersion':1}}},
            {'Command':'SyncAllowed','Params':{'ManualSync':True,'DataProtected':False,'EducationModeEnabled':False,'DisabledAssetTypes':[]}},
            {'Command':'ReadyForSync','Session':2,'Params':{'DataclassAnchors':{'Media':'7'},'DeviceInfo':{'Grappa':reply}}},
            {'Command':'SyncFinished','Session':2}])
        with patch('atc_date_test.stable_capture', AsyncMock(return_value=(b'',None))), \
             patch('atc_date_test.parse_catalog',return_value=after):
            result=await update_date(service,Afc(),identity=(1,2),before=before,track_id=3,
                                     saved_unix=1681381588,allowed_foreign=set())
        self.assertEqual(result,after)
