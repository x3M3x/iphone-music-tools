"""Add exactly one temporary playlist for an explicitly authorized test."""
from datetime import datetime, timezone

from atc_protocol import (ProtocolError, file_unavailable, metadata_finished, negotiate,
                          receive_event, send_frame, validate_foreign_manifest)
from atc_staging import stage_playlist_create
from direct_phone import parse_catalog, stable_capture
from playlist_operation_preview import create_playlist_preview, _signed_pid
from automatic_playlists import match_playlist


async def create(service, afc, *, identity, catalog, playlist_id, name, track_id=None,
                 track_ids=None, allowed_foreign=()):
    if match_playlist(catalog,name) is not None:
        raise ProtocolError('The playlist already exists; inspect it before retrying.')
    desired = list(track_ids) if track_ids is not None else [track_id]
    if len(set(desired)) != len(desired):
        raise ProtocolError('A new playlist requires distinct verified tracks.')
    if any(sum(pid == track for pid, _ in catalog.song_dates) != 1 for track in desired):
        raise ProtocolError('A playlist song was not found uniquely on the phone.')
    unsigned = lambda value: value & (2**64 - 1)
    operation = create_playlist_preview(database_id=unsigned(catalog.database_id),
        library_id=unsigned(catalog.library_id), anchor=catalog.anchor, playlist_id=playlist_id,
        name=name, ordered_track_ids=[unsigned(track) for track in desired],
        occupied_playlist_ids=[unsigned(p.id) for p in catalog.playlists],
        when=datetime.now(timezone.utc))
    signed_id = _signed_pid(playlist_id)
    reply = await negotiate(service, identity, catalog.anchor)
    current_db,current_wal=await stable_capture(afc)
    if parse_catalog(current_db,current_wal) != catalog:
        raise ProtocolError('The phone library changed while creating the playlist; review before retrying.')
    await stage_playlist_create(afc, operation, reply, database_id=catalog.database_id,
        library_id=catalog.library_id, playlist_id=signed_id, expected_name=name,
        desired=desired, anchor=catalog.anchor)
    await send_frame(service, metadata_finished(catalog.anchor))
    event = await receive_event(service)
    if event['Command'] == 'AssetManifest':
        for asset in validate_foreign_manifest(event, allowed_foreign):
            await send_frame(service, file_unavailable(asset))
        event = await receive_event(service)
    if event['Command'] != 'SyncFinished':
        raise ProtocolError('The playlist result is uncertain; inspect before retrying.')
    database, wal = await stable_capture(afc)
    after = parse_catalog(database, wal)
    matches = [p for p in after.playlists if p.id == signed_id]
    if len(matches) != 1 or matches[0].name != name or matches[0].members != tuple(desired):
        raise ProtocolError('The new playlist could not be verified; inspect before retrying.')
    others = {p.id:p for p in after.playlists if p.id != signed_id}
    from collections import Counter
    names_before=Counter(catalog.all_playlist_names)
    names_after=Counter(after.all_playlist_names)
    if after.all_playlist_names:
        names_after.subtract([(signed_id,name)])
        names_after += Counter()
    if (others != {p.id:p for p in catalog.playlists} or after.song_dates != catalog.song_dates
            or (catalog.all_playlist_names and names_after != names_before)):
        raise ProtocolError('Other music or playlists changed; inspect before continuing.')
    return signed_id
