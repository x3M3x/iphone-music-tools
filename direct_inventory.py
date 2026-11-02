"""Read-only identity allocation planning from a copied phone music catalog."""
import secrets
import re
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from collections import Counter

from direct_phone import PhoneReadError


@dataclass(frozen=True)
class Album:
    id: int
    name: str
    artist_id: int
    artist_name: str
    compilations: bool


@dataclass(frozen=True)
class Inventory:
    ids: frozenset[int]
    artists: tuple[tuple[int, str], ...]
    albums: tuple[Album, ...]
    artwork_ids: frozenset[int]
    used_paths: frozenset[str] = frozenset()
    item_artists: tuple[tuple[str, str], ...] = ()
    artist_sorts: tuple[tuple[int, str], ...] = ()


def active_albums(rows):
    """Ignore orphan duplicates and never reuse a duplicated blank album."""
    rows = tuple(rows)
    counts = Counter(int(row[0]) for row in rows if row[0])
    for pid,count in counts.items():
        if count < 2:
            continue
        group=[row for row in rows if row[0] and int(row[0])==pid]
        if sum(bool(row[5]) for row in group)>1 or (any(row[5] for row in group) and
                any((row[1] or '').strip() or (row[3] or '').strip() for row in group)):
            raise PhoneReadError('The phone has duplicate album identities. Import is paused until they are reconciled.')
    return tuple(Album(int(pid or 0), name or '', int(artist_pid or 0), artist_name or '', bool(comp))
                 for pid,name,artist_pid,artist_name,comp,used in rows if used and counts[int(pid or 0)] < 2)


def parse_inventory(database, wal):
    with tempfile.TemporaryDirectory(prefix='music-inventory-') as directory:
        path = Path(directory) / 'catalog.sqlite'
        path.write_bytes(database)
        if wal:
            Path(str(path) + '-wal').write_bytes(wal)
        try:
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as db:
                db.execute('PRAGMA query_only=ON')
                ids = set()
                for table, field in (('item_store','sync_id'), ('album','sync_id'),
                                     ('album_artist','sync_id'), ('container','sync_id'),
                                     ('db_info','db_pid'), ('db_info','primary_container_pid')):
                    for (value,) in db.execute(f'SELECT {field} FROM {table}'):
                        if value:
                            ids.add(int(value) & (2**64 - 1))
                artists = tuple((int(value or 0), name or '') for value,name in
                                db.execute('SELECT sync_id,album_artist FROM album_artist'))
                artist_sorts = tuple((int(value or 0), sort or '') for value,sort in
                                     db.execute('SELECT sync_id,sort_album_artist FROM album_artist'))
                item_artists = tuple((name or '', sort or '') for name,sort in
                                     db.execute('SELECT item_artist,sort_item_artist FROM item_artist'))
                albums = active_albums(db.execute('''
                    SELECT a.sync_id,a.album,ar.sync_id,ar.album_artist,a.all_compilations,
                           EXISTS(SELECT 1 FROM item i WHERE i.album_pid=a.album_pid)
                    FROM album a LEFT JOIN album_artist ar ON ar.album_artist_pid=a.album_artist_pid'''))
                artwork = set()
                for table in ('artwork', 'artwork_token'):
                    for (value,) in db.execute(f'SELECT artwork_token FROM {table} WHERE artwork_source_type=300'):
                        if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or str(int(value)) != value:
                            raise PhoneReadError('A phone artwork identity is not recognized.')
                        number = int(value)
                        if not 0 < number < 2**32:
                            raise PhoneReadError('A phone artwork identity is outside its range.')
                        artwork.add(number)
                paths = set()
                for location,base in db.execute('''SELECT e.location,b.path FROM item i
                    JOIN item_extra e USING(item_pid)
                    LEFT JOIN base_location b ON b.base_location_id=i.base_location_id
                    WHERE e.location IS NOT NULL AND e.location != '' '''):
                    location = str(location).replace(':','/').strip('/')
                    base = str(base or '').replace(':','/').strip('/')
                    if location.startswith('iTunes_Control/Music/'):
                        remote = location
                    elif re.fullmatch(r'F\d{2}',base,re.I):
                        remote = 'iTunes_Control/Music/' + base + '/' + location
                    elif re.fullmatch(r'iTunes_Control/Music/F\d{2}',base,re.I):
                        remote = base + '/' + location
                    else:
                        continue
                    if not re.fullmatch(r'iTunes_Control/Music/F\d{2}/[^/\\]+',remote,re.I) or '/..' in remote:
                        raise PhoneReadError('A phone music location is malformed.')
                    paths.add(remote.casefold())
                return Inventory(frozenset(ids), artists, albums, frozenset(artwork), frozenset(paths),
                                 item_artists, artist_sorts)
        except sqlite3.Error as exc:
            raise PhoneReadError('Could not inspect phone identity allocation.') from exc


def fresh_id(occupied):
    for _ in range(128):
        candidate = secrets.randbits(64)
        if candidate >= 1000 and candidate not in occupied:
            return candidate
    raise PhoneReadError('Could not reserve a unique music identity.')


