"""AFC staging primitives for the direct sender.

The caller must hold the phone's music sync lock and
a durable pending journal entry before calling any write function here. The
protocol provides no atomic exclusive create; absence checks alone do not
protect against a different client that ignores the lock.
"""
import hashlib
import io
import plistlib
import re

from pymobiledevice3.exceptions import AfcFileNotFoundError
from atc_signing import sign_metadata

SYNC_DIR = '/iTunes_Control/Sync/Media'
METADATA = SYNC_DIR + '/Sync_00000000.plist'
SIGNATURE = METADATA + '.cig'
CHUNK = 1024 * 1024
MAX_AUDIO = 8 * 1024 * 1024 * 1024


class StagingError(Exception):
    pass


async def _absent(afc, path):
    try:
        await afc.stat(path)
    except AfcFileNotFoundError:
        return
    raise StagingError('A staging or destination file already exists; inspect before retrying.')


async def _put_new(afc, path, data):
    if not data or len(data) > 64 * 1024 * 1024:
        raise StagingError('Invalid staged file size.')
    await _put_stream(afc, path, io.BytesIO(data), len(data), hashlib.sha256(data).hexdigest())


async def _put_stream(afc, path, stream, size, expected_sha, readback=True, on_progress=None):
    """Write and read back one file in bounded chunks under the caller's lock."""
    if not 0 < size <= MAX_AUDIO:
        raise StagingError('Invalid staged file size.')
    await _absent(afc, path)
    handle = await afc.fopen(path, 'r+')
    written = 0
    digest = hashlib.sha256()
    try:
        while written < size:
            block = stream.read(min(CHUNK, size - written))
            if not block:
                raise StagingError('The source ended during upload; inspect before retrying.')
            digest.update(block)
            await afc.fwrite(handle, block)
            written += len(block)
            if on_progress is not None:
                await on_progress(written, size)
        if stream.read(1) or digest.hexdigest() != expected_sha:
            raise StagingError('The source changed during upload; inspect before retrying.')
    finally:
        await afc.fclose(handle)
    info = await afc.stat(path)
    if info.get('st_size') != size or info.get('st_ifmt') != 'S_IFREG':
        raise StagingError('Staged file size differs; inspect before retrying.')
    if readback:
        # shortcut: audio readback duplicates the permanent-file check; keep
        # readback for the small signed metadata pair, drop it for audio.
        handle = await afc.fopen(path, 'r')
        read_digest = hashlib.sha256()
        remaining = size
        try:
            while remaining:
                block = await afc.fread(handle, min(CHUNK, remaining))
                if not block or len(block) > remaining:
                    raise StagingError('Staged file is incomplete; inspect before retrying.')
                read_digest.update(block)
                remaining -= len(block)
        finally:
            await afc.fclose(handle)
        if read_digest.hexdigest() != expected_sha:
            raise StagingError('Staged file readback differs; inspect before retrying.')


def _validate_insert(operation, database_id, library_id, track_id, anchor):
    try:
        root = plistlib.loads(operation)
        entries = root['operations']
        if (set(root) != {'revision','timestamp','operations'} or root['revision'] != anchor + 1 or len(entries) != 2
                or entries[0] != {'operation':'update_db_info', 'pid':database_id,
                                  'db_info':{'primary_container_pid':library_id}}
                or set(entries[1]) != {'operation','pid','item','item_stats','avformat_info','location'}
                or entries[1]['operation'] != 'insert_track' or entries[1]['pid'] != track_id):
            raise StagingError('Insert operation does not match the selected phone and song.')
    except StagingError:
        raise
    except Exception as exc:
        raise StagingError('Malformed insert operation.') from exc


async def stage_insert(afc, operation, reply, *, database_id, library_id, track_id, anchor):
    """Stage one signed insert; caller must already own an exclusive sync lock."""
    _validate_insert(operation, database_id, library_id, track_id, anchor)
    names = await afc.listdir(SYNC_DIR)
    if any(name not in ('.', '..') for name in names):
        raise StagingError('The phone has another pending music operation; do not overwrite it.')
    signature = sign_metadata(reply, operation)
    await _put_new(afc, METADATA, operation)
    await _put_new(afc, SIGNATURE, signature)


