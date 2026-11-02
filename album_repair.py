"""Plan and verify a repair for an interrupted single-track test.

This module only reads local database snapshots and builds a plist. The caller
must hold the phone sync lock, durably journal the operation and verify it.
"""
import plistlib
import secrets
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class RepairError(Exception):
    pass


TABLES = ('item','item_extra','item_store','item_playback','item_state',
          'item_artist','album_artist','container','container_item',
          'artwork','artwork_token','best_artwork_token')


@dataclass(frozen=True)
class RepairPlan:
    track_id: int
    item_id: int
    album_row_id: int
    empty_row_id: int
    old_id: int
    new_id: int
    baseline: dict


def snapshot(database, wal):
    """Read bounded copies and return typed rows for strict comparison."""
    with tempfile.TemporaryDirectory(prefix='music-repair-') as directory:
        path = Path(directory) / 'catalog.sqlite'
        path.write_bytes(database)
        if wal:
            Path(str(path)+'-wal').write_bytes(wal)
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RepairError('The copied music database failed its integrity check.')
            tables = {}
            for table in TABLES + ('album','db_info'):
                tables[table] = tuple(db.execute(f'SELECT * FROM "{table}"').fetchall())
            albums = db.execute('''SELECT a.album_pid,a.sync_id,a.album,a.album_artist_pid,
                a.representative_item_pid,COUNT(i.item_pid)
                FROM album a LEFT JOIN item i ON i.album_pid=a.album_pid
                GROUP BY a.album_pid''').fetchall()
            target_map = db.execute('''SELECT s.sync_id,i.item_pid,i.album_pid,e.title
                FROM item_store s JOIN item i USING(item_pid) JOIN item_extra e USING(item_pid)
                WHERE e.media_kind=1 AND i.in_my_library=1''').fetchall()
            occupied = set()
            for table, field in (('item_store','sync_id'),('album','sync_id'),
                                 ('album_artist','sync_id'),('container','sync_id'),
                                 ('db_info','db_pid'),('db_info','primary_container_pid')):
                occupied.update((int(row[0]) & (2**64-1)) for row in db.execute(
                    f'SELECT "{field}" FROM "{table}"') if row[0])
            album_columns = [r[1] for r in db.execute('PRAGMA table_info(album)')]
            item_columns = [r[1] for r in db.execute('PRAGMA table_info(item)')]
            return {'tables':tables,'albums':albums,'tracks':target_map,
                    'occupied':occupied,'album_columns':album_columns,'item_columns':item_columns}


def plan(view, track_id, expected_title, random_id=None):
    targets = [r for r in view['tracks'] if r[0] == track_id]
    if len(targets) != 1 or targets[0][3] != expected_title:
        raise RepairError('The test song could not be identified uniquely.')
    _, item_id, album_row, _ = targets[0]
    albums = view['albums']
    selected = [r for r in albums if r[0] == album_row]
    if len(selected) != 1:
        raise RepairError('The test album could not be identified uniquely.')
    selected = selected[0]
    duplicates = [r for r in albums if r[1] == selected[1]]
    if (not selected[1] or len(duplicates) != 2 or selected[5] != 1
            or selected[4] != item_id):
        raise RepairError('The duplicate album is not the expected single-track test case.')
    empty = [r for r in duplicates if r[0] != album_row]
    if (len(empty) != 1 or empty[0][5] != 0 or empty[0][4] not in (0,None)
            or empty[0][2] != selected[2] or empty[0][3] != selected[3]):
        raise RepairError('The duplicate album is not the expected empty record.')
    counts = Counter(r[1] for r in albums if r[1])
    if any(value > 1 and key != selected[1] for key,value in counts.items()):
        raise RepairError('Other duplicate album identities require separate review.')
    candidate = random_id if random_id is not None else secrets.randbits(63)
    if isinstance(candidate,bool) or not isinstance(candidate,int) or not 1000 <= candidate < 2**63 or candidate in view['occupied']:
        raise RepairError('The new album ID is not unused.')
    return RepairPlan(track_id,item_id,album_row,empty[0][0],selected[1],candidate,view)


