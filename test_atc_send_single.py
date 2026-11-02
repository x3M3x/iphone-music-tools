import hashlib
import plistlib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from atc_send_single import send_one
from atc_protocol import ProtocolError
from test_atc_staging import Afc
from test_atc_protocol import Service, HandshakeTests


class SingleSongTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'song.mp3'
        self.path.write_bytes(b'audio')
        self.source = SimpleNamespace(path=self.path, cover=b'cover', cover_format='JPEG')
        self.catalog = SimpleNamespace(anchor=7, database_id=1, library_id=2)
        self.operation = plistlib.dumps({'revision':8,'timestamp':datetime(2026,1,1),'operations':[
            {'operation':'update_db_info','pid':1,'db_info':{'primary_container_pid':2}},
            {'operation':'insert_track','pid':3,'item':{},'item_stats':{},'avformat_info':{},'location':{}}]}, fmt=plistlib.FMT_BINARY)
        self.afc = Afc()
        fixture = Path(__file__).resolve().parent / 'tests-fixtures/grappa-fixed-vectors.plist'
        reply = plistlib.loads(fixture.read_bytes())[0]['reply']
        self.handshake = [
            {'Command':'Pong','Session':2},
            {'Command':'Capabilities','Params':{'GrappaSupportInfo':{'version':1,'deviceType':0,'protocolVersion':1}}},
            {'Command':'SyncAllowed','Params':{'ManualSync':True,'DataProtected':False,'EducationModeEnabled':False,'DisabledAssetTypes':[]}},
            {'Command':'ReadyForSync','Session':2,'Params':{'DataclassAnchors':{'Media':'7'},'DeviceInfo':{'Grappa':reply}}}]

    def service(self, asset_id='3'):
        manifest = {'Session':2,'Command':'AssetManifest','Params':{'AssetManifest':{'Media':[
            {'AssetID':asset_id,'AssetType':'Music','IsDownload':True,'Variant':{'AssetParts':3}}]}}}
        return Service(self.handshake + [manifest, {'Session':2,'Command':'SyncFinished'}])

    async def test_one_song_waits_for_manifest_and_terminal_then_verifies(self):
        service = self.service()
        verify = AsyncMock(return_value=True)
        with patch('atc_send_single.verify_insert', verify):
            result = await send_one(service, self.afc, identity=(1,2), catalog=self.catalog,
                source=self.source, expected_sha=hashlib.sha256(b'audio').hexdigest(),
                signed_track_id=3, artwork_id=9, operation=self.operation,
                permanent_path='iTunes_Control/Music/F01/ABCD.mp3')
        self.assertEqual(result, '3')
        self.assertEqual([x['Command'] for x in service.outgoing],
                         ['Ping','RequestingSync','FinishedSyncingMetadata','FileComplete'])
        self.assertEqual(self.afc.files['/Airlock/Media/3'], b'audio')
        self.assertEqual(self.afc.files['/Airlock/Media/Artwork/3'], b'cover')
        verify.assert_awaited_once()

    async def test_unknown_manifest_stops_before_uploading_audio(self):
        service = self.service('4')
        with self.assertRaisesRegex(ProtocolError, 'unrecognized'):
            await send_one(service, self.afc, identity=(1,2), catalog=self.catalog,
                source=self.source, expected_sha=hashlib.sha256(b'audio').hexdigest(),
                signed_track_id=3, artwork_id=9, operation=self.operation,
                permanent_path='iTunes_Control/Music/F01/ABCD.mp3')
        self.assertNotIn('/Airlock/Media/3',self.afc.files)
        self.assertNotIn('FileComplete',[x['Command'] for x in service.outgoing])

    async def test_no_cover_accepts_audio_with_optional_genius_bit(self):
        self.source.cover = None
        service = self.service()
        service.incoming.clear()
        from test_atc_protocol import frame
        manifest = {'Session':2,'Command':'AssetManifest','Params':{'AssetManifest':{'Media':[
            {'AssetID':'3','AssetType':'Music','IsDownload':True,'Variant':{'AssetParts':5}}]}}}
        service.incoming.extend(b''.join(map(frame,self.handshake + [manifest, {'Session':2,'Command':'SyncFinished'}])))
        with patch('atc_send_single.verify_insert', AsyncMock(return_value=True)):
            result = await send_one(service,self.afc,identity=(1,2),catalog=self.catalog,
                source=self.source,expected_sha=hashlib.sha256(b'audio').hexdigest(),
                signed_track_id=3,artwork_id=None,operation=self.operation,
                permanent_path='iTunes_Control/Music/F01/ABCD.mp3')
        self.assertEqual(result,'3')


if __name__ == '__main__':
    unittest.main()
