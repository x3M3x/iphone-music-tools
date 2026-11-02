"""A narrow, journaled date experiment for an already verified test track."""
from datetime import datetime, timezone
import plistlib

from atc_protocol import (ProtocolError, file_unavailable, metadata_finished, negotiate,
                          receive_event, send_frame, validate_foreign_manifest)
from atc_staging import _put_new, METADATA, SIGNATURE, SYNC_DIR
from atc_signing import sign_metadata
from direct_phone import parse_catalog, stable_capture


async def update_date(service, afc, *, identity, before, track_id, saved_unix, allowed_foreign):
    """Try setting one track's date; caller holds phone lock and pending journal."""
    if isinstance(track_id, bool) or not isinstance(track_id, int) or track_id == 0:
        raise ProtocolError('Invalid test track ID.')
    if isinstance(saved_unix, bool) or not isinstance(saved_unix, int) or saved_unix <= 978307200:
        raise ProtocolError('Invalid saved file date.')
    old = dict(before.song_dates)
    if track_id not in old:
        raise ProtocolError('The test track is missing.')
    timestamp = datetime.fromtimestamp(saved_unix, timezone.utc).replace(tzinfo=None)
    operation = plistlib.dumps({'revision':before.anchor+1, 'timestamp':datetime.now(timezone.utc).replace(tzinfo=None),
        'operations':[{'operation':'update_db_info','pid':before.database_id,
                       'db_info':{'primary_container_pid':before.library_id}},
                      {'operation':'update_track','pid':track_id,
                       'item':{'date_created':timestamp}}]}, fmt=plistlib.FMT_BINARY)
    reply = await negotiate(service, identity, before.anchor)
    if any(x not in ('.','..') for x in await afc.listdir(SYNC_DIR)):
        raise ProtocolError('A prior music sync is pending.')
    await _put_new(afc,METADATA,operation)
    await _put_new(afc,SIGNATURE,sign_metadata(reply,operation))
    await send_frame(service, metadata_finished(before.anchor))
    event = await receive_event(service)
    if event['Command'] == 'AssetManifest':
        for asset in validate_foreign_manifest(event,allowed_foreign):
            await send_frame(service,file_unavailable(asset))
        event = await receive_event(service)
    if event['Command'] != 'SyncFinished':
        raise ProtocolError('The date update did not complete; inspect the phone.')
    data,wal = await stable_capture(afc)
    after = parse_catalog(data,wal)
    if (after.anchor != before.anchor+1 or after.playlists != before.playlists
            or after.song_count != before.song_count):
        raise ProtocolError('Other phone state changed; inspect before continuing.')
    new = dict(after.song_dates)
    if set(old) != set(new) or any(new[k] != old[k] for k in old if k != track_id):
        raise ProtocolError('Another track date changed; inspect before continuing.')
    if new[track_id] != saved_unix - 978307200:
        raise ProtocolError('The phone did not retain the requested original date.')
    return after
