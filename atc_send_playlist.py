"""One complete ordered playlist update, not yet connected to the GUI.

The caller must hold the selected phone's sync lock, use a fresh catalog and
journal the pending operation before invoking this function. Unknown outcomes
must be reconciled instead of retried.
"""
from datetime import datetime, timedelta, timezone

from atc_protocol import (ProtocolError, file_unavailable, metadata_finished, negotiate,
                          receive_event, send_frame, validate_foreign_manifest)
from atc_staging import stage_playlist_update
from direct_phone import stable_capture, parse_catalog
from playlist_operation_preview import update_playlist_preview


def unsigned(value):
    if isinstance(value, bool) or not isinstance(value, int) or not -(2**63) <= value < 2**63 or value == 0:
        raise ProtocolError('Invalid phone music identity.')
    return value & (2**64 - 1)


async def update_playlist(service, afc, *, identity, catalog, playlist_id,
                          desired_ids, allowed_foreign=()):
    matches = [p for p in catalog.playlists if p.id == playlist_id]
    if len(matches) != 1:
        raise ProtocolError('The selected phone playlist was not found uniquely.')
    target = matches[0]
    desired = [int(value) for value in desired_ids]
    for item in desired:
        unsigned(item)
    if len(set(desired)) != len(desired) or not set(target.members).issubset(desired):
        raise ProtocolError('The playlist update would remove or duplicate existing songs.')
    if [item for item in desired if item in set(target.members)] != list(target.members):
        raise ProtocolError('The update would reorder existing playlist members.')
    known = [wire_id for wire_id, _ in catalog.song_dates]
    if any(item not in known for item in desired):
        raise ProtocolError('A requested playlist song is missing from the phone catalog.')
    created = datetime(2001,1,1,tzinfo=timezone.utc) + timedelta(seconds=target.created)
    operation = update_playlist_preview(database_id=unsigned(catalog.database_id),
        library_id=unsigned(catalog.library_id), anchor=catalog.anchor,
        playlist_id=unsigned(playlist_id), name=target.name,
        ordered_track_ids=[unsigned(v) for v in desired],
        existing_track_ids=[unsigned(v) for v in target.members],
        when=datetime.now(timezone.utc), created=created)
    reply = await negotiate(service, identity, catalog.anchor)
    await stage_playlist_update(afc, operation, reply, database_id=catalog.database_id,
        library_id=catalog.library_id, playlist_id=playlist_id, expected_name=target.name,
        existing=list(target.members), desired=desired, anchor=catalog.anchor)
    await send_frame(service, metadata_finished(catalog.anchor))
    event = await receive_event(service)
    if event['Command'] == 'AssetManifest':
        for foreign in validate_foreign_manifest(event, allowed_foreign):
            await send_frame(service, file_unavailable(foreign))
        event = await receive_event(service)
    if event['Command'] != 'SyncFinished':
        raise ProtocolError('The phone did not confirm the playlist update; inspect before retrying.')
    database, wal = await stable_capture(afc)
    after = parse_catalog(database, wal)
    actual = [p for p in after.playlists if p.id == playlist_id]
    if len(actual) != 1 or list(actual[0].members) != desired or actual[0].name != target.name:
        raise ProtocolError('The playlist order could not be verified; inspect before retrying.')
    before_others = {p.id:p for p in catalog.playlists if p.id != playlist_id}
    after_others = {p.id:p for p in after.playlists if p.id != playlist_id}
    if before_others != after_others or catalog.song_dates != after.song_dates:
        raise ProtocolError('Other playlists or song dates changed; inspect before continuing.')
    return True
