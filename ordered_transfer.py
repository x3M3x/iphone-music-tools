"""Sequential import coordinator with a durable, fail-closed journal.

The transport adapter is injected. This module does not connect to or write to
an iPhone. An adapter must upload and read back one song, and confirm playlist
order, before returning success.
"""
import asyncio
import json
import os
import sqlite3
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from music_importer import ImportErrorSafe, content_hash, newest_first

CHECK_WORKERS = 4


async def ordered_hashes(songs, stop, workers=CHECK_WORKERS):
    """Hash a bounded number of local files concurrently, yielding in order."""
    loop = asyncio.get_running_loop()
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 16:
        raise ImportErrorSafe('Choose 1 to 16 hash workers.')
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='music-hash')
    pending = deque()
    iterator = iter(songs)

    def submit_one():
        song = next(iterator, None)
        if song is None:
            return False
        pending.append(loop.run_in_executor(pool, content_hash, song))
        return True

    try:
        for _ in range(workers):
            if stop.is_set() or not submit_one():
                break
        while pending and not stop.is_set():
            digest = await pending.popleft()
            if stop.is_set():
                break
            yield digest
            if not stop.is_set():
                submit_one()
    finally:
        for future in pending:
            future.cancel()
        # Running hashes are local reads; wait for them before returning so a
        # stopped run never leaves worker threads checking files in background.
        await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)


class RunLock:
    """Prevent two instances of this importer from using the same journal."""
    def __init__(self, path):
        self.path = Path(str(path) + '.lock')
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == 'nt':
                import msvcrt
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self.fd)
            self.fd = None
            raise ImportErrorSafe('Another import is already running for this journal.') from exc
        return self

    def __exit__(self, *_):
        if self.fd is not None:
            try:
                if os.name == 'nt':
                    import msvcrt
                    os.lseek(self.fd, 0, os.SEEK_SET)
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


