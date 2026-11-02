"""One AirTraffic session carrying many prepared song inserts."""
import base64
import asyncio
import plistlib
import time
from datetime import datetime, timezone

from atc_protocol import (ProtocolError, file_complete, file_progress, file_unavailable, metadata_finished,
                          negotiate, receive_event, send_frame, validate_batch_manifest)
from atc_staging import SYNC_DIR, METADATA, SIGNATURE, _put_new, check_audio_paths, stage_audio
from atc_signing import sign_metadata
from direct_audio import phone_cover

MAX_INSERTS = 499


def build_operation(entries, *, database_id, library_id, anchor, now=None):
    """Combine each entry's prepared insert into one session payload."""
    if not entries or len(entries) > MAX_INSERTS:
        raise ProtocolError('Invalid batch size.')
    if not database_id or not library_id or not isinstance(anchor,int) or isinstance(anchor,bool) or not 0 <= anchor < 2**32-1:
        raise ProtocolError('Invalid phone library identity or anchor.')
    inserts = []
    seen_ids, seen_paths = set(), set()
    for entry in entries:
        plan = entry['plan']
        try:
            per_song = plistlib.loads(base64.b64decode(plan['operation'], validate=True))
            own = per_song['operations'][1]
        except Exception as exc:
            raise ProtocolError('A prepared song operation is malformed.') from exc
        db_update={'operation':'update_db_info','pid':database_id,'db_info':{'primary_container_pid':library_id}}
        if (set(per_song) != {'revision','timestamp','operations'} or per_song.get('revision') != anchor + 1
                or len(per_song.get('operations', ())) != 2 or per_song['operations'][0] != db_update
                or set(own) != {'operation','pid','item','item_stats','avformat_info','location'}
                or own.get('operation') != 'insert_track' or own.get('pid') != int(plan['track_id'])
                or plan.get('anchor') != anchor or plan.get('database_id') != database_id
                or plan.get('library_id') != library_id
                or plan['track_id'] in seen_ids or plan['path'].casefold() in seen_paths):
            raise ProtocolError('A prepared song does not match the planned session.')
        seen_ids.add(plan['track_id'])
        seen_paths.add(plan['path'].casefold())
        inserts.append(own)
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)
    operations = ([{'operation':'update_db_info','pid':database_id,
                    'db_info':{'primary_container_pid':library_id}}] + inserts)
    return plistlib.dumps({'revision':anchor+1,'timestamp':timestamp,'operations':operations},
                          fmt=plistlib.FMT_BINARY, sort_keys=True)


async def send_batch(service, afc, *, identity, database_id, library_id, anchor,
                     entries, allowed_foreign=(), on_asset=None):
    """Stage every insert, then deliver assets in planned oldest-first order.

    The caller holds the phone's sync lock and has journaled every song as
    pending. A stop request cannot interrupt a batch once its metadata is
    staged; check for stop between batches.
    """
    covers, own_parts = {}, {}
    for entry in entries:
        plan, source = entry['plan'], entry['source']
        cover = phone_cover(source)
        if cover and not plan.get('artwork_id'):
            raise ProtocolError('This cover cannot yet be verified exactly; no import was started.')
        covers[plan['track_id']] = cover
        own_parts[plan['track_id']] = {3, 7} if cover else {1, 5}
        await check_audio_paths(afc, asset_id=plan['track_id'], extension=source.path.suffix.lower(),
                                permanent_path=plan['path'], has_cover=cover is not None)
    operation = build_operation(entries, database_id=database_id, library_id=library_id, anchor=anchor)
    if any(name not in ('.','..') for name in await afc.listdir(SYNC_DIR)):
        raise ProtocolError('The phone has another pending music operation.')
    reply = await negotiate(service, identity, anchor)
    await _put_new(afc, METADATA, operation)
    await _put_new(afc, SIGNATURE, sign_metadata(reply, operation))
    await send_frame(service, metadata_finished(anchor))
    message = await receive_event(service)
    if message['Command'] != 'AssetManifest':
        raise ProtocolError('The phone did not request the planned songs; inspect before retrying.')
    foreign = validate_batch_manifest(message, own_parts, allowed_foreign)
    # The phone can ping while audio is being staged over AFC. Keep a single
    # ATC reader alive throughout the transfer so pings receive a prompt reply.
    send_lock = asyncio.Lock()
    reader = asyncio.create_task(receive_event(service, timeout=None, send_lock=send_lock))
    try:
        for position, entry in enumerate(entries):
            asset_id = entry['plan']['track_id']
            plan, source = entry['plan'], entry['source']
            before = source.path.stat()
            if before.st_size <= 0:
                raise ProtocolError('The source file is empty.')
            if on_asset is not None:
                on_asset(asset_id, entry)
            last_progress = 0.0
            async def on_progress(written, size):
                nonlocal last_progress
                now = time.monotonic()
                if last_progress and now - last_progress < 2 and written < size:
                    return
                async with send_lock:
                    await send_frame(service, file_progress(written / size, (position + written / size) / len(entries)))
                last_progress = now
            with source.path.open('rb') as stream:
                staged = asyncio.create_task(stage_audio(afc, asset_id=asset_id,
                    extension=source.path.suffix.lower(), stream=stream, size=before.st_size,
                    expected_sha=entry['sha'], permanent_path=plan['path'], cover=covers[asset_id],
                    on_progress=on_progress))
                try:
                    done, _ = await asyncio.wait((staged, reader), return_when=asyncio.FIRST_COMPLETED)
                    if reader in done:
                        terminal = reader.result()
                        raise ProtocolError('The phone ended the sync before the audio upload completed: ' + terminal['Command'])
                    await staged
                finally:
                    if not staged.done():
                        staged.cancel()
                        await asyncio.gather(staged, return_exceptions=True)
            after = source.path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ProtocolError(f'The source changed during upload; inspect {source.path.name}.')
            async with send_lock:
                await send_frame(service, file_complete(asset_id, plan['path']))
        for asset_id in foreign:
            async with send_lock:
                await send_frame(service, file_unavailable(asset_id))
        terminal = await asyncio.wait_for(reader, 30)
        if terminal['Command'] != 'SyncFinished':
            raise ProtocolError('The phone did not confirm the batch; inspect before retrying.')
        return [entry['plan']['track_id'] for entry in entries]
    finally:
        if not reader.done():
            reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
