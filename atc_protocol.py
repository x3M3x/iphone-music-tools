"""Windows-compatible AirTraffic session preparation; no device writes here.

Session shapes are based on rustunes protocol documentation (MIT, copyright
2026 Feim Biba). This module only opens and negotiates a session: it does not
stage metadata, finish a sync, transfer files, or change the phone library.
"""
import asyncio
import plistlib
import struct

from atc_signing import HOST_REQUEST, sign_metadata

MAX_FRAME = 4 * 1024 * 1024


class ProtocolError(Exception):
    pass


def library_identity(data):
    if len(data) > 1024 * 1024:
        raise ProtocolError('Music preferences are too large.')
    try:
        raw = data if data.startswith(b'frpd') else plistlib.loads(data)['iPodPrefs']
    except (ValueError, KeyError, TypeError) as exc:
        raise ProtocolError('Unsupported music preferences.') from exc
    if not isinstance(raw, bytes) or len(raw) != 1232 or raw[:9] != b'frpd\x01\x00\x19\x00\x01':
        raise ProtocolError('The phone music preferences are not configured in the supported format.')
    catalog, user = struct.unpack_from('<QQ', raw, 12)
    if not catalog or not user or not catalog ^ user:
        raise ProtocolError('The phone music library identity is unavailable.')
    return catalog, user


def sync_request(identity, anchor):
    if not isinstance(identity, tuple) or len(identity) != 2 or any(
            isinstance(n, bool) or not isinstance(n, int) or n <= 0 or n >= 2**64 for n in identity):
        raise ProtocolError('Invalid phone music library identity.')
    if identity[0] == identity[1] or isinstance(anchor, bool) or not isinstance(anchor, int) or not 0 <= anchor < 2**32 - 1:
        raise ProtocolError('Invalid library identity or sync anchor.')
    return {'Session': 2, 'Command': 'RequestingSync', 'Params': {
        'Dataclasses': ['Media', 'Book', 'Keybag'],
        'DataclassAnchors': {'Media': str(anchor), 'VoiceMemo': '1'},
        'HostInfo': {'Type': 'iTunes', 'Version': '13.2.4.123', 'WindowsOSVersion': '10.0',
                     'SyncHostName': 'Music Importer', 'LibraryID': f'{identity[0] ^ identity[1]:016X}',
                     'SyncedDataclasses': ['Media'], 'SyncedAssetTypes': ['Music'],
                     'Wakeable': 0, 'Grappa': HOST_REQUEST}}}


async def send_frame(service, message):
    payload = plistlib.dumps(message, fmt=plistlib.FMT_BINARY)
    if not 0 < len(payload) <= MAX_FRAME:
        raise ProtocolError('AirTraffic message exceeds the size limit.')
    await service.sendall(struct.pack('<I', len(payload)) + payload)


async def receive_frame(service):
    length = struct.unpack('<I', await service.recvall(4))[0]
    if not 0 < length <= MAX_FRAME:
        raise ProtocolError('AirTraffic frame has an invalid length.')
    try:
        message = plistlib.loads(await service.recvall(length))
    except Exception as exc:
        raise ProtocolError('Invalid AirTraffic message.') from exc
    if not isinstance(message, dict) or not isinstance(message.get('Command'), str):
        raise ProtocolError('Invalid AirTraffic message shape.')
    return message