def spelling_overrides(inventory, source):
    """Use an existing group's spelling for case/space variants only."""
    tags = dict(source.tags)
    overrides = {}
    def canonical(text, rows, label):
        if not text.strip():
            return None
        matches = [(name, sort) for name,sort in rows if name.strip().casefold() == text.strip().casefold()]
        if len(matches) > 1:
            raise PhoneReadError('The phone has an ambiguous '+label+' group; review before import.')
        return matches[0] if matches else None

    track_match = canonical(source.artist, inventory.item_artists, 'artist')
    if track_match and track_match[0] != source.artist:
        overrides['artist'] = track_match[0]
        overrides['artistsort'] = track_match[1] or track_match[0]
    explicit = tags.get('albumartist', ())
    if len(explicit) > 1:
        raise PhoneReadError('This song has multiple album artists and needs review.')
    group_name = explicit[0] if explicit else source.artist
    sorts = dict(inventory.artist_sorts)
    group_match = canonical(group_name, [(name, sorts.get(pid, '')) for pid,name in inventory.artists], 'album artist')
    if group_match and group_match[0] != group_name:
        if explicit:
            overrides['albumartist'] = group_match[0]
            overrides['albumartistsort'] = group_match[1] or group_match[0]
        else:
            if 'artist' in overrides and overrides['artist'] != group_match[0]:
                raise PhoneReadError('Artist and album artist group spellings disagree; review before import.')
            overrides['artist'] = group_match[0]
            overrides['artistsort'] = group_match[1] or group_match[0]
    return overrides


def apply_spelling(source, overrides):
    """Create a metadata-only view; the original audio file is never edited."""
    from dataclasses import is_dataclass, replace
    from types import SimpleNamespace
    allowed = {'artist','artistsort','albumartist','albumartistsort'}
    if not isinstance(overrides, dict) or set(overrides) - allowed or any(not isinstance(v,str) for v in overrides.values()):
        raise PhoneReadError('Invalid artist spelling plan.')
    tags = dict(source.tags)
    tags.update({key:(value,) for key,value in overrides.items()})
    attrs = {'tags':tuple(tags.items()), 'artist':overrides.get('artist',source.artist)}
    if is_dataclass(source):
        return replace(source, **attrs)
    return SimpleNamespace(**(vars(source) | attrs))


MASK = 2**64 - 1


class AllocationState:
    """Evolving view of the phone's groups while one run allocates IDs."""
    def __init__(self, inventory):
        self.ids=set(inventory.ids)
        self.artists=list(inventory.artists)
        self.albums=list(inventory.albums)
        self.item_artists=list(inventory.item_artists)
        self.artist_sorts=list(inventory.artist_sorts)
        self.artwork_ids=inventory.artwork_ids

    def view(self):
        return Inventory(frozenset(self.ids),tuple(self.artists),tuple(self.albums),
                         self.artwork_ids,frozenset(),tuple(self.item_artists),tuple(self.artist_sorts))

    def allocate(self, source):
        """Reserve IDs for one song and record the groups it creates."""
        overrides=spelling_overrides(self.view(),source)
        source=apply_spelling(source,overrides)
        track,album,artist=track_ids(self.view(),source)
        self.ids.update((track,album,artist))
        tags=dict(source.tags)
        name=(tags.get('albumartist') or (source.artist,))[0]
        if not any((i & MASK)==artist for i,_ in self.artists):
            self.artists.append((artist,name))
            self.artist_sorts.append((artist,(tags.get('albumartistsort') or tags.get('artistsort') or (name,))[0]))
        if source.artist and not any(n==source.artist for n,_ in self.item_artists):
            self.item_artists.append((source.artist,(tags.get('artistsort') or (source.artist,))[0]))
        if not any((a.id & MASK)==album for a in self.albums):
            self.albums.append(Album(album,source.album,artist,name,
                (tags.get('compilation') or ('0',))[0]=='1'))
        return track,album,artist,source,overrides


def track_ids(inventory, source):
    """Reserve IDs while reusing only an unambiguous exact album and artist."""
    if not source.title:
        raise PhoneReadError('This song has no title metadata and needs review.')
    tags = dict(source.tags)
    album_artists = tags.get('albumartist', ())
    if len(album_artists) > 1:
        raise PhoneReadError('This song has multiple album artists and needs review.')
    group_artist = album_artists[0] if album_artists else source.artist
    compilation = tags.get('compilation', ())
    if len(compilation) > 1 or (compilation and compilation[0] not in ('0','1')):
        raise PhoneReadError('The compilation tag needs review.')
    def same_group(left, right):
        return left.strip().casefold() == right.strip().casefold()

    def safe_spelling(left, right):
        return left == right or not left.strip() and not right.strip()

    matches = [(value, name) for value,name in inventory.artists if same_group(name, group_artist)]
    # The phone never joins a group whose name is blank: naming an existing
    # blank-named pid makes it create a second row with that pid (observed on
    # the phone with two blank-album songs of one artist). Always go fresh.
    if not group_artist.strip():
        matches = []
    if len(matches) > 1 or any(not safe_spelling(name, group_artist) or not value for value,name in matches):
        raise PhoneReadError('The phone has an ambiguous or different artist spelling; review before import.')
    occupied = set(inventory.ids)
    track = fresh_id(occupied)
    occupied.add(track)
    if matches:
        artist = matches[0][0] & (2**64 - 1)
    else:
        artist = fresh_id(occupied)
    occupied.add(artist)
    albums = [a for a in inventory.albums if same_group(a.name, source.album)
              and same_group(a.artist_name, group_artist)]
    if not source.album.strip():
        albums = []
    if len(albums) > 1 or any(not safe_spelling(a.name, source.album)
                              or not safe_spelling(a.artist_name, group_artist) or not a.id for a in albums):
        raise PhoneReadError('The phone has an ambiguous or different album spelling; review before import.')
    if albums and albums[0].compilations != bool(compilation and compilation[0] == '1'):
        raise PhoneReadError('The phone album has a different compilation setting; review before import.')
    if albums:
        album = albums[0].id & (2**64 - 1)
    else:
        album = fresh_id(occupied)
    if len({track, album, artist}) != 3:
        raise PhoneReadError('Phone music identities overlap unexpectedly.')
    return track, album, artist