class Journal:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS uploads (
                device TEXT NOT NULL, sha TEXT NOT NULL, status TEXT NOT NULL,
                track_id TEXT, path TEXT NOT NULL, plan TEXT NOT NULL, PRIMARY KEY(device, sha));
            CREATE TABLE IF NOT EXISTS playlists (
                device TEXT NOT NULL, playlist_id TEXT NOT NULL, plan TEXT NOT NULL,
                status TEXT NOT NULL, PRIMARY KEY(device, playlist_id));
            CREATE TABLE IF NOT EXISTS date_updates (
                device TEXT NOT NULL, track_id TEXT NOT NULL, plan TEXT NOT NULL,
                status TEXT NOT NULL, PRIMARY KEY(device, track_id));
            CREATE TABLE IF NOT EXISTS album_repairs (
                device TEXT NOT NULL, track_id TEXT NOT NULL, plan TEXT NOT NULL,
                status TEXT NOT NULL, PRIMARY KEY(device, track_id));
        ''')
        self.db.commit()

    def close(self):
        self.db.close()

    def song(self, device, sha):
        return self.db.execute('SELECT status,track_id,plan FROM uploads WHERE device=? AND sha=?',
                               (device, sha)).fetchone()

    def has_uncertain_operations(self, device):
        return bool(self.db.execute('SELECT 1 FROM uploads WHERE device=? AND status != ? LIMIT 1',
                                    (device, 'verified')).fetchone() or
                    self.db.execute('SELECT 1 FROM playlists WHERE device=? AND status != ? LIMIT 1',
                                    (device, 'verified')).fetchone() or
                    self.db.execute('SELECT 1 FROM date_updates WHERE device=? AND status != ? LIMIT 1',
                                    (device, 'verified')).fetchone() or
                    self.db.execute('SELECT 1 FROM album_repairs WHERE device=? AND status != ? LIMIT 1',
                                    (device, 'verified')).fetchone())

    def verified_tracks(self, device):
        return [row[0] for row in self.db.execute(
            'SELECT track_id FROM uploads WHERE device=? AND status=?', (device, 'verified'))]

    def begin_song(self, device, sha, path, plan):
        encoded = json.dumps(plan, sort_keys=True, separators=(',', ':'))
        self.db.execute('INSERT INTO uploads VALUES (?,?,?,?,?,?)', (device, sha, 'pending', None, str(path), encoded))
        self.db.commit()

    def finish_song(self, device, sha, track_id):
        self.db.execute('UPDATE uploads SET status=?,track_id=? WHERE device=? AND sha=?',
                        ('verified', str(track_id), device, sha))
        self.db.commit()

    def playlist(self, device, playlist_id):
        return self.db.execute('SELECT status,plan FROM playlists WHERE device=? AND playlist_id=?',
                               (device, str(playlist_id))).fetchone()

    def begin_playlist(self, device, playlist_id, plan):
        self.db.execute('INSERT OR REPLACE INTO playlists VALUES (?,?,?,?)',
                        (device, str(playlist_id), plan, 'pending'))
        self.db.commit()

    def finish_playlist(self, device, playlist_id):
        self.db.execute('UPDATE playlists SET status=? WHERE device=? AND playlist_id=?',
                        ('verified', device, str(playlist_id)))
        self.db.commit()

    def clear_song(self, device, sha):
        self.db.execute('DELETE FROM uploads WHERE device=? AND sha=?', (device, sha))
        self.db.commit()


async def run(adapter, device, songs, playlists, journal_path, progress, stop, batch_size=1,
              automatic_playlists=(), hash_workers=CHECK_WORKERS):
    """Upload in saved-date order, then link playlist members in newest order.

    Playlists are (destination ID, source songs). The adapter must reject
    writes to another device and verify content and membership readback.
    An interrupted write remains pending and requires explicit reconciliation.
    New songs are sent in groups of batch_size; a stop takes effect between
    groups, never inside one.
    """
    if not device:
        raise ImportErrorSafe('Choose a phone before importing.')
    ordered = list(reversed(newest_first(songs)))
    paths = {song.path for song in ordered}
    if not ordered or len(paths) != len(ordered):
        raise ImportErrorSafe('The music plan is empty or contains duplicate file paths.')
    destination_ids = [str(destination) for destination, _ in playlists]
    if any(not value or value == 'None' for value in destination_ids) or len(set(destination_ids)) != len(destination_ids):
        raise ImportErrorSafe('Choose a different existing playlist for each folder.')
    if any(any(song.path not in paths for song in members) for _, members in playlists):
        raise ImportErrorSafe('A playlist file is missing from the library plan.')
    if any(any(song.path not in paths for song in members) for _,members,_ in automatic_playlists):
        raise ImportErrorSafe('A playlist file is missing from the library plan.')
    lock = RunLock(Path(journal_path))
    lock.__enter__()
    try:
        return await _run_locked(adapter, device, ordered, playlists, journal_path, progress, stop,
                                 destination_ids, batch_size, automatic_playlists, hash_workers)
    finally:
        lock.__exit__(None, None, None)


async def _run_locked(adapter, device, ordered, playlists, journal_path, progress, stop,
                      destination_ids, batch_size, automatic_playlists, hash_workers):
    journal = Journal(Path(journal_path))
    identities = {}
    try:
        if journal.has_uncertain_operations(device):
            raise ImportErrorSafe('The previous upload or playlist update has an uncertain result; inspect the phone before retrying.')
        existing_playlists = await adapter.validate_device(device, destination_ids, journal.verified_tracks(device))
        creates={}
        if automatic_playlists:
            targets=await adapter.resolve_playlist_targets(device,[name for name,_,_ in automatic_playlists])
            if len(targets)!=len(automatic_playlists):
                raise ImportErrorSafe('Could not resolve all playlist folders.')
            resolved=[]
            for (name,members,expected_id),(destination,is_new) in zip(automatic_playlists,targets):
                destination=str(destination)
                if (expected_id is None and not is_new) or (expected_id is not None and (is_new or destination != str(expected_id))):
                    raise ImportErrorSafe(f'The phone playlist {name} changed since confirmation; review the import again.')
                if destination in destination_ids:
                    raise ImportErrorSafe('Playlist folders resolve to the same destination.')
                destination_ids.append(destination)
                resolved.append((destination,members))
                if is_new:
                    creates[destination]=name
            playlists=list(playlists)+resolved
            existing_playlists.update(await adapter.validate_device(
                device,[dest for dest,_ in resolved if dest not in creates],journal.verified_tracks(device)))
            existing_playlists.update({dest:() for dest in creates})
        for destination in destination_ids:
            prior = journal.playlist(device, destination)
            if prior and prior[0] != 'verified':
                raise ImportErrorSafe('The previous playlist update has an uncertain result; inspect it before retrying.')
        if isinstance(batch_size,bool) or not isinstance(batch_size,int) or not 1 <= batch_size <= 100:
            raise ImportErrorSafe('Choose a batch size from 1 to 100.')
        pending = []
        duplicates = []
        existing_checks = []
        seen_shas = set()
        hashes = {}
        index = 0
        async for sha in ordered_hashes(ordered, stop, hash_workers):
            song = ordered[index]
            index += 1
            progress('checking_local', index, len(ordered), song.path.name)
            hashes[song.path] = sha
            record = journal.song(device, sha)
            if record and record[0] != 'verified':
                raise ImportErrorSafe('The previous upload has an uncertain result; inspect the phone before retrying.')
            if record:
                track_id = record[1]
                existing_checks.append((song,track_id,sha,json.loads(record[2])))
                identities[song.path] = str(track_id)
            else:
                if sha in seen_shas:
                    duplicates.append((index, song, sha))
                    continue
                seen_shas.add(sha)
                pending.append((index, song, sha))
        if stop.is_set():
            return {'stopped': True, 'uploaded_or_verified': len(identities)}
        if existing_checks:
            try:
                progress('checking_metadata',0,len(existing_checks),'Preparing local metadata')
                await adapter.verify_existing(device,existing_checks,
                    lambda done,total,name,phase='checking_phone':progress(phase,done,total,name),stop)
            except Exception:
                if stop.is_set():
                    return {'stopped': True, 'uploaded_or_verified': 0}
                raise
        if stop.is_set():
            return {'stopped': True, 'uploaded_or_verified': len(identities)}
        # Resolve existing playlist members before uploading anything; use the
        # same checked hashes instead of rereading each source a second time.
        for destination, members in playlists:
            current = list(existing_playlists[str(destination)])
            if not current:
                continue
            known=[]
            for song in newest_first(members):
                record=journal.song(device,hashes[song.path])
                if record and record[0]=='verified' and record[1] not in known:
                    known.append(record[1])
            if not set(current).issubset(known) or [v for v in known if v in set(current)] != current:
                raise ImportErrorSafe('An existing playlist contains unrelated songs or has a different order; review it before importing.')
        for start in range(0, len(pending), batch_size):
            if stop.is_set():
                break
            chunk = pending[start:start + batch_size]
            progress('preparing_batch',start,len(pending),f'Preparing batch of {len(chunk)} songs')
            plans = await adapter.prepare_batch(device, [(song, sha) for _, song, sha in chunk])
            if len(plans) != len(chunk) or any(not isinstance(plan, dict) for plan in plans):
                raise ImportErrorSafe('Could not prepare a recoverable song operation.')
            checks = ordered_hashes((song for _, song, _ in chunk), stop, hash_workers)
            checked = 0
            async for digest in checks:
                _, song, sha = chunk[checked]
                plan = plans[checked]
                checked += 1
                if digest != sha or plan.get('sha') != sha:
                    raise ImportErrorSafe('The source changed during preparation; no upload was attempted.')
            if stop.is_set() or checked != len(chunk):
                return {'stopped': True, 'uploaded_or_verified': len(identities)}
            with journal.db:
                for (_, song, sha), plan in zip(chunk, plans):
                    encoded=json.dumps(plan,sort_keys=True,separators=(',',':'))
                    journal.db.execute('INSERT INTO uploads VALUES (?,?,?,?,?,?)',
                                       (device,sha,'pending',None,str(song.path),encoded))
            entries = [{'index': index, 'total': len(ordered), 'song': song, 'sha': sha, 'plan': plan}
                       for (index, song, sha), plan in zip(chunk, plans)]
            def on_asset(_asset_id, entry, _progress=progress, _size=len(chunk)):
                _progress('uploading', entry['index'], entry['total'],
                          f"{entry['song'].path.name} (group of {_size})")
            track_ids = await adapter.upload_batch(device, entries, on_asset)
            if len(track_ids) != len(chunk) or any(not track_id for track_id in track_ids):
                raise ImportErrorSafe('A song upload could not be verified; inspect the phone before retrying.')
            if [str(x) for x in track_ids] != [p['track_id'] for p in plans]:
                raise ImportErrorSafe('The verified batch identities differ from the saved plan.')
            with journal.db:
                for (_, song, sha), track_id in zip(chunk, track_ids):
                    cursor=journal.db.execute('UPDATE uploads SET status=?,track_id=? WHERE device=? AND sha=? AND status=?',
                                              ('verified',str(track_id),device,sha,'pending'))
                    if cursor.rowcount != 1:
                        raise ImportErrorSafe('A batch journal entry changed; inspect before retrying.')
            for (_, song, _sha), track_id in zip(chunk, track_ids):
                identities[song.path] = str(track_id)
            for (index, song, _sha) in chunk:
                progress('verified', index, len(ordered), song.path.name)
        if stop.is_set():
            return {'stopped': True, 'uploaded_or_verified': len(identities)}
        for index, song, sha in duplicates:
            record = journal.song(device, sha)
            if not record or record[0] != 'verified':
                raise ImportErrorSafe('An identical source file was not uploaded with its copy.')
            identities[song.path] = record[1]
            progress('verified', index, len(ordered), song.path.name)
        if stop.is_set():
            return {'stopped': True, 'uploaded_or_verified': len(ordered)}
        for index, (destination, members) in enumerate(playlists, 1):
            if stop.is_set():
                return {'stopped': True, 'uploaded_or_verified': len(ordered)}
            # Identical files resolve to one phone track; keep its first
            # occurrence in the desired order instead of adding duplicates.
            desired = list(dict.fromkeys(identities[song.path] for song in newest_first(members)))
            plan = json.dumps(desired, separators=(',', ':'))
            prior = journal.playlist(device, destination)
            if prior and prior[0] != 'verified':
                raise ImportErrorSafe('The previous playlist update has an uncertain result; inspect it before retrying.')
            if prior and prior[1] == plan:
                if not await adapter.verify_playlist(device, destination, desired):
                    raise ImportErrorSafe('A previously linked playlist no longer matches its verified order.')
            else:
                if destination not in creates:
                    await adapter.validate_playlist_update(device, destination, desired)
                saved_plan = json.dumps({'name':creates[destination], 'track_ids':desired},ensure_ascii=False) if destination in creates else plan
                journal.begin_playlist(device, destination, saved_plan)
                progress('playlist', index, len(playlists), str(destination))
                if destination in creates:
                    await adapter.create_playlist(device,destination,creates[destination],desired)
                else:
                    await adapter.update_playlist(device, destination, desired)
                if not await adapter.verify_playlist(device, destination, desired):
                    raise ImportErrorSafe('Playlist update could not be verified; inspect it before retrying.')
                journal.finish_playlist(device, destination)
        return {'stopped': False, 'uploaded_or_verified': len(ordered)}
    finally:
        journal.close()