async def negotiate(service, identity, anchor):
    """Negotiate with a fresh connection and return a session-scoped signer.

    Caller must close the service if anything fails. Do not stage files or
    perform an actual sync until a separate verified writer is implemented.
    """
    request = sync_request(identity, anchor)
    await send_frame(service, {'Session': 1, 'Command': 'Ping'})
    pong = capable = allowed = requested = False
    for _ in range(64):
        message = await asyncio.wait_for(receive_frame(service), 30)
        command, params = message['Command'], message.get('Params', {})
        if command == 'Pong':
            if pong or requested:
                raise ProtocolError('Unexpected Pong during handshake.')
            pong = True
        elif command == 'Capabilities':
            if params.get('GrappaSupportInfo') != {'version': 1, 'deviceType': 0, 'protocolVersion': 1}:
                raise ProtocolError('The phone uses an unsupported signing protocol.')
            capable = True
        elif command == 'SyncAllowed':
            if (params.get('ManualSync') is not True or params.get('DataProtected') is not False
                    or params.get('EducationModeEnabled') is not False
                    or not isinstance(params.get('DisabledAssetTypes'), list)
                    or 'Music' in params['DisabledAssetTypes']):
                raise ProtocolError('The phone is not ready for a music sync.')
            allowed = True
        elif command in ('InstalledAssets', 'AssetMetrics'):
            if not isinstance(params, dict):
                raise ProtocolError('Invalid device telemetry.')
        elif command == 'ReadyForSync':
            if not requested or message.get('Session') != 2:
                raise ProtocolError('Unexpected AirTraffic session.')
            try:
                actual = params['DataclassAnchors']['Media']
                reply = params['DeviceInfo']['Grappa']
            except (KeyError, TypeError) as exc:
                raise ProtocolError('The phone did not provide the required sync details.') from exc
            if actual != str(anchor):
                raise ProtocolError('The phone library changed during preparation; retry.')
            try:
                sign_metadata(reply, b'validation')
            except ValueError as exc:
                raise ProtocolError('Unsupported signing reply.') from exc
            return reply
        else:
            raise ProtocolError('Unexpected AirTraffic command: ' + command)
        if not requested and pong and capable and allowed:
            await send_frame(service, request)
            requested = True
    raise ProtocolError('Phone handshake did not complete.')


def metadata_finished(anchor):
    if isinstance(anchor, bool) or not isinstance(anchor, int) or not 0 <= anchor < 2**32 - 1:
        raise ProtocolError('Invalid sync anchor.')
    return {'Session': 2, 'Command': 'FinishedSyncingMetadata', 'Params': {
        'DataclassAnchors': {'Media': str(anchor + 1), 'VoiceMemo': '1'},
        'SyncTypes': {'Media': 1, 'Book': 0, 'Keybag': 1}}}


def validate_foreign_manifest(message, allowed_foreign):
    if message.get('Session') != 2 or message.get('Command') != 'AssetManifest':
        raise ProtocolError('Unexpected metadata-only asset manifest.')
    try:
        assets = message['Params']['AssetManifest']['Media']
    except (KeyError, TypeError) as exc:
        raise ProtocolError('Malformed asset manifest.') from exc
    if not isinstance(assets, list) or len(assets) > 1024:
        raise ProtocolError('Invalid asset manifest size.')
    allowed, seen = set(allowed_foreign), set()
    for entry in assets:
        try:
            identity, parts = entry['AssetID'], entry['Variant']['AssetParts']
            if (entry['AssetType'] != 'Music' or entry['IsDownload'] is not True
                    or parts not in (2, 4) or (identity,parts) not in allowed or identity in seen):
                raise ProtocolError(f'Unrecognized foreign asset request (part {parts!r}; known: '
                                    f'{(identity,parts) in allowed}).')
            seen.add(identity)
        except (KeyError, TypeError) as exc:
            raise ProtocolError('Malformed foreign asset request.') from exc
    return [entry['AssetID'] for entry in assets]


def validate_manifest(message, own_id, own_parts, allowed_foreign=()):
    """Validate every request before the caller replies or uploads anything."""
    if message.get('Session') != 2 or message.get('Command') != 'AssetManifest':
        raise ProtocolError('Expected an asset manifest for the current session.')
    try:
        assets = message['Params']['AssetManifest']['Media']
    except (KeyError, TypeError) as exc:
        raise ProtocolError('Malformed asset manifest.') from exc
    if not isinstance(assets, list) or not 1 <= len(assets) <= 1024:
        raise ProtocolError('Unsupported asset manifest size.')
    expected = str(own_id)
    allowed = set(allowed_foreign)
    seen, foreign = set(), []
    own = False
    for entry in assets:
        try:
            asset_id, part = entry['AssetID'], entry['Variant']['AssetParts']
            canonical = str(int(asset_id)) == asset_id and -(2**63) <= int(asset_id) < 2**63 and int(asset_id) != 0
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ProtocolError('Malformed asset request.') from None
        if (not canonical or asset_id in seen or entry.get('AssetType') != 'Music'
                or entry.get('IsDownload') is not True or isinstance(part, bool) or not isinstance(part, int)):
            raise ProtocolError('Unsupported or duplicate asset request.')
        seen.add(asset_id)
        if asset_id == expected:
            if part not in own_parts:
                raise ProtocolError(f'The phone requested unexpected parts of the song: {part!r}.')
            own = True
        elif (asset_id, part) in allowed and part in (2, 4):
            foreign.append(asset_id)
        else:
            raise ProtocolError('The phone requested an unrecognized asset; no transfer was attempted.')
    if not own:
        raise ProtocolError('The phone did not request the planned song.')
    return foreign


