"""Windows direct adapter; callers hold the phone sync lock during writes."""
import base64
import asyncio
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from artwork_inventory import parse as parse_artwork, reserve as reserve_artwork
from atc_send_playlist import update_playlist
from atc_send_batch import send_batch
from atc_staging import check_audio_paths, StagingError
from direct_audio import inspect
from direct_inventory import AllocationState, parse_inventory, apply_spelling, fresh_id
from automatic_playlists import match_playlist, check_folder_names
from atc_create_test_playlist import create as create_playlist
from direct_phone import (PhoneReadError, _stat_optional, identity_from_afc,
                          parse_catalog, stable_capture)
from direct_verify import verify_insert
from direct_verify import verify_inserts, preserved_rows, verify_preserved
from playlist_operation_preview import _signed_pid
from track_operation_preview import insert_preview
from music_importer import file_date_ns

ARTWORK_DB = '/iTunes_Control/Artwork/ArtworkDB'


class DirectAdapter:
    def __init__(self, serial, client, afc, metadata_workers=4, readback_workers=4, fast_existing=False):
        if not serial:
            raise PhoneReadError('Choose the destination phone.')
        self.serial, self.client, self.afc = serial, client, afc
        if isinstance(metadata_workers, bool) or not isinstance(metadata_workers, int) or not 1 <= metadata_workers <= 16:
            raise PhoneReadError('Choose 1 to 16 metadata workers.')
        if isinstance(readback_workers, bool) or not isinstance(readback_workers, int) or not 1 <= readback_workers <= 8:
            raise PhoneReadError('Choose 1 to 8 phone readback workers.')
        self.metadata_workers, self.readback_workers = metadata_workers, readback_workers
        self.fast_existing = fast_existing
        self.expected_identity = None
        self.current_batch = None
        self.verified_ids = set()
        self._inspect_cache = {}
        self.before_batch_rows = None

    def _device(self, serial):
        if serial != self.serial:
            raise PhoneReadError('The selected destination changed; stop the import.')

    async def _snapshot(self):
        data, wal = await stable_capture(self.afc)
        return parse_catalog(data, wal), parse_inventory(data, wal)

    async def _artwork(self):
        info = await _stat_optional(self.afc, ARTWORK_DB)
        if info is None:
            raise PhoneReadError('The saved artwork allocation database is missing.')
        attrs = dict(info)
        size = attrs['st_size']
        if attrs['st_ifmt'] != 'S_IFREG' or not 132 <= size <= 32*1024*1024:
            raise PhoneReadError('Unsupported saved artwork database.')
        handle = await self.afc.fopen(ARTWORK_DB)
        try:
            data = await self.afc.fread(handle, size + 1)
        finally:
            await self.afc.fclose(handle)
        if len(data) != size or await _stat_optional(self.afc, ARTWORK_DB) != info:
            raise PhoneReadError('Artwork database changed during preparation; retry.')
        return parse_artwork(data)

    async def validate_device(self, serial, destinations, verified_tracks):
        self._device(serial)
        self.expected_identity = await identity_from_afc(self.afc)
        catalog, _ = await self._snapshot()
        existing = [str(track_id) for track_id, _ in catalog.song_dates]
        if sorted(existing) != sorted(verified_tracks):
            raise PhoneReadError('The phone contains music not verified by this importer; review it before importing.')
        self.verified_ids = set(verified_tracks)
        available = {str(p.id) for p in catalog.playlists}
        if not set(destinations).issubset(available):
            raise PhoneReadError('A selected playlist changed or is not available.')
        return {str(p.id):tuple(map(str,p.members)) for p in catalog.playlists if str(p.id) in destinations}

    async def prepare_batch(self, serial, songs_with_shas):
        """Plan a whole batch against one snapshot; plans share one anchor."""
        self._device(serial)
        self._inspect_cache.clear()
        for song, _ in songs_with_shas:
            if file_date_ns(song.path) != song.file_date_ns:
                raise PhoneReadError('A source file date changed after planning; refresh and retry.')
        if await identity_from_afc(self.afc) != self.expected_identity:
            raise PhoneReadError('The phone music library identity changed.')
        data,wal=await stable_capture(self.afc)
        catalog,inventory=parse_catalog(data,wal),parse_inventory(data,wal)
        if sorted(str(track_id) for track_id,_ in catalog.song_dates) != sorted(self.verified_ids):
            raise PhoneReadError('The phone music library changed before preparing a batch.')
        self.before_batch_rows=preserved_rows(data,wal,self.verified_ids)
        state = AllocationState(inventory)
        artwork_state = None
        reserved_artwork = set()
        chosen = set(inventory.used_paths)
        plans = []
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=self.metadata_workers, thread_name_prefix='batch-metadata') as pool:
            inspected = []
            for start in range(0, len(songs_with_shas), self.metadata_workers):
                inspected.extend(await asyncio.gather(*(loop.run_in_executor(pool,self._inspect,song)
                    for song,_ in songs_with_shas[start:start+self.metadata_workers])))
        for (song, sha), raw in zip(songs_with_shas, inspected):
            track, album, artist, source, overrides = state.allocate(raw)
            artwork = None
            if source.cover:
                if artwork_state is None:
                    artwork_state = await self._artwork()
                artwork = reserve_artwork(artwork_state[0], artwork_state[1],
                                          inventory.artwork_ids, reserved_artwork)
                reserved_artwork.add(artwork)
            signed_track = _signed_pid(track)
            extension = source.path.suffix.lower()
            for _ in range(128):
                bucket = secrets.randbelow(50)
                name = ''.join(chr(65 + secrets.randbelow(26)) for _ in range(4))
                path = f'iTunes_Control/Music/F{bucket:02d}/{name}{extension}'
                if path.casefold() in chosen:
                    continue
                try:
                    await check_audio_paths(self.afc, asset_id=str(signed_track), extension=extension,
                                            permanent_path=path, has_cover=bool(source.cover))
                except StagingError:
                    continue
                chosen.add(path.casefold())
                break
            else:
                raise PhoneReadError('Could not find an unused music destination path.')
            operation = insert_preview(source, database_id=catalog.database_id & (2**64-1),
                library_id=catalog.library_id & (2**64-1), anchor=catalog.anchor, track_id=track,
                album_id=album, artist_id=artist, artwork_id=artwork,
                timestamp=datetime.now(timezone.utc))
            plans.append({'sha':sha, 'anchor':catalog.anchor, 'database_id':catalog.database_id,
                'library_id':catalog.library_id, 'track_id':str(signed_track), 'artwork_id':artwork,
                'path':path, 'operation':base64.b64encode(operation).decode('ascii'),
                'source_size':song.size, 'source_mtime_ns':song.modified_ns,
                'spelling_overrides':overrides})
        self.current_batch = plans
        return plans

    def _inspect(self, song):
        stat = song.path.stat()
        key = (str(song.path), stat.st_size, stat.st_mtime_ns)
        if key not in self._inspect_cache:
            source = inspect(song.path)
            after = song.path.stat()
            if (after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                raise PhoneReadError('A source file changed during metadata inspection.')
            self._inspect_cache[key] = source
        return self._inspect_cache[key]

    async def upload_batch(self, serial, entries, on_asset=None):
        self._device(serial)
        plans = [entry['plan'] for entry in entries]
        if plans != self.current_batch:
            raise PhoneReadError('The upload plan changed; stop before sending.')
        if any(entry['sha'] != plan.get('sha') for entry, plan in zip(entries, plans)):
            raise PhoneReadError('The upload plan changed; stop before sending.')
        for entry in entries:
            if file_date_ns(entry['song'].path) != entry['song'].file_date_ns:
                raise PhoneReadError('A source file date changed before upload.')
        if await identity_from_afc(self.afc) != self.expected_identity:
            raise PhoneReadError('The phone music library identity changed.')
        anchor = plans[0]['anchor']
        if any(plan['anchor'] != anchor for plan in plans):
            raise PhoneReadError('The batch does not share one planned anchor.')
        if self.before_batch_rows is None:
            raise PhoneReadError('The batch has no saved before-state.')
        entries = [dict(entry, source=apply_spelling(self._inspect(entry['song']),
                          entry['plan'].get('spelling_overrides', {}))) for entry in entries]
        service = await self.client.start_lockdown_service('com.apple.atc')
        try:
            track_ids = await send_batch(service, self.afc, identity=self.expected_identity,
                database_id=plans[0]['database_id'], library_id=plans[0]['library_id'], anchor=anchor,
                entries=entries,
                allowed_foreign={(track_id, part) for track_id in self.verified_ids for part in (2,4)},
                on_asset=on_asset)
        finally:
            await service.close()
        after_data,after_wal=await stable_capture(self.afc)
        after_catalog=parse_catalog(after_data,after_wal)
        parse_inventory(after_data,after_wal)  # catches duplicate IDs before the next batch
        expected=set(self.verified_ids) | set(track_ids)
        if (after_catalog.anchor != anchor+1 or
                sorted(str(pid) for pid,_ in after_catalog.song_dates) != sorted(expected)):
            raise PhoneReadError('The phone library differs from the planned batch; inspect before continuing.')
        verify_preserved(self.before_batch_rows,preserved_rows(after_data,after_wal,self.verified_ids))
        items = []
        for entry, track_id in zip(entries, track_ids):
            plan, song, sha = entry['plan'], entry['song'], entry['sha']
            source = apply_spelling(self._inspect(song), plan.get('spelling_overrides', {}))
            items.append((int(track_id), source, plan['path'], sha, plan['artwork_id']))
        await verify_inserts(self.afc, items, workers=self.readback_workers)
        self.verified_ids.update(str(track_id) for track_id in track_ids)
        return list(track_ids)

    async def verify_song(self, serial, track_id, song, sha, plan):
        self._device(serial)
        if file_date_ns(song.path) != song.file_date_ns:
            raise PhoneReadError('The source file date changed after planning.')
        if plan['track_id'] != str(track_id) or plan['sha'] != sha:
            raise PhoneReadError('A previous song has an inconsistent journal entry.')
        source = apply_spelling(self._inspect(song), plan.get('spelling_overrides',{}))
        verified = await verify_insert(self.afc, int(track_id), source, plan['path'],
                                       sha, plan['artwork_id'])
        if verified:
            self.verified_ids.add(str(track_id))
        return verified

    async def verify_existing(self, serial, entries, progress, stop):
        """Inspect local files concurrently; use one phone snapshot for readback."""
        self._device(serial)
        loop=asyncio.get_running_loop()
        pool=ThreadPoolExecutor(max_workers=self.metadata_workers, thread_name_prefix='music-metadata')
        try:
            def prepare(entry):
                song,track_id,sha,plan=entry
                if file_date_ns(song.path) != song.file_date_ns:
                    raise PhoneReadError('The source file date changed after planning.')
                if plan['track_id'] != str(track_id) or plan['sha'] != sha:
                    raise PhoneReadError('A previous song has an inconsistent journal entry.')
                source=apply_spelling(inspect(song.path),plan.get('spelling_overrides',{}))
                return (int(track_id),source,plan['path'],sha,plan['artwork_id'])
            # Limit queued work to a small window, and preserve input order.
            items=[]
            for start in range(0,len(entries),self.metadata_workers):
                if stop.is_set():
                    raise PhoneReadError('Stopped while checking songs already on the phone.')
                items.extend(await asyncio.gather(*(loop.run_in_executor(pool,prepare,entry)
                                                    for entry in entries[start:start+self.metadata_workers])))
                progress(len(items),len(entries),entries[len(items)-1][0].path.name,phase='checking_metadata')
            await verify_inserts(self.afc,items,progress=progress,stop=stop,workers=self.readback_workers,
                                 fast_existing=self.fast_existing)
            self.verified_ids.update(str(item[0]) for item in items)
        finally:
            await asyncio.to_thread(pool.shutdown,wait=True,cancel_futures=True)

    async def validate_playlist_update(self, serial, destination, desired):
        self._device(serial)
        catalog, _ = await self._snapshot()
        matches = [p for p in catalog.playlists if str(p.id) == str(destination)]
        if len(matches) != 1 or not set(map(str, matches[0].members)).issubset(set(desired)):
            raise PhoneReadError('The selected playlist changed or contains unrelated songs.')

    async def resolve_playlist_targets(self, serial, names):
        """Resolve all folder names against one fresh snapshot before upload."""
        self._device(serial)
        check_folder_names(names)
        catalog,inventory=await self._snapshot()
        occupied=set(inventory.ids)
        result=[]
        for name in names:
            existing=match_playlist(catalog,name)
            if existing:
                result.append((str(existing.id),False))
            else:
                new=fresh_id(occupied)
                occupied.add(new)
                result.append((str(_signed_pid(new)),True))
        return result

    async def create_playlist(self, serial, destination, name, desired):
        self._device(serial)
        catalog,inventory=await self._snapshot()
        if match_playlist(catalog,name) is not None:
            raise PhoneReadError('The playlist appeared after planning; refresh and retry.')
        new=int(destination)
        if (new & (2**64-1)) in inventory.ids:
            raise PhoneReadError('The reserved playlist identity is already in use.')
        service=await self.client.start_lockdown_service('com.apple.atc')
        try:
            await create_playlist(service,self.afc,identity=self.expected_identity,catalog=catalog,
                playlist_id=new & (2**64-1),name=name,track_ids=[int(i) for i in desired],
                allowed_foreign={(track_id,part) for track_id in self.verified_ids for part in (2,4)})
        finally:
            await service.close()

    async def update_playlist(self, serial, destination, desired):
        self._device(serial)
        catalog, _ = await self._snapshot()
        service = await self.client.start_lockdown_service('com.apple.atc')
        try:
            await update_playlist(service, self.afc, identity=self.expected_identity, catalog=catalog,
                                  playlist_id=int(destination), desired_ids=desired,
                                  allowed_foreign={(track_id, part) for track_id in self.verified_ids for part in (2,4)})
        finally:
            await service.close()

    async def verify_playlist(self, serial, destination, desired):
        self._device(serial)
        catalog, _ = await self._snapshot()
        matches = [p for p in catalog.playlists if str(p.id) == str(destination)]
        return len(matches) == 1 and list(map(str, matches[0].members)) == list(desired)
