"""One-song AirTraffic transaction, not yet connected to the GUI.

The caller must first select the phone, hold its exclusive sync lock, verify a
fresh catalog and persist an uncertain-operation journal record. Any exception
after staging requires reconciliation and no blind retry. This module does not
select a device or acquire its lock.
"""
import hashlib

from atc_protocol import (ProtocolError, file_complete, file_unavailable, metadata_finished,
                          negotiate, receive_event, send_frame, validate_manifest)
from atc_staging import MAX_AUDIO, CHUNK, check_audio_paths, stage_audio, stage_insert
from direct_verify import verify_insert
from direct_audio import phone_cover


async def send_one(service, afc, *, identity, catalog, source, expected_sha,
                   signed_track_id, artwork_id, operation, permanent_path,
                   allowed_foreign=()):
    """Stage one already-planned song and verify its audio and cover bytes."""
    # The source must be held or re-statted by the caller as part of its journal.
    before = source.path.stat()
    if before.st_size <= 0 or before.st_size > MAX_AUDIO:
        raise ProtocolError('The source file size is unsupported.')
    cover = phone_cover(source)
    if cover and not artwork_id:
        raise ProtocolError('This cover cannot yet be verified exactly; no import was started.')
    asset_id = str(signed_track_id)
    extension = source.path.suffix.lower()
    await check_audio_paths(afc, asset_id=asset_id, extension=extension,
                            permanent_path=permanent_path, has_cover=cover is not None)
    with source.path.open('rb') as stream:
        digest=hashlib.sha256()
        for block in iter(lambda:stream.read(CHUNK), b''):
            digest.update(block)
        after=source.path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ProtocolError('The source file changed before the upload.')
        if digest.hexdigest() != expected_sha:
            raise ProtocolError('The source file no longer matches the planned song.')
        stream.seek(0)
        reply = await negotiate(service, identity, catalog.anchor)
        await stage_insert(afc, operation, reply, database_id=catalog.database_id,
                           library_id=catalog.library_id, track_id=signed_track_id, anchor=catalog.anchor)
        await send_frame(service, metadata_finished(catalog.anchor))
        message = await receive_event(service)
        parts = {3, 7} if cover else {1, 5}
        foreign = validate_manifest(message, asset_id, parts, allowed_foreign)
        await stage_audio(afc, asset_id=asset_id, extension=extension, stream=stream,
                          size=before.st_size, expected_sha=expected_sha,
                          permanent_path=permanent_path, cover=cover)
        after=source.path.stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
            raise ProtocolError('The source changed during upload; inspect before retrying.')
    await send_frame(service, file_complete(asset_id, permanent_path))
    for foreign_id in foreign:
        await send_frame(service, file_unavailable(foreign_id))
    terminal = await receive_event(service)
    if terminal['Command'] != 'SyncFinished':
        raise ProtocolError('The phone did not confirm the upload; inspect before retrying.')
    await verify_insert(afc, signed_track_id, source, permanent_path, expected_sha, artwork_id)
    return str(signed_track_id)
