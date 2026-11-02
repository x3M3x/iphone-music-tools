"""Read-only verification of a single uploaded phone track and its cover."""
import asyncio
import hashlib
import re
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from collections import Counter

from direct_phone import PhoneReadError, stable_capture
from direct_audio import phone_cover
from music_importer import file_date_ns
from pymobiledevice3.exceptions import ConnectionTerminatedError

CONNECTION_ERRORS = (ConnectionTerminatedError, ConnectionError, EOFError, asyncio.IncompleteReadError)


@dataclass(frozen=True)
class TrackReadback:
    path: str
    size: int
    title: str
    artist: str
    album: str
    artwork_path: str | None
    date_added: int
    metadata: dict


def _parse_track_db(db, wire_id, artwork_id=None):
    if isinstance(wire_id, bool) or not isinstance(wire_id, int) or not -(2**63) <= wire_id < 2**63 or wire_id == 0:
        raise PhoneReadError('Invalid track identity.')
    rows = db.execute('''SELECT i.item_pid,e.location,e.file_size,e.title,
        ar.item_artist,al.album,b.path,i.date_added,
        i.track_number,e.track_count,i.disc_number,e.disc_count,e.year,
        g.genre,aa.album_artist,c.composer,e.copyright,e.comment,i.is_compilation
        FROM item i
        JOIN item_store s ON s.item_pid=i.item_pid
        JOIN item_extra e ON e.item_pid=i.item_pid
        LEFT JOIN item_artist ar ON ar.item_artist_pid=i.item_artist_pid
        LEFT JOIN album al ON al.album_pid=i.album_pid
        LEFT JOIN album_artist aa ON aa.album_artist_pid=i.album_artist_pid
        LEFT JOIN genre g ON g.genre_id=i.genre_id
        LEFT JOIN composer c ON c.composer_pid=i.composer_pid
        LEFT JOIN base_location b ON b.base_location_id=i.base_location_id
        WHERE s.sync_id=? AND e.media_kind=1 AND i.in_my_library=1''', (wire_id,)).fetchall()
    if len(rows) != 1:
        raise PhoneReadError('The uploaded track is missing or has an ambiguous identity.')
    (item_pid, loc, size, title, artist, album, base, date_added,
     track_number,track_count,disc_number,disc_count,year,genre,
     album_artist,composer,copyright,comment,compilation) = rows[0]
    loc = (loc or '').replace(':','/').strip('/')
    base = (base or '').replace(':','/').strip('/')
    if loc.startswith('iTunes_Control/Music/'):
        remote = loc
    elif re.fullmatch(r'F\d{2}', base, re.I):
        remote = 'iTunes_Control/Music/' + base + '/' + loc
    elif re.fullmatch(r'iTunes_Control/Music/F\d{2}', base, re.I):
        remote = base + '/' + loc
    else:
        raise PhoneReadError('The uploaded track has an unsupported storage path.')
    if not re.fullmatch(r'iTunes_Control/Music/F\d{2}/[^/\\]+', remote, re.I) or '/..' in remote:
        raise PhoneReadError('The uploaded track path is invalid.')
    cover_path = None
    if artwork_id is not None:
        artwork = db.execute('''SELECT a.relative_path FROM artwork_token t
            JOIN artwork a ON a.artwork_token=t.artwork_token
                AND a.artwork_source_type=t.artwork_source_type
            WHERE t.entity_pid=? AND t.entity_type=0 AND t.artwork_source_type=300
                AND t.artwork_token=?''', (item_pid, str(artwork_id))).fetchall()
        if len(artwork) != 1 or not artwork[0][0]:
            raise PhoneReadError('The uploaded cover is not registered on the phone.')
        relative = PurePosixPath(artwork[0][0])
        if relative.is_absolute() or any(part in ('..', '.') for part in relative.parts) or '\\' in str(relative):
            raise PhoneReadError('The uploaded cover has an invalid storage path.')
        cover_path = 'iTunes_Control/iTunes/Artwork/Originals/' + str(relative)
    metadata={'track_number':track_number,'track_count':track_count,'disc_number':disc_number,
              'disc_count':disc_count,'year':year,'genre':genre,'albumartist':album_artist,
              'composer':composer,'copyright':copyright,'comment':comment,'compilation':compilation}
    return TrackReadback(remote, int(size), title or '', artist or '', album or '', cover_path, int(date_added or 0), metadata)


