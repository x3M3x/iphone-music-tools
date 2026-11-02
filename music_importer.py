"""Plan and safely append exported music to an iTunes device source.

This module does not launch iTunes, change sync settings, delete tracks,
or write to a device until an explicit import operation is requested.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from mutagen import File as AudioFile

SUPPORTED = {'.mp3', '.m4a', '.aac', '.aif', '.aiff', '.wav'}
KNOWN_AUDIO = SUPPORTED | {'.flac', '.ogg', '.opus', '.wma', '.m4p'}


class ImportErrorSafe(Exception):
    pass


@dataclass(frozen=True)
class Collection:
    name: str
    folder: Path
    is_rest: bool = False
    all_music: bool = False


@dataclass(frozen=True)
class Song:
    path: Path
    title: str
    artist: str
    album: str
    size: int
    modified_ns: int
    duration: float | None
    file_date_ns: int = 0
    has_artwork: bool | None = None


def collections(root: Path) -> list[Collection]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ImportErrorSafe('Choose an existing music export folder.')
    result = []
    playlist_dir = root / 'Playlists'
    if playlist_dir.is_dir():
        for folder in sorted(playlist_dir.iterdir(), key=lambda p: p.name.casefold()):
            if folder.is_dir() and not folder.is_symlink():
                result.append(Collection(folder.name, folder.resolve(), False))
    rest = root / 'All the rest'
    if rest.is_dir() and not rest.is_symlink():
        result.append(Collection('All the rest', rest.resolve(), True))
    if result:
        result.insert(0, Collection('All music (all folders)', root, False, True))
    else:
        result.append(Collection(root.name, root, False))
    return result


def _under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _music_files(folder: Path) -> tuple[list[Path], list[str]]:
    allowed, unsupported = [], []
    for entry in folder.rglob('*'):
        if entry.is_symlink():
            raise ImportErrorSafe(f'Symbolic link skipped for safety: {entry.name}')
        if not entry.is_file():
            continue
        if entry.suffix.lower() not in KNOWN_AUDIO:
            continue
        resolved = entry.resolve(strict=True)
        if not _under(resolved, folder):
            raise ImportErrorSafe('A music file is outside the chosen folder.')
        if entry.suffix.lower() in SUPPORTED:
            allowed.append(resolved)
        else:
            unsupported.append(str(entry.relative_to(folder)))
    return allowed, unsupported


def _verified_shared_track(root: Path, missing: Path) -> Path | None:
    """Resolve a moved Arabic track only through matching recorded IDs and hashes."""
    metadata_file = root / '_metadata_report.json'
    nasheed_file = root / '_nasheed_repair_report.json'
    if not metadata_file.is_file() or not nasheed_file.is_file():
        return None
    try:
        metadata = json.loads(metadata_file.read_text(encoding='utf-8'))
        nasheed = json.loads(nasheed_file.read_text(encoding='utf-8'))
        key = str(missing.relative_to(root)).casefold()
        rows = [row for row in metadata['updated'] if str(Path(row['file'])).casefold() == key]
        if len(rows) != 1:
            return None
        matches = [row for row in nasheed['tracks'] if row['id'] == rows[0]['id']]
        if len(matches) != 1 or matches[0]['sha256'] != rows[0]['sha256']:
            return None
        name = matches[0]['file']
        if not name or Path(name).name != name:
            return None
        folder = (root / 'Playlists' / 'اناشيد').resolve()
        candidate = folder / name
        if candidate.is_symlink() or not candidate.is_file() or not _under(candidate.resolve(), folder):
            return None
        digest = hashlib.sha256()
        with candidate.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        return candidate.resolve() if digest.hexdigest() == matches[0]['sha256'] else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _moved_shared_tracks(root: Path, selected: Collection, available: list[Path]) -> list[Path]:
    """Recover verified moved members; this does not determine their order."""
    if selected.is_rest or selected.all_music:
        return []
    report = root / '_metadata_report.json'
    if not report.is_file() or report.is_symlink():
        return []
    try:
        data = json.loads(report.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    seen = set(available)
    result = []
    for row in data.get('updated', []):
        try:
            relative = Path(row['file'])
            if relative.is_absolute() or '..' in relative.parts:
                continue
            previous = (root / relative).resolve()
            if previous.parent != selected.folder or previous.is_file():
                continue
            replacement = _verified_shared_track(root, previous)
            if replacement is not None and replacement not in seen:
                result.append(replacement)
                seen.add(replacement)
        except (KeyError, TypeError, OSError):
            continue
    return result


def file_date_ns(path: Path) -> int:
    stat = path.stat()
    if hasattr(stat, 'st_birthtime_ns'):
        return stat.st_birthtime_ns
    if hasattr(stat, 'st_birthtime'):
        return int(stat.st_birthtime * 1_000_000_000)
    return stat.st_mtime_ns


def _song(path: Path) -> Song:
    stat = path.stat()
    title, artist, album, duration = path.stem, '', '', None
    try:
        audio = AudioFile(path, easy=True)
        if audio is not None:
            title = str((audio.get('title') or [title])[0])
            artist = str((audio.get('artist') or [''])[0])
            album = str((audio.get('album') or [''])[0])
            duration = getattr(audio.info, 'length', None)
    except Exception:
        pass
    has_artwork = None
    try:
        raw = AudioFile(path)
        if raw is not None and raw.tags is not None:
            if hasattr(raw.tags, 'getall'):
                has_artwork = bool(raw.tags.getall('APIC'))
            elif path.suffix.lower() == '.m4a':
                has_artwork = bool(raw.tags.get('covr'))
    except Exception:
        pass
    return Song(path, title, artist, album, stat.st_size, stat.st_mtime_ns, duration, file_date_ns(path), has_artwork)


def build_plan(root: Path, selected: Collection, order: str = 'newest') -> tuple[list[Song], list[str], str]:
    root = root.resolve(strict=True)
    folder = selected.folder.resolve(strict=True)
    if not _under(folder, root):
        raise ImportErrorSafe('The chosen collection is outside the music export.')
    files, unsupported = _music_files(folder)
    moved = _moved_shared_tracks(root, selected, files)
    all_entries = files + moved
    if order == 'oldest':
        ordered, note = sorted(all_entries, key=lambda p: (file_date_ns(p), str(p).casefold())), 'Oldest file date first.'
    elif order == 'newest':
        ordered, note = sorted(all_entries, key=lambda p: (file_date_ns(p), str(p).casefold()), reverse=True), 'Newest file date first; imports oldest first.'
    elif order == 'name':
        ordered, note = sorted(all_entries, key=lambda p: str(p).casefold()), 'Sorted by filename.'
    else:
        raise ImportErrorSafe('Unknown song order.')
    if moved:
        word = 'track' if len(moved) == 1 else 'tracks'
        note += f' {len(moved)} moved {word} use verified copies in Nasheed.'
    return [_song(path) for path in ordered], unsupported, note


def prepare_import_plan(root: Path, selected: Collection, mappings, expected_names=None, stop=None):
    """Rescan the actual folders immediately before confirmation, without device access."""
    root = root.resolve(strict=True)
    if expected_names is not None:
        current = {c.name for c in collections(root) if not c.is_rest and not c.all_music}
        if current != set(expected_names):
            raise ImportErrorSafe('Playlist folders changed. Refresh the export folder and map playlists again.')
    songs, unsupported, note = build_plan(root, selected, 'newest')
    paths = {song.path for song in songs}
    bindings = []
    for group, destination in mappings:
        if stop is not None and stop.is_set():
            raise ImportErrorSafe('Stopped before connecting to iTunes.')
        members, _, _ = build_plan(root, group, 'newest')
        if any(song.path not in paths for song in members):
            raise ImportErrorSafe('Files changed while scanning. Retry so All Music includes every playlist song.')
        bindings.append(PlaylistBinding(group.name, destination.get('id'), destination.get('new_name'), members))
    return songs, unsupported, note, bindings


def content_hash(song: Song) -> str:
    before = song.path.stat()
    if before.st_size != song.size or before.st_mtime_ns != song.modified_ns:
        raise ImportErrorSafe(f'The source file changed after preview: {song.path.name}')
    h = hashlib.sha256()
    with song.path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    after = song.path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ImportErrorSafe(f'The source file changed while reading: {song.path.name}')
    return h.hexdigest()


def open_itunes():
    import win32com.client
    try:
        return win32com.client.GetActiveObject('iTunes.Application')
    except Exception:
        # A normally opened iTunes window may not register in the running
        # object table until its automation interface is first activated.
        # Never activate it unless iTunes is already running for this user.
        import psutil
        try:
            owner = psutil.Process().username().casefold()
            running = any((p.info.get('name') or '').casefold() == 'itunes.exe'
                          and (p.info.get('username') or '').casefold() == owner
                          for p in psutil.process_iter(['name', 'username']))
        except Exception as exc:
            raise ImportErrorSafe('Could not check whether iTunes is already open.') from exc
        if not running:
            raise ImportErrorSafe('Open iTunes first, then connect and unlock the new iPhone.')
        try:
            return win32com.client.Dispatch('iTunes.Application')
        except Exception as exc:
            raise ImportErrorSafe('iTunes is open, but its automation interface could not be connected.') from exc


def persistent_id(itunes, obj) -> tuple[int, int]:
    try:
        hi = int(itunes.ITObjectPersistentIDHigh(obj))
        lo = int(itunes.ITObjectPersistentIDLow(obj))
    except Exception as exc:
        raise ImportErrorSafe('iTunes could not provide a stable identifier; import was stopped.') from exc
    if hi == 0 and lo == 0:
        raise ImportErrorSafe('iTunes returned an empty identifier; import was stopped.')
    return hi, lo


def id_text(value: tuple[int, int]) -> str:
    return f'{value[0]}:{value[1]}'


def find_device(itunes, identity: str):
    found = []
    for i in range(1, int(itunes.Sources.Count) + 1):
        source = itunes.Sources.Item(i)
        if int(source.Kind) in (2, 5) and id_text(persistent_id(itunes, source)) == identity:
            found.append(source)
    if len(found) != 1:
        raise ImportErrorSafe('The selected device changed or disconnected; refresh the device list.')
    return found[0]


def get_devices(itunes) -> list[dict]:
    result = []
    for i in range(1, int(itunes.Sources.Count) + 1):
        source = itunes.Sources.Item(i)
        if int(source.Kind) not in (2, 5):
            continue
        ident = id_text(persistent_id(itunes, source))
        result.append({'id': ident, 'name': str(source.Name), 'source_id': int(source.sourceID)})
    return result


def cast_playlist(playlist, kind: int):
    import win32com.client
    if kind == 1:
        return win32com.client.CastTo(playlist, 'IITLibraryPlaylist')
    if kind == 2:
        return win32com.client.CastTo(playlist, 'IITUserPlaylist')
    if kind == 4:
        # iTunes reports some device libraries as kind 4. Use them only if
        # they actually expose the library add interface.
        return win32com.client.CastTo(playlist, 'IITLibraryPlaylist')
    raise ImportErrorSafe('This is not a writable music playlist.')


def get_destinations(itunes, device_id: str) -> list[dict]:
    source = find_device(itunes, device_id)
    result = []
    for i in range(1, int(source.Playlists.Count) + 1):
        playlist = source.Playlists.Item(i)
        kind = int(playlist.Kind)
        if kind not in (1, 2, 4):
            continue
        try:
            target = cast_playlist(playlist, kind)
            if kind == 2 and (bool(target.Smart) or int(target.SpecialKind) not in (0, 6)):
                continue
        except Exception:
            continue
        identity = id_text(persistent_id(itunes, playlist))
        result.append({'id': identity, 'name': str(playlist.Name), 'kind': kind,
                       'count': int(playlist.Tracks.Count)})
    return result


def find_playlist(itunes, source, identity: str):
    matches = []
    for i in range(1, int(source.Playlists.Count) + 1):
        playlist = source.Playlists.Item(i)
        if id_text(persistent_id(itunes, playlist)) == identity:
            matches.append(playlist)
    if len(matches) != 1:
        raise ImportErrorSafe('The chosen playlist changed or disappeared; refresh the destinations.')
    return matches[0]


class Journal:
    """Local history prevents blind retries after a partially completed COM call."""
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS assets (
                device TEXT NOT NULL, sha TEXT NOT NULL, hi INTEGER NOT NULL, lo INTEGER NOT NULL,
                PRIMARY KEY(device, sha));
            CREATE TABLE IF NOT EXISTS operations (
                device TEXT NOT NULL, playlist TEXT NOT NULL, sha TEXT NOT NULL,
                occurrence INTEGER NOT NULL, status TEXT NOT NULL, path TEXT NOT NULL,
                hi INTEGER, lo INTEGER, message TEXT,
                PRIMARY KEY(device, playlist, sha, occurrence));
        ''')
        self.db.commit()

    def operation(self, device, playlist, sha, occurrence):
        return self.db.execute('SELECT status,hi,lo FROM operations WHERE device=? AND playlist=? AND sha=? AND occurrence=?',
                               (device, playlist, sha, occurrence)).fetchone()

    def asset(self, device, sha):
        return self.db.execute('SELECT hi,lo FROM assets WHERE device=? AND sha=?', (device, sha)).fetchone()

    def pending(self, device, playlist, sha, occurrence, path):
        self.db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?)',
                        (device, playlist, sha, occurrence, 'pending', str(path), None, None, None))
        self.db.commit()

    def finish(self, device, playlist, sha, occurrence, track_id, message):
        self.db.execute('UPDATE operations SET status=?,hi=?,lo=?,message=? WHERE device=? AND playlist=? AND sha=? AND occurrence=?',
                        ('verified', track_id[0], track_id[1], message, device, playlist, sha, occurrence))
        self.db.execute('INSERT OR IGNORE INTO assets VALUES(?,?,?,?)', (device, sha, track_id[0], track_id[1]))
        self.db.commit()

    def close(self):
        self.db.close()


def _collection_item_by_pid(collection, identity):
    try:
        return collection.ItemByPersistentID(identity[0], identity[1])
    except Exception as exc:
        # A missing item is commonly returned as NULL. Other COM errors are
        # ambiguous, so do not retry an operation against a live device.
        raise ImportErrorSafe('Could not verify a previously imported track on the phone; inspect iTunes before continuing.') from exc


def _find_library(itunes, source):
    matches = []
    for i in range(1, int(source.Playlists.Count) + 1):
        playlist = source.Playlists.Item(i)
        if int(playlist.Kind) in (1, 4):
            try:
                cast_playlist(playlist, int(playlist.Kind))
                matches.append(playlist)
            except Exception:
                continue
    if len(matches) != 1:
        raise ImportErrorSafe('iTunes did not expose exactly one writable music library for this phone.')
    return matches[0]


def _matching_keys(collection):
    result = set()
    for i in range(1, int(collection.Count) + 1):
        track = collection.Item(i)
        title = str(track.Name or '').strip().casefold()
        artist = str(track.Artist or '').strip().casefold()
        if title and artist:
            result.add((title, artist))
    return result


def wait_operation(operation, timeout=180):
    if operation is None:
        raise ImportErrorSafe('iTunes returned no import operation; check the phone before retrying.')
    deadline = time.monotonic() + timeout
    while bool(operation.InProgress):
        if time.monotonic() >= deadline:
            raise ImportErrorSafe('iTunes is still processing a track; its outcome is unknown. Check iTunes before retrying.')
        time.sleep(0.2)
    if int(operation.Tracks.Count) != 1:
        raise ImportErrorSafe('iTunes did not confirm exactly one imported track; check the phone before retrying.')
    return operation.Tracks.Item(1)


def verify_append(itunes, playlist, track, before_count, is_library):
    if int(track.sourceID) != int(playlist.Source.sourceID):
        raise ImportErrorSafe('iTunes returned a track from a different source; import stopped.')
    if is_library:
        return
    deadline = time.monotonic() + 10
    while int(playlist.Tracks.Count) < before_count + 1 and time.monotonic() < deadline:
        time.sleep(0.2)
    count = int(playlist.Tracks.Count)
    if count != before_count + 1:
        raise ImportErrorSafe('iTunes did not confirm a single playlist addition; check the playlist before retrying.')
    last = playlist.Tracks.ItemByPlayOrder(count)
    if persistent_id(itunes, last) != persistent_id(itunes, track):
        raise ImportErrorSafe('The track was added but iTunes did not confirm it at the end of the playlist; import stopped.')


def verify_relative_order(itunes, playlist, desired_ids):
    """Check that confirmed tracks occur in the requested append sequence."""
    if not desired_ids:
        return True
    pos = 0
    for i in range(1, int(playlist.Tracks.Count) + 1):
        if persistent_id(itunes, playlist.Tracks.ItemByPlayOrder(i)) == desired_ids[pos]:
            pos += 1
            if pos == len(desired_ids):
                return True
    return False


def library_date(track):
    try:
        value = track.DateAdded
    except Exception as exc:
        raise ImportErrorSafe('iTunes cannot read the track’s library date; playlist linking was stopped before changing it.') from exc
    if value is None:
        raise ImportErrorSafe('iTunes returned no library date; playlist linking was stopped before changing it.')
    return value


def verify_track_metadata(track, song: Song) -> bool:
    """Check available device metadata; return whether artwork was verifiable."""
    for prop, expected in [('Name', song.title), ('Artist', song.artist), ('Album', song.album)]:
        if expected and expected.strip():
            actual = str(getattr(track, prop) or '')
            if actual.strip() != expected.strip():
                raise ImportErrorSafe(f'iTunes reported different {prop.lower()} metadata for the imported track.')
    if not song.has_artwork:
        return True
    try:
        artwork = track.Artwork
        deadline = time.monotonic() + 5
        while int(artwork.Count) == 0 and time.monotonic() < deadline:
            time.sleep(0.2)
        if int(artwork.Count) == 0:
            raise ImportErrorSafe('The source has artwork but iTunes reported none on the imported track.')
        return True
    except ImportErrorSafe:
        raise
    except Exception:
        # Some device sources do not expose their artwork collection via COM.
        return False


def perform_import(itunes, device_id: str, destination_id: str | None, new_name: str | None,
                   songs: list[Song], journal_path: Path, progress, stop, skip_possible=True):
    """Copy oldest-first to Music, then link newest-first to the chosen playlist."""
    source = find_device(itunes, device_id)
    library = _find_library(itunes, source)
    library_id = id_text(persistent_id(itunes, library))
    library_target = cast_playlist(library, int(library.Kind))
    if new_name:
        name = new_name.strip()
        if not name:
            raise ImportErrorSafe('Enter a name for the new playlist.')
        if any(str(source.Playlists.Item(i).Name).strip().casefold() == name.casefold()
               for i in range(1, int(source.Playlists.Count) + 1)):
            raise ImportErrorSafe('A playlist with that name already exists; select it instead.')
        try:
            playlist = itunes.CreatePlaylistInSource(name, source)
        except Exception as exc:
            raise ImportErrorSafe('iTunes could not confirm playlist creation. Check the phone before retrying, and use manual music management without accepting an erase prompt.') from exc
        if playlist is None:
            raise ImportErrorSafe('iTunes did not create the playlist; check manual music management.')
    else:
        if not destination_id:
            raise ImportErrorSafe('Choose a destination playlist.')
        playlist = find_playlist(itunes, source, destination_id)
    playlist_id = id_text(persistent_id(itunes, playlist))
    kind = int(playlist.Kind)
    target = cast_playlist(playlist, kind)
    if kind == 2 and (bool(target.Smart) or int(target.SpecialKind) not in (0, 6)):
        raise ImportErrorSafe('Smart or system playlists cannot be modified by this importer.')
    if int(playlist.Source.sourceID) != int(source.sourceID):
        raise ImportErrorSafe('The playlist does not belong to the selected device.')
    if kind in (1, 4) and playlist_id != library_id:
        raise ImportErrorSafe('The selected music library could not be identified safely.')
    counts = {'added': 0, 'reused': 0, 'already': 0, 'possible': 0, 'total': len(songs),
              'processed': 0, 'stage': 'copy', 'artwork_unverified': 0, 'order': 'not yet checked'}
    progress('checking', 0, counts, 'Checking music already on the selected phone')
    candidates = _matching_keys(library.Tracks) if skip_possible else set()
    journal = Journal(journal_path)
    batch_seen = {}
    prepared = []
    try:
        for index, song in enumerate(songs, 1):
            if stop.is_set():
                break
            progress('hashing', index, counts, song.path.name)
            sha = content_hash(song)
            if stop.is_set():
                break
            if sha in batch_seen:
                duplicate_outcome = 'possible' if batch_seen[sha] == 'possible' else 'already'
                counts[duplicate_outcome] += 1
                counts['processed'] = index
                prepared.append((song, sha, 'duplicate'))
                progress(duplicate_outcome, index, counts, song.path.name)
                continue
            prior = journal.operation(device_id, library_id, sha, 1)
            if prior and prior[0] != 'verified':
                raise ImportErrorSafe(f'The previous result for {song.path.name} is uncertain. Check All Music before retrying.')
            if prior:
                existing = _collection_item_by_pid(library.Tracks, (prior[1], prior[2]))
                if existing is None:
                    raise ImportErrorSafe(f'A previously imported song is no longer in All Music: {song.path.name}')
                counts['already'] += 1
                batch_seen[sha] = 'present'
                prepared.append((song, sha, 'ready'))
                counts['processed'] = index
                progress('already', index, counts, song.path.name)
                continue
            asset = journal.asset(device_id, sha)
            if not asset:
                key = (song.title.strip().casefold(), song.artist.strip().casefold())
                if skip_possible and key[0] and key[1] and key in candidates:
                    counts['possible'] += 1
                    batch_seen[sha] = 'possible'
                    prepared.append((song, sha, 'possible'))
                    counts['processed'] = index
                    progress('possible', index, counts, song.path.name)
                    continue
            existing = None
            if asset:
                existing = _collection_item_by_pid(library.Tracks, asset)
                if existing is None:
                    raise ImportErrorSafe('A known track could not be found on the phone; inspect iTunes before continuing.')
            journal.pending(device_id, library_id, sha, 1, song.path)
            try:
                if asset:
                    track = existing
                    outcome = 'already'
                else:
                    progress('importing', index, counts, song.path.name)
                    before = int(library.Tracks.Count)
                    operation = library_target.AddFile(str(song.path))
                    track = wait_operation(operation)
                    verify_append(itunes, library, track, before, True)
                    if not verify_track_metadata(track, song):
                        counts['artwork_unverified'] += 1
                    current = song.path.stat()
                    if (current.st_size, current.st_mtime_ns) != (song.size, song.modified_ns):
                        raise ImportErrorSafe('The source file changed during the import.')
                    outcome = 'added'
                track_id = persistent_id(itunes, track)
                journal.finish(device_id, library_id, sha, 1, track_id, outcome)
                counts[outcome] += 1
                batch_seen[sha] = 'present'
                prepared.append((song, sha, 'ready'))
                counts['processed'] = index
                if song.title.strip() and song.artist.strip():
                    candidates.add((song.title.strip().casefold(), song.artist.strip().casefold()))
                progress(outcome, index, counts, song.path.name)
            except Exception as exc:
                # The operation might have succeeded before an exception. Leave
                # it pending so the next run cannot silently add a duplicate.
                raise ImportErrorSafe(f'Import stopped at {song.path.name}. The last operation may have completed; inspect the phone before retrying. Details: {exc}') from exc
        if stop.is_set():
            counts['order'] = 'Stopped before completing the library import.'
            return counts
        if kind in (1, 4):
            counts['order'] = 'Files were sent oldest first; the iPhone controls library sorting.'
            return counts

        # The file copies above ran oldest-to-newest. Linking existing phone
        # tracks in reverse gives a new playlist a newest-first play order.
        counts['stage'] = 'playlist'
        counts['processed'] = 0
        desired_ids = []
        progress('playlist', 0, counts, 'Adding the selected songs to the playlist')
        for index, (song, sha, state) in enumerate(reversed(prepared), 1):
            if stop.is_set():
                break
            if state != 'ready':
                counts['processed'] = index
                progress(state, index, counts, song.path.name)
                continue
            prior = journal.operation(device_id, playlist_id, sha, 1)
            if prior and prior[0] != 'verified':
                raise ImportErrorSafe(f'The previous playlist result for {song.path.name} is uncertain. Check the playlist before retrying.')
            if prior:
                track = _collection_item_by_pid(playlist.Tracks, (prior[1], prior[2]))
                if track is None:
                    raise ImportErrorSafe(f'A previously linked song is no longer in the selected playlist: {song.path.name}')
                track_id = (prior[1], prior[2])
                outcome = 'already'
            else:
                asset = journal.asset(device_id, sha)
                if not asset:
                    raise ImportErrorSafe('A verified library track could not be identified; no playlist addition was attempted.')
                existing = _collection_item_by_pid(library.Tracks, asset)
                if existing is None:
                    raise ImportErrorSafe('A known track could not be found on the phone; inspect iTunes before continuing.')
                already_in_target = _collection_item_by_pid(playlist.Tracks, asset)
                if already_in_target is not None:
                    track_id = persistent_id(itunes, already_in_target)
                    journal.pending(device_id, playlist_id, sha, 1, song.path)
                    journal.finish(device_id, playlist_id, sha, 1, track_id, 'already')
                    outcome = 'already'
                else:
                    before = int(playlist.Tracks.Count)
                    date_before = library_date(existing)
                    journal.pending(device_id, playlist_id, sha, 1, song.path)
                    try:
                        track = target.AddTrack(existing)
                        if track is None:
                            raise ImportErrorSafe('iTunes returned no track after linking the song.')
                        verify_append(itunes, playlist, track, before, False)
                        current_library_track = _collection_item_by_pid(library.Tracks, asset)
                        if current_library_track is None or library_date(current_library_track) != date_before:
                            raise ImportErrorSafe('The song was linked, but its All Music date changed or could not be verified.')
                        track_id = persistent_id(itunes, track)
                        journal.finish(device_id, playlist_id, sha, 1, track_id, 'reused')
                        counts['reused'] += 1
                        outcome = 'reused'
                    except Exception as exc:
                        raise ImportErrorSafe(f'Playlist linking stopped at {song.path.name}. The last operation may have completed; inspect the playlist before retrying. Details: {exc}') from exc
            desired_ids.append(track_id)
            counts['processed'] = index
            progress(outcome, index, counts, song.path.name)
        if stop.is_set():
            counts['order'] = 'Stopped before the playlist was complete.'
        elif not desired_ids:
            counts['order'] = 'No playlist tracks were linked; review any skipped matches.'
        else:
            try:
                counts['order'] = ('Newest-first relative order confirmed for linked tracks.'
                                   if verify_relative_order(itunes, playlist, desired_ids)
                                   else 'The existing playlist order differs; no existing tracks were moved.')
            except Exception:
                counts['order'] = 'Playlist order could not be verified in iTunes.'
        return counts
    finally:
        journal.close()


@dataclass(frozen=True)
class PlaylistBinding:
    name: str
    destination_id: str | None
    new_name: str | None
    songs: list[Song]


def newest_first(songs: list[Song]) -> list[Song]:
    return sorted(songs, key=lambda s: (s.file_date_ns or s.modified_ns,
                                        str(s.path).casefold()), reverse=True)


def perform_batch(itunes, device_id: str, library_id: str, all_songs: list[Song],
                  bindings: list[PlaylistBinding], journal_path: Path, progress, stop,
                  skip_possible=True):
    """Import all files oldest-first, then link selected playlists newest-first."""
    source = find_device(itunes, device_id)
    library = _find_library(itunes, source)
    if id_text(persistent_id(itunes, library)) != library_id:
        raise ImportErrorSafe('Choose All Music on the selected phone for the first step.')
    all_paths = {song.path for song in all_songs}
    used_destinations, used_names = set(), set()
    for binding in bindings:
        if not binding.songs or any(song.path not in all_paths for song in binding.songs):
            raise ImportErrorSafe(f'The {binding.name} playlist contains a file outside the planned library.')
        if binding.destination_id:
            if binding.destination_id in used_destinations:
                raise ImportErrorSafe('Choose a different iPhone playlist for each source playlist.')
            used_destinations.add(binding.destination_id)
            target = find_playlist(itunes, source, binding.destination_id)
            if int(target.Kind) != 2:
                raise ImportErrorSafe('A playlist destination is not a normal user playlist.')
            target = cast_playlist(target, 2)
            if bool(target.Smart) or int(target.SpecialKind) != 0:
                raise ImportErrorSafe('A playlist destination is smart or system-managed.')
        elif binding.new_name and binding.new_name.strip():
            key = binding.new_name.strip().casefold()
            existing = {str(source.Playlists.Item(i).Name).strip().casefold()
                        for i in range(1, int(source.Playlists.Count) + 1)}
            if key in existing or key in used_names:
                raise ImportErrorSafe('A requested new playlist name already exists.')
            used_names.add(key)
        else:
            raise ImportErrorSafe('Choose an existing playlist or a new playlist name.')
    library_count = int(library.Tracks.Count)
    total = len(all_songs) + sum(2 * len(binding.songs) for binding in bindings)
    overall = 0
    results = []

    def step(label, offset, stage_songs, playlist_stage):
        def emit(phase, index, counts, filename):
            n = len(stage_songs)
            completed = counts['processed'] + (n if playlist_stage and counts['stage'] == 'playlist' else 0)
            info = dict(counts)
            info.update({'batch_done': offset + completed, 'batch_total': total, 'batch_label': label,
                         'existing_library_count': library_count})
            progress(phase, index, info, filename)
        return emit

    ordered = list(reversed(newest_first(all_songs)))
    summary = perform_import(itunes, device_id, library_id, None, ordered,
                             journal_path, step('All Music', overall, ordered, False), stop, skip_possible)
    results.append({'collection': 'All Music', 'result': summary})
    if stop.is_set():
        return {'stopped': True, 'existing_library_count': library_count, 'results': results}
    overall += len(ordered)
    for binding in bindings:
        if stop.is_set():
            break
        ordered = list(reversed(newest_first(binding.songs)))
        summary = perform_import(itunes, device_id, binding.destination_id, binding.new_name,
                                 ordered, journal_path, step(binding.name, overall, ordered, True),
                                 stop, skip_possible)
        results.append({'collection': binding.name, 'result': summary})
        if stop.is_set():
            break
        overall += 2 * len(ordered)
    return {'stopped': stop.is_set(), 'existing_library_count': library_count, 'results': results}