def operation(repair, database_id, library_id, anchor, now=None):
    if not database_id or not library_id or not 0 <= anchor < 2**32-1:
        raise RepairError('The phone library identity is unavailable.')
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)
    root = {'revision':anchor+1,'timestamp':timestamp,'operations':[
        {'operation':'update_db_info','pid':database_id,'db_info':{'primary_container_pid':library_id}},
        {'operation':'update_track','pid':repair.track_id,'item':{'album_pid':repair.new_id}}]}
    return plistlib.dumps(root,fmt=plistlib.FMT_BINARY,sort_keys=True)


def verify(repair, after):
    """Require unchanged protected tables and only the intended album ID change."""
    before = repair.baseline
    for table in TABLES + ('db_info',):
        if Counter(before['tables'][table]) != Counter(after['tables'][table]):
            raise RepairError(f'The phone {table} changed unexpectedly; inspect before continuing.')
    columns = before['album_columns']
    if columns != after['album_columns']:
        raise RepairError('The phone album schema changed.')
    row_index, sync_index = columns.index('album_pid'), columns.index('sync_id')
    expected=[]
    for row in before['tables']['album']:
        row=list(row)
        if row[row_index] == repair.album_row_id:
            if row[sync_index] != repair.old_id:
                raise RepairError('The old album ID changed.')
            row[sync_index] = repair.new_id
        expected.append(tuple(row))
    if Counter(expected) != Counter(after['tables']['album']):
        raise RepairError('The album readback differs from the one requested change.')
    duplicates=Counter(r[1] for r in after['albums'] if r[1])
    if any(count > 1 for count in duplicates.values()):
        raise RepairError('The phone still has duplicate album identities.')
    return True


def plan_duplicates(view):
    """Re-key every extra row of each duplicate album sync-id group."""
    groups = {}
    for row in view['albums']:
        if row[1]:
            groups.setdefault(row[1], []).append(row)
    duplicates = {sid: rows for sid, rows in groups.items() if len(rows) > 1}
    if not duplicates:
        raise RepairError('No duplicate album identities were found.')
    by_item = {item_pid: (sync_id, title) for sync_id, item_pid, _, title in view['tracks']}
    occupied = set(view['occupied'])
    rekeys = []
    for sid, rows in sorted(duplicates.items()):
        # An empty row has no member track to re-key, so it must keep the
        # original id; every filled row moves to its own fresh id.
        empty = [row for row in rows if row[5] == 0]
        if len(empty) > 1:
            raise RepairError('Two empty duplicate album rows cannot be repaired safely.')
        keeper = empty[0] if empty else rows[0]
        for row in rows:
            if row[0] == keeper[0]:
                continue
            album_pid, _, name, _, representative, members = row
            if members < 1 or representative in (0, None) or representative not in by_item:
                raise RepairError('A duplicate album row has no member track; review it manually.')
            candidate = secrets.randbits(63)
            if not 1000 <= candidate < 2**63 or candidate in occupied:
                raise RepairError('Could not reserve a fresh album identity.')
            occupied.add(candidate)
            rekeys.append({'track_id': by_item[representative][0], 'album_row': album_pid,
                           'old_id': sid, 'new_id': candidate, 'title': by_item[representative][1]})
    return rekeys, view


def operation_duplicates(rekeys, database_id, library_id, anchor, now=None):
    if not rekeys or len(rekeys) > 498:
        raise RepairError('Invalid duplicate-repair size.')
    if not database_id or not library_id or not 0 <= anchor < 2**32-1:
        raise RepairError('The phone library identity is unavailable.')
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)
    operations = [{'operation':'update_db_info','pid':database_id,
                   'db_info':{'primary_container_pid':library_id}}]
    for rekey in rekeys:
        operations.append({'operation':'update_track','pid':rekey['track_id'],
                           'item':{'album_pid':rekey['new_id']}})
    return plistlib.dumps({'revision':anchor+1,'timestamp':timestamp,'operations':operations},
                          fmt=plistlib.FMT_BINARY, sort_keys=True)