def parse_tracks(database, wal, identities):
    """Read all requested identities from one local database snapshot."""
    with tempfile.TemporaryDirectory(prefix='music-track-') as tmp:
        path = Path(tmp) / 'catalog.sqlite'
        path.write_bytes(database)
        if wal:
            Path(str(path) + '-wal').write_bytes(wal)
        try:
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as db:
                db.execute('PRAGMA query_only=ON')
                return [_parse_track_db(db, wire_id, artwork_id) for wire_id,artwork_id in identities]
        except sqlite3.Error as exc:
            raise PhoneReadError('Could not read the uploaded track from the phone catalog.') from exc


def parse_track(database, wal, wire_id, artwork_id=None):
    return parse_tracks(database, wal, [(wire_id, artwork_id)])[0]


async def _hash_remote(afc, path, expected_size):
    path = '/' + path.lstrip('/')
    before = await afc.stat(path)
    if before.get('st_ifmt') != 'S_IFREG' or before.get('st_size') != expected_size:
        raise PhoneReadError('The transferred file has an unexpected size.')
    handle = await afc.fopen(path)
    digest = hashlib.sha256()
    remaining = expected_size
    try:
        while remaining:
            chunk = await afc.fread(handle, min(remaining, 1024 * 1024))
            if not chunk or len(chunk) > remaining:
                raise PhoneReadError('The transferred file is incomplete.')
            digest.update(chunk)
            remaining -= len(chunk)
    finally:
        await afc.fclose(handle)
    after = await afc.stat(path)
    if any(before.get(key) != after.get(key) for key in ('st_size','st_ifmt','st_mtime','st_birthtime')):
        raise PhoneReadError('The transferred file changed during readback.')
    return digest.hexdigest()


async def verify_inserts(afc, items, progress=None, stop=None, workers=4, fast_existing=False, reconnect=None):
    """Verify tracks against one snapshot; fast checks omit remote byte hashes."""
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 8:
        raise PhoneReadError('Choose 1 to 8 phone readback workers.')
    for attempt in (0, 1):
        try:
            db, wal = await stable_capture(afc)
            break
        except CONNECTION_ERRORS:
            if reconnect is None or attempt:
                raise
            afc = await reconnect()
    tracks = parse_tracks(db, wal, [(item[0],item[4]) for item in items])
    results = {}
    pos = 0
    reconnects = 0
    while pos < len(items):
        if stop is not None and stop.is_set():
            raise PhoneReadError('Stopped while checking songs already on the phone.')
        group = list(zip(items[pos:pos+workers], tracks[pos:pos+workers]))
        # Independent handles share AFC's packet demultiplexer; all reads
        # finish or are cancelled before leaving this group.
        check = _check_fast if fast_existing else _check_and_hash
        pending = [asyncio.create_task(check(afc, track, source, path, sha))
                   for (wire_id, source, path, sha, artwork_id), track in group]
        try:
            await asyncio.gather(*pending)
        except BaseException as error:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if isinstance(error, CONNECTION_ERRORS) and reconnect is not None and reconnects < 3:
                reconnects += 1
                afc = await reconnect()
                db, wal = await stable_capture(afc)
                tracks = parse_tracks(db, wal, [(item[0],item[4]) for item in items])
                continue
            raise
        for offset, ((wire_id, source, *_), _) in enumerate(group, 1):
            results[str(wire_id)] = True
            if progress is not None:
                progress(pos+offset,len(items),source.path.name)
        pos += workers
    return results


def preserved_rows(database, wal, existing_ids):
    """Snapshot existing tracks and playlists for before/after comparison."""
    with tempfile.TemporaryDirectory(prefix='music-preserved-') as tmp:
        path=Path(tmp)/'catalog.sqlite'
        path.write_bytes(database)
        if wal:
            Path(str(path)+'-wal').write_bytes(wal)
        try:
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
                db.execute('CREATE TEMP TABLE selected_ids(id INTEGER PRIMARY KEY)')
                db.executemany('INSERT INTO selected_ids VALUES (?)',[(int(i),) for i in existing_ids])
                db.execute('PRAGMA query_only=ON')
                rows={}
                for table in ('item','item_extra','item_store','item_playback','item_state'):
                    rows[table]=Counter(db.execute(f'''SELECT t.* FROM "{table}" t
                        JOIN item_store s ON s.item_pid=t.item_pid JOIN selected_ids x ON x.id=s.sync_id''').fetchall())
                for table in ('container','container_item'):
                    rows[table]=Counter(db.execute(f'SELECT * FROM "{table}"').fetchall())
                rows['track_artwork']=Counter(db.execute('''SELECT t.* FROM artwork_token t
                    JOIN item_store s ON s.item_pid=t.entity_pid JOIN selected_ids x ON x.id=s.sync_id
                    WHERE t.entity_type=0''').fetchall())
                rows['track_groups']=Counter(db.execute('''SELECT s.sync_id,ar.item_artist,ar.sort_item_artist,
                    al.album,al.sort_album,aa.album_artist,aa.sort_album_artist FROM item i
                    JOIN item_store s ON s.item_pid=i.item_pid JOIN selected_ids x ON x.id=s.sync_id
                    LEFT JOIN item_artist ar ON ar.item_artist_pid=i.item_artist_pid
                    LEFT JOIN album al ON al.album_pid=i.album_pid
                    LEFT JOIN album_artist aa ON aa.album_artist_pid=i.album_artist_pid''').fetchall())
                return rows
        except sqlite3.Error as exc:
            raise PhoneReadError('Could not verify the existing phone library.') from exc


