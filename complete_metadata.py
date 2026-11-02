#!/usr/bin/env python3
"""Add the source phone's remaining metadata to an already rebuilt export.

Only files listed in the rebuild report are changed. The protected playlist
is not listed there and is never opened for writing.
"""
import argparse
import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from mutagen.id3 import ID3, TALB, TCOM, TCON, TDRC, TPE2, TPOS, TRCK
from mutagen.mp4 import MP4


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def read_metadata(snapshot):
    with closing(sqlite3.connect(snapshot.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('''
            SELECT i.item_pid, i.track_number, i.disc_number, i.date_added, e.track_count, e.disc_count, e.year,
                   g.genre, aa.album_artist, c.composer
            FROM item i JOIN item_extra e ON e.item_pid=i.item_pid
            LEFT JOIN genre g ON g.genre_id=i.genre_id
            LEFT JOIN album_artist aa ON aa.album_artist_pid=i.album_artist_pid
            LEFT JOIN composer c ON c.composer_pid=i.composer_pid
            WHERE e.media_kind=1
        ''')
        return {row['item_pid']: dict(row) for row in rows}


def artwork(path):
    if path.suffix.lower() == '.mp3':
        return [(str(x.mime), int(x.type), bytes(x.data)) for x in ID3(path).getall('APIC')]
    if path.suffix.lower() == '.m4a':
        return [bytes(x) for x in MP4(path).get('covr', [])]
    raise ValueError('Unsupported audio type')


def positive(value):
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return value if 0 < value <= 65535 else 0


def set_file_times(path, creation_unix, modified_unix):
    os.utime(path, (path.stat().st_atime, modified_unix))
    if sys.platform == 'win32':
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                           wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        set_time = kernel.SetFileTime
        set_time.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                             ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
        set_time.restype = wintypes.BOOL
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        handle = create(str(path), 0x100, 7, None, 3, 0x80, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            def filetime(unix):
                ticks = int(round((unix + 11644473600) * 10000000))
                return wintypes.FILETIME(ticks & 0xffffffff, ticks >> 32)
            created, modified = filetime(creation_unix), filetime(modified_unix)
            if not set_time(handle, ctypes.byref(created), None, ctypes.byref(modified)):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            close(handle)


def set_phone_added_time(path, value):
    """Preserve the phone's library-added time as file creation/modification time."""
    if value is None or float(value) <= 0:
        return None
    unix = float(value) + 978307200  # Apple's 2001-01-01 epoch
    date = datetime.fromtimestamp(unix, timezone.utc)
    set_file_times(path, unix, unix)
    return date.isoformat()


def update_tags(path, meta):
    genre = meta['genre'] or ''
    artist = meta['album_artist'] or ''
    composer = meta['composer'] or ''
    year, number, total = map(positive, (meta['year'], meta['track_number'], meta['track_count']))
    disc, discs = map(positive, (meta['disc_number'], meta['disc_count']))
    if path.suffix.lower() == '.mp3':
        tags = ID3(path)
        for key, cls, value in [('TCON', TCON, genre), ('TPE2', TPE2, artist),
                                ('TCOM', TCOM, composer), ('TDRC', TDRC, str(year) if year else '')]:
            if value and value.strip():
                tags.delall(key)
                tags.add(cls(encoding=3, text=value))
        for key, cls, value, maximum in [('TRCK', TRCK, number, total), ('TPOS', TPOS, disc, discs)]:
            if value:
                tags.delall(key)
                tags.add(cls(encoding=3, text=f'{value}/{maximum}' if maximum else str(value)))
        tags.save(path)
    elif path.suffix.lower() == '.m4a':
        tags = MP4(path)
        for key, value in [('\xa9gen', genre), ('aART', artist), ('\xa9wrt', composer),
                           ('\xa9day', str(year) if year else '')]:
            if value and value.strip():
                tags[key] = [value]
        if number:
            tags['trkn'] = [(number, total)]
        if disc:
            tags['disk'] = [(disc, discs)]
        tags.save()
    else:
        raise ValueError('Unsupported audio type')


def safely_update(target, expected_hash, meta):
    if digest(target) != expected_hash:
        raise ValueError('File changed since rebuild; left untouched')
    before = artwork(target)
    fd, name = tempfile.mkstemp(prefix='.metadata-', suffix=target.suffix, dir=target.parent)
    os.close(fd)
    temp = Path(name)
    try:
        shutil.copyfile(target, temp)
        update_tags(temp, meta)
        if artwork(temp) != before:
            raise ValueError('Artwork changed unexpectedly; original kept')
        added = set_phone_added_time(temp, meta.get('date_added'))
        if digest(target) != expected_hash:
            raise ValueError('File changed during update; left untouched')
        new_hash = digest(temp)
        os.replace(temp, target)
        return new_hash, added
    finally:
        temp.unlink(missing_ok=True)


def run(snapshot, output):
    metadata = read_metadata(snapshot)
    report = json.loads((output / '_rebuild_report.json').read_text(encoding='utf-8'))
    dest = output / '_metadata_report.json'
    if dest.exists():
        raise FileExistsError('Metadata report exists; stopping without overwriting it')
    updated, failures = [], []
    for record in report['tracks']:
        try:
            meta = metadata[record['id']]
            for relative in record['files']:
                rel = Path(relative)
                if rel.is_absolute() or '..' in rel.parts or (len(rel.parts) > 1 and rel.parts[0] == 'Playlists' and rel.parts[1] == 'اناشيد'):
                    raise ValueError('Unsafe or protected path in report')
                target = output / rel
                new_hash, added = safely_update(target, record['sha256'], meta)
                updated.append({'id': record['id'], 'file': str(rel), 'sha256': new_hash,
                                'phone_date_added_utc': added})
                if len(updated) % 250 == 0:
                    print(f'Completed metadata for {len(updated)} files', flush=True)
        except Exception as exc:
            failures.append({'id': record['id'], 'error': f'{type(exc).__name__}: {exc}'})
    dest.write_text(json.dumps({'updated': updated, 'failures': failures}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Finished metadata: {len(updated)} files, {len(failures)} failures.', flush=True)
    return 1 if failures else 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--snapshot', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    args = p.parse_args()
    raise SystemExit(run(args.snapshot, args.out.resolve()))


if __name__ == '__main__':
    main()