def verify_duplicates(rekeys, before, after):
    """Require unchanged protected tables and exactly the planned re-keys."""
    for table in TABLES + ('db_info',):
        if Counter(before['tables'][table]) != Counter(after['tables'][table]):
            raise RepairError(f'The phone {table} changed unexpectedly; inspect before continuing.')
    if before['album_columns'] != after['album_columns']:
        raise RepairError('The phone album schema changed.')
    row_index, sync_index = before['album_columns'].index('album_pid'), before['album_columns'].index('sync_id')
    expected = []
    for row in before['tables']['album']:
        row = list(row)
        for rekey in rekeys:
            if row[row_index] == rekey['album_row']:
                if row[sync_index] != rekey['old_id']:
                    raise RepairError('A duplicate album row changed unexpectedly.')
                row[sync_index] = rekey['new_id']
        expected.append(tuple(row))
    if Counter(expected) != Counter(after['tables']['album']):
        raise RepairError('The album readback differs from the requested re-keys.')
    counts = Counter(r[1] for r in after['albums'] if r[1])
    if any(value > 1 for value in counts.values()):
        raise RepairError('The phone still has duplicate album identities.')
    return True


def verify_split_duplicates(rekeys, before, after):
    """Verify the phone split selected single-track albums, preserving all else."""
    for table in TABLES + ('db_info',):
        if table != 'item' and Counter(before['tables'][table]) != Counter(after['tables'][table]):
            raise RepairError(f'The phone {table} changed unexpectedly.')
    if before['album_columns'] != after['album_columns'] or before['item_columns'] != after['item_columns']:
        raise RepairError('The phone database schema changed.')
    ac=before['album_columns']; ic=before['item_columns']
    ai,si,ri=map(ac.index,('album_pid','sync_id','representative_item_pid'))
    ii,ia=map(ic.index,('item_pid','album_pid'))
    old_albums={row[ai]:row for row in before['tables']['album']}
    before_tracks={row[0]:row for row in before['tracks']}
    after_tracks={row[0]:row for row in after['tracks']}
    expected_albums=Counter(before['tables']['album'])
    expected_items=Counter(before['tables']['item'])
    items={row[ii]:row for row in before['tables']['item']}
    for rekey in rekeys:
        old=old_albums[rekey['album_row']]
        track=before_tracks[rekey['track_id']]
        new_track=after_tracks[rekey['track_id']]
        if (track[2]!=rekey['album_row'] or old[si]!=rekey['old_id'] or old[ri]!=track[1]
                or new_track[:2]!=track[:2] or new_track[3]!=track[3]):
            raise RepairError('A repaired track no longer matches its plan.')
        new_row_id=new_track[2]
        if new_row_id in old_albums or new_row_id==rekey['album_row']:
            raise RepairError('The phone reused an album row unexpectedly.')
        item=items[track[1]]
        if item[ia]!=rekey['album_row']:
            raise RepairError('The original album membership changed.')
        expected_items.subtract([item]); moved=list(item); moved[ia]=new_row_id; expected_items.update([tuple(moved)])
        expected_albums.subtract([old])
        emptied=list(old); emptied[ri]=0; expected_albums.update([tuple(emptied)])
        created=list(old); created[ai]=new_row_id; created[si]=rekey['new_id']; expected_albums.update([tuple(created)])
    if +expected_items != Counter(after['tables']['item']) or +expected_albums != Counter(after['tables']['album']):
        raise RepairError('The album split differs from the planned changes.')
    return True
