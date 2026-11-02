import asyncio
import plistlib
import struct
import unittest
from pathlib import Path

from atc_protocol import (ProtocolError, library_identity, negotiate, metadata_finished,
                          validate_manifest, receive_event, file_complete, file_unavailable)


def frame(message):
    data = plistlib.dumps(message, fmt=plistlib.FMT_BINARY)
    return struct.pack('<I', len(data)) + data


class Service:
    def __init__(self, messages):
        self.incoming = bytearray(b''.join(map(frame, messages)))
        self.outgoing = []

    async def recvall(self, size):
        if len(self.incoming) < size:
            raise EOFError('disconnected')
        out = bytes(self.incoming[:size])
        del self.incoming[:size]
        return out

    async def sendall(self, data):
        self.outgoing.append(plistlib.loads(data[4:]))


class IdentityTests(unittest.TestCase):
    def test_reads_both_preference_forms_and_rejects_invalid(self):
        raw = bytearray(1232)
        raw[:9] = b'frpd\x01\x00\x19\x00\x01'
        struct.pack_into('<QQ', raw, 12, 1, 2)
        self.assertEqual(library_identity(bytes(raw)), (1, 2))
        self.assertEqual(library_identity(plistlib.dumps({'iPodPrefs':bytes(raw)})), (1, 2))
        for bad in (bytes(raw[:-1]), b'bad', plistlib.dumps({'iPodPrefs': b'wrong'})):
            with self.subTest(bad=bad[:4]), self.assertRaises(ProtocolError):
                library_identity(bad)

    def test_manifest_is_checked_completely_before_upload(self):
        asset = lambda ident, parts: {'AssetID':ident, 'AssetType':'Music', 'IsDownload':True,
                                      'Variant':{'AssetParts':parts}}
        message = {'Session':2, 'Command':'AssetManifest',
                   'Params':{'AssetManifest':{'Media':[asset('42',3),asset('7',2)]}}}
        self.assertEqual(validate_manifest(message, 42, {3,7}, {('7',2)}), ['7'])
        with self.assertRaisesRegex(ProtocolError, 'unrecognized'):
            validate_manifest(message, 42, {3,7})
        message['Params']['AssetManifest']['Media'].append(asset('42',3))
        with self.assertRaisesRegex(ProtocolError, 'duplicate'):
            validate_manifest(message, 42, {3,7}, {('7',2)})
        self.assertEqual(metadata_finished(7)['Params']['DataclassAnchors']['Media'], '8')


class HandshakeTests(unittest.IsolatedAsyncioTestCase):
    def messages(self, anchor='7'):
        fixture = Path(__file__).resolve().parent / 'tests-fixtures/grappa-fixed-vectors.plist'
        reply = plistlib.loads(fixture.read_bytes())[0]['reply']
        return [
            {'Command':'Pong', 'Session':2},
            {'Command':'Capabilities','Params':{'GrappaSupportInfo':{'version':1,'deviceType':0,'protocolVersion':1}}},
            {'Command':'SyncAllowed','Params':{'ManualSync':True,'DataProtected':False,'EducationModeEnabled':False,'DisabledAssetTypes':[]}},
            {'Command':'ReadyForSync','Session':2,'Params':{'DataclassAnchors':{'Media':anchor},'DeviceInfo':{'Grappa':reply}}},
        ]

    async def test_matching_anchor_and_full_request(self):
        service = Service(self.messages())
        reply = await negotiate(service, (1,2), 7)
        self.assertEqual(len(reply), 83)
        self.assertEqual([x['Command'] for x in service.outgoing], ['Ping','RequestingSync'])
        self.assertEqual(service.outgoing[1]['Params']['HostInfo']['LibraryID'], '0000000000000003')

    async def test_anchor_mismatch_never_stages_or_finishes(self):
        service = Service(self.messages('8'))
        with self.assertRaisesRegex(ProtocolError, 'changed'):
            await negotiate(service, (1,2), 7)
        self.assertEqual([x['Command'] for x in service.outgoing], ['Ping','RequestingSync'])

    async def test_locked_phone_never_requests_sync(self):
        messages = self.messages()
        messages[2]['Params']['DataProtected'] = True
        service = Service(messages)
        with self.assertRaisesRegex(ProtocolError, 'not ready'):
            await negotiate(service, (1,2), 7)
        self.assertEqual([x['Command'] for x in service.outgoing], ['Ping'])

    async def test_progress_and_ping_before_terminal(self):
        service = Service([{'Session':2,'Command':'Ping'},
                           {'Session':2,'Command':'Progress','Params':{'OverallProgress':0.5}},
                           {'Session':2,'Command':'SyncFinished'}])
        self.assertEqual((await receive_event(service))['Command'], 'SyncFinished')
        self.assertEqual(service.outgoing, [{'Session':2, 'Command':'Pong'}])
        self.assertEqual(file_complete('42','iTunes_Control/Music/F01/ABCD.mp3')['Params']['AssetID'], '42')
        self.assertEqual(file_unavailable('9')['Params']['ErrorCode'], 16)


if __name__ == '__main__':
    unittest.main()