def verify_preserved(before, after):
    for table, rows in before.items():
        if rows != after.get(table):
            raise PhoneReadError(f'Existing phone {table} changed during import; inspect before continuing.')


def _check_metadata(track, source, expected_path):
    if track.path != expected_path or track.size != source.path.stat().st_size:
        raise PhoneReadError('The uploaded track path or file size does not match.')
    if track.date_added != file_date_ns(source.path)//1_000_000_000 - 978307200:
        raise PhoneReadError('The uploaded song date does not match the original file date.')
    for label in ('title', 'artist', 'album'):
        expected = getattr(source, label)
        if expected and getattr(track, label) != expected:
            raise PhoneReadError('The uploaded ' + label + ' does not match the source.')
    tags=dict(source.tags)
    for field in ('genre','albumartist','composer','copyright','comment'):
        values=tags.get(field,())
        if len(values)==1 and values[0] and track.metadata[field] != values[0]:
            raise PhoneReadError('The uploaded '+field+' does not match the source.')
    for source_field,target,total_field in (('tracknumber','track_number','track_count'),('discnumber','disc_number','disc_count')):
        values=tags.get(source_field,())
        match=re.fullmatch(r'(\d+)(?:\s*/\s*(\d+))?',values[0].strip()) if len(values)==1 else None
        if match and int(match[1]) > 0:
            if track.metadata[target] != int(match[1]):
                raise PhoneReadError('The uploaded '+target+' does not match the source.')
            if match[2] and int(match[2]) >= int(match[1]) and track.metadata[total_field] != int(match[2]):
                raise PhoneReadError('The uploaded '+total_field+' does not match the source.')
    dates=tags.get('date',())
    if len(dates)==1 and re.match(r'^\d{4}(?:$|[- T])',dates[0]) and int(dates[0][:4]) > 0:
        if track.metadata['year'] != int(dates[0][:4]):
            raise PhoneReadError('The uploaded year does not match the source.')
    comp=tags.get('compilation',())
    if len(comp)==1 and comp[0]=='1' and not track.metadata['compilation']:
        raise PhoneReadError('The uploaded compilation flag does not match the source.')
    if source.cover and not track.artwork_path:
        raise PhoneReadError('The original embedded cover could not be verified.')


async def _check_fast(afc, track, source, expected_path, expected_sha):
    """Check the catalog and remote sizes, without claiming byte equality."""
    _check_metadata(track, source, expected_path)
    files = [(track.path, track.size)]
    if source.cover:
        files.append((track.artwork_path, len(phone_cover(source))))
    for path, size in files:
        info = await afc.stat('/' + path.lstrip('/'))
        if info.get('st_ifmt') != 'S_IFREG' or info.get('st_size') != size:
            raise PhoneReadError('The existing phone file has an unexpected size or type.')
    return True


async def _check_and_hash(afc, track, source, expected_path, expected_sha):
    _check_metadata(track, source, expected_path)
    if await _hash_remote(afc, track.path, track.size) != expected_sha:
        raise PhoneReadError('The uploaded song bytes do not match the source.')
    if source.cover:
        cover = phone_cover(source)
        if await _hash_remote(afc, track.artwork_path, len(cover)) != hashlib.sha256(cover).hexdigest():
            raise PhoneReadError('The uploaded artwork does not match the source.')
    return True


async def verify_insert(afc, wire_id, source, expected_path, expected_sha, artwork_id=None):
    """Require matching catalog metadata, exact audio and exact JPEG cover bytes."""
    await verify_inserts(afc, [(wire_id, source, expected_path, expected_sha, artwork_id)])
    return True