async def stage_playlist_update(afc, operation, reply, *, database_id, library_id,
                                playlist_id, expected_name, existing, desired, anchor):
    """Stage a single full-list update; caller owns the sync lock and journal."""
    try:
        root = plistlib.loads(operation)
        entries = root['operations']
        update = entries[1]
        if (root['revision'] != anchor + 1 or len(entries) != 2
                or entries[0] != {'operation':'update_db_info','pid':database_id,
                                   'db_info':{'primary_container_pid':library_id}}
                or update['operation'] != 'update_playlist' or update['pid'] != playlist_id
                or set(update) != {'operation','pid','container','container_ui','item_to_container'}
                or set(update['container']) != {'name','date_created','date_modified'}
                or update['container_ui'] != {'play_order':1,'album_field_order':0,'repeat_mode':0,
                    'is_reversed':False,'shuffle_items':False,'has_been_shuffled':False}
                or playlist_id == library_id or update['container']['name'] != expected_name
                or update['item_to_container'] != desired
                or len(set(desired)) != len(desired) or not set(existing).issubset(desired)):
            raise StagingError('Playlist update does not match the selected playlist or would remove members.')
    except StagingError:
        raise
    except Exception as exc:
        raise StagingError('Malformed playlist update.') from exc
    names = await afc.listdir(SYNC_DIR)
    if any(name not in ('.', '..') for name in names):
        raise StagingError('The phone has another pending music operation; do not overwrite it.')
    signature = sign_metadata(reply, operation)
    await _put_new(afc, METADATA, operation)
    await _put_new(afc, SIGNATURE, signature)


async def stage_playlist_create(afc, operation, reply, *, database_id, library_id,
                                playlist_id, expected_name, desired, anchor):
    """Stage an additive test playlist; caller owns the sync lock and journal."""
    try:
        root = plistlib.loads(operation)
        entries = root['operations']
        create = entries[1]
        if (set(root) != {'revision','timestamp','operations'} or root['revision'] != anchor + 1
                or len(entries) != 2
                or entries[0] != {'operation':'update_db_info','pid':database_id,
                                  'db_info':{'primary_container_pid':library_id}}
                or create['operation'] != 'insert_playlist' or create['pid'] != playlist_id
                or playlist_id == library_id
                or set(create) != {'operation','pid','container','container_ui','item_to_container'}
                or set(create['container']) != {'name','date_created','date_modified'}
                or create['container']['name'] != expected_name
                or create['container_ui'] != {'play_order':1,'album_field_order':0,'repeat_mode':0,
                    'is_reversed':False,'shuffle_items':False,'has_been_shuffled':False}
                or create['item_to_container'] != desired or len(set(desired)) != len(desired)):
            raise StagingError('Test playlist operation did not match the prepared plan.')
    except StagingError:
        raise
    except Exception as exc:
        raise StagingError('Malformed test playlist operation.') from exc
    names = await afc.listdir(SYNC_DIR)
    if any(name not in ('.', '..') for name in names):
        raise StagingError('The phone has another pending music operation; do not overwrite it.')
    signature = sign_metadata(reply, operation)
    await _put_new(afc, METADATA, operation)
    await _put_new(afc, SIGNATURE, signature)


async def stage_audio(afc, *, asset_id, extension, stream, size, expected_sha, permanent_path, cover=None, on_progress=None):
    """Stage one requested asset; caller must already own an exclusive sync lock."""
    await check_audio_paths(afc, asset_id=asset_id, extension=extension,
                            permanent_path=permanent_path, has_cover=cover is not None)
    audio_base = '/Airlock/Media/' + asset_id
    artwork_path = '/Airlock/Media/Artwork/' + asset_id
    if cover is not None:
        await _put_new(afc, artwork_path, cover)
    # The staged copy is size-checked; the phone's permanent file is hashed
    # byte for byte by verify_insert after the session completes.
    await _put_stream(afc, audio_base + extension, stream, size, expected_sha, readback=False, on_progress=on_progress)
    await _absent(afc, audio_base)
    await afc.rename(audio_base + extension, audio_base)


async def check_audio_paths(afc, *, asset_id, extension, permanent_path, has_cover):
    """Check all file targets before the metadata is committed."""
    if (not re.fullmatch(r'-?[1-9][0-9]{0,18}', asset_id)
            or extension not in ('.mp3', '.m4a')
            or not re.fullmatch(r'iTunes_Control/Music/F(?:[0-3][0-9]|4[0-9])/[A-Z]{4}\.(?:mp3|m4a)', permanent_path)
            or not permanent_path.endswith(extension)):
        raise StagingError('Unsupported audio asset identity or destination.')
    audio_base = '/Airlock/Media/' + asset_id
    artwork_path = '/Airlock/Media/Artwork/' + asset_id
    directories = ['/Airlock/Media', '/' + permanent_path.rsplit('/',1)[0]]
    if has_cover:
        directories.append('/Airlock/Media/Artwork')
    for directory in directories:
        try:
            info = await afc.stat(directory)
        except AfcFileNotFoundError as exc:
            raise StagingError('The phone music staging directory is missing; no transfer was started.') from exc
        if info.get('st_ifmt') != 'S_IFDIR':
            raise StagingError('A phone music staging path is not a directory.')
    await _absent(afc, '/' + permanent_path)
    await _absent(afc, audio_base)
    await _absent(afc, audio_base + extension)
    if has_cover:
        await _absent(afc, artwork_path)