def validate_batch_manifest(message, own_parts, allowed_foreign=()):
    """Validate a manifest that must request every song of one batch."""
    if message.get('Session') != 2 or message.get('Command') != 'AssetManifest':
        raise ProtocolError('Expected an asset manifest for the current session.')
    try:
        assets = message['Params']['AssetManifest']['Media']
    except (KeyError, TypeError) as exc:
        raise ProtocolError('Malformed asset manifest.') from exc
    if not isinstance(assets, list) or not 1 <= len(assets) <= 1024:
        raise ProtocolError('Unsupported asset manifest size.')
    allowed = set(allowed_foreign)
    remaining = dict(own_parts)
    if not remaining:
        raise ProtocolError('The planned batch is empty.')
    seen, foreign = set(), []
    for entry in assets:
        try:
            asset_id, part = entry['AssetID'], entry['Variant']['AssetParts']
            canonical = str(int(asset_id)) == asset_id and -(2**63) <= int(asset_id) < 2**63 and int(asset_id) != 0
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ProtocolError('Malformed asset request.') from None
        if (not canonical or asset_id in seen or entry.get('AssetType') != 'Music'
                or entry.get('IsDownload') is not True or isinstance(part, bool) or not isinstance(part, int)):
            raise ProtocolError('Unsupported or duplicate asset request.')
        seen.add(asset_id)
        if asset_id in remaining:
            if part not in remaining.pop(asset_id):
                raise ProtocolError(f'The phone requested unexpected parts of a song: {part!r}.')
        elif (asset_id, part) in allowed and part in (2, 4):
            foreign.append(asset_id)
        else:
            raise ProtocolError('The phone requested an unrecognized asset; no transfer was attempted.')
    if remaining:
        raise ProtocolError('The phone did not request every planned song.')
    return foreign


async def receive_event(service):
    """Read one post-metadata event, answering a phone ping when needed."""
    for _ in range(1024):
        message = await asyncio.wait_for(receive_frame(service), 30)
        if message.get('Session') != 2:
            raise ProtocolError('Unexpected device session; stop and inspect the phone.')
        command = message['Command']
        if command == 'Ping':
            await send_frame(service, {'Session': 2, 'Command': 'Pong'})
            continue
        if command in ('Progress', 'SyncAllowed', 'InstalledAssets', 'AssetMetrics'):
            if not isinstance(message.get('Params'), dict):
                raise ProtocolError('Invalid phone progress information.')
            continue
        if command == 'SyncFailed':
            raise ProtocolError('The phone reported that the music sync failed; inspect before retrying.')
        return message
    raise ProtocolError('The phone exceeded the event limit; inspect before retrying.')


def file_complete(asset_id, path):
    if not isinstance(asset_id, str) or not asset_id or not isinstance(path, str):
        raise ProtocolError('Invalid completed audio asset.')
    return {'Session':2, 'Command':'FileComplete',
            'Params':{'AssetID':asset_id, 'Dataclass':'Media', 'AssetPath':path}}


def file_unavailable(asset_id):
    if not isinstance(asset_id, str) or not asset_id:
        raise ProtocolError('Invalid unavailable asset.')
    return {'Session':2, 'Command':'FileError',
            'Params':{'AssetID':asset_id, 'Dataclass':'Media', 'ErrorCode':16, 'ErrorIsVisible':False}}
