#!/usr/bin/env python3
"""Rebuild an export using only the source phone's audio, tags and artwork.

The original export and the phone are never modified. Playlist order is
recorded in M3U files; song filenames have no playlist-number prefixes.
"""
import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
from contextlib import closing
from io import BytesIO
from pathlib import Path

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import AfcService
from pymobiledevice3.exceptions import AfcFileNotFoundError
from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, APIC
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from PIL import Image

from phone_music_pull import parse_media_library, plan_export, sanitize
from complete_metadata import artwork as read_artwork, read_metadata, set_file_times, set_phone_added_time, update_tags

DB_PATH = '/iTunes_Control/iTunes/MediaLibrary.sqlitedb'
ART_ROOT = '/iTunes_Control/iTunes/Artwork/Originals/'


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def metadata_from_db(snapshot):
    library = parse_media_library(snapshot)
    metadata = {}
    with closing(sqlite3.connect(snapshot.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        for row in db.execute('''
            SELECT i.item_pid, e.title, ia.item_artist AS artist, al.album,
                   aw.relative_path AS art_path
            FROM item i JOIN item_extra e ON e.item_pid=i.item_pid
            LEFT JOIN item_artist ia ON ia.item_artist_pid=i.item_artist_pid
            LEFT JOIN album al ON al.album_pid=i.album_pid
            LEFT JOIN artwork_token at ON at.entity_pid=i.item_pid AND at.entity_type=0
            LEFT JOIN artwork aw ON aw.artwork_token=at.artwork_token
                AND aw.artwork_source_type=at.artwork_source_type
            WHERE e.media_kind=1
        '''):
            if row['item_pid'] in metadata:
                raise ValueError('Ambiguous artwork mapping for a track; stopping safely')
            metadata[row['item_pid']] = dict(row)
    return library, metadata


def safe_title(title):
    title = sanitize(title or 'Unknown Title')[:110].rstrip(' .') or 'Unknown Title'
    if re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])', title):
        title = '_' + title
    return title


def destinations(library, only_playlist=None, include_protected=False, protected=None):
    plan = plan_export(library)
    groups = [(name, [tid for _, tid in members]) for name, members in plan['playlists']
              if (name.strip().casefold() == only_playlist.strip().casefold()
                  if only_playlist is not None else
                  (include_protected or not protected or name.strip().casefold() != protected.casefold()))]
    if only_playlist is None:
        groups.append(('All the rest', plan['rest']))
    mapping, playlists = {}, {}
    for name, ids in groups:
        folder = Path('All the rest') if name == 'All the rest' else Path('Playlists') / sanitize(name)
        used = {}
        entries = []
        for tid in ids:
            track = library.tracks[tid]
            if not track.path or track.is_drm:
                continue
            ext = Path(track.path).suffix.lower()
            if ext not in ('.m4a', '.mp3'):
                continue
            base = safe_title(track.title)
            filename = base + ext
            key = filename.casefold()
            if key in used and used[key] != tid:
                filename = f'{base} - {safe_title(track.artist)}{ext}'
                key = filename.casefold()
            if key in used and used[key] != tid:
                filename = f'{base} - {tid}{ext}'
                key = filename.casefold()
            used[key] = tid
            dest = folder / filename
            if dest not in mapping.setdefault(tid, []):
                mapping[tid].append(dest)
            entries.append((tid, dest))
        if name != 'All the rest':
            playlists[name] = entries
    return mapping, playlists


def picture_type(blob):
    if not blob or len(blob) > 25 * 1024 * 1024:
        return None
    try:
        with Image.open(BytesIO(blob)) as image:
            fmt = image.format
            image.verify()
        return {'JPEG': 'image/jpeg', 'PNG': 'image/png'}.get(fmt)
    except Exception:
        return None


def embed_tags(path, metadata, artwork):
    mime = picture_type(artwork) if artwork else None
    if artwork and not mime:
        raise ValueError('Unsupported original artwork format')
    if path.suffix.lower() == '.mp3':
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        for key, cls, value in [('TIT2', TIT2, metadata['title']),
                                ('TPE1', TPE1, metadata['artist']),
                                ('TALB', TALB, metadata['album'])]:
            if value and value.strip():
                tags.delall(key)
                tags.add(cls(encoding=3, text=value))
        if artwork:
            other_pictures = [pic for pic in tags.getall('APIC') if pic.type != 3]
            tags.delall('APIC')
            for pic in other_pictures:
                tags.add(pic)
            tags.add(APIC(encoding=3, mime=mime, type=3, desc='', data=artwork))
        tags.save(path)
    elif path.suffix.lower() == '.m4a':
        tags = MP4(path)
        if tags.tags is None:
            tags.add_tags()
        for key, value in [('\xa9nam', metadata['title']),
                           ('\xa9ART', metadata['artist']), ('\xa9alb', metadata['album'])]:
            if value and value.strip():
                tags[key] = [value]
        if artwork:
            fmt = MP4Cover.FORMAT_PNG if mime == 'image/png' else MP4Cover.FORMAT_JPEG
            existing = tags.get('covr', [])
            tags['covr'] = [MP4Cover(artwork, imageformat=fmt)] + list(existing[1:])
        tags.save()
    else:
        raise ValueError('Unsupported audio type')


def has_embedded_art(path):
    try:
        if path.suffix.lower() == '.mp3':
            return any(picture_type(pic.data) for pic in ID3(path).getall('APIC'))
        if path.suffix.lower() == '.m4a':
            return any(picture_type(bytes(pic)) for pic in MP4(path).get('covr', []))
    except Exception:
        return None
    return False


def probe_tags(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format_tags',
                             '-of', 'json', str(path)], capture_output=True, check=True, timeout=60,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return json.loads(result.stdout.decode('utf-8', errors='strict')).get('format', {}).get('tags', {})


def fallback_remux(original, target, metadata, artwork, temp_dir):
    source_tags = probe_tags(original)
    cmd = ['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(original)]
    if artwork:
        image = temp_dir / ('cover.png' if picture_type(artwork) == 'image/png' else 'cover.jpg')
        image.write_bytes(artwork)
        cmd += ['-i', str(image), '-map', '0:a', '-map', '1:v:0', '-disposition:v:0', 'attached_pic']
    else:
        cmd += ['-map', '0']
    cmd += ['-map_metadata', '0', '-c', 'copy']
    for key in ('title', 'artist', 'album'):
        if metadata[key] and metadata[key].strip():
            cmd += ['-metadata', f'{key}={metadata[key]}']
    cmd += [str(target)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=180,
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if not target.exists() or target.stat().st_size == 0:
        raise ValueError('ffmpeg produced no audio file')
    # Preserve free-form tags such as iTunSMPB that ffmpeg otherwise discards.
    ignored = {'title', 'artist', 'album', 'encoder', 'major_brand', 'minor_version', 'compatible_brands'}
    result_tags = probe_tags(target)
    missing = {key: value for key, value in source_tags.items()
               if key.lower() not in ignored and result_tags.get(key) != value}
    if missing and target.suffix.lower() == '.m4a':
        tags = MP4(target)
        for key, value in missing.items():
            tags[f'----:com.apple.iTunes:{key}'] = [MP4FreeForm(str(value).encode('utf-8'))]
        tags.save()
        result_tags = probe_tags(target)
        missing = {key: value for key, value in missing.items() if result_tags.get(key) != value}
    if missing:
        raise ValueError('Could not preserve source metadata: ' + ', '.join(missing))


def publish(source, target, date_added=None, preserve_times=False):
    """Create a destination atomically, never replacing an existing file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(f'.{uuid.uuid4().hex}.part')
    try:
        shutil.copyfile(source, part)
        if date_added:
            set_phone_added_time(part, date_added)
        elif preserve_times:
            stat = source.stat()
            set_file_times(part, getattr(stat, 'st_birthtime', stat.st_ctime), stat.st_mtime)
        os.link(part, target)
    finally:
        part.unlink(missing_ok=True)


def copy_protected_playlist(original, output, protected):
    if not protected or Path(protected).name != protected or protected in ('.', '..'):
        raise ValueError('Choose a valid playlist folder name to preserve.')
    source = original / 'Playlists' / protected
    dest = output / 'Playlists' / protected
    if not source.is_dir():
        raise ValueError('The selected playlist folder was not found in the existing export.')
    count = 0
    for root, dirs, files in os.walk(source, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
        for name in files:
            item = Path(root) / name
            if item.is_symlink():
                continue
            target = dest / item.relative_to(source)
            if target.exists():
                if sha256(item) != sha256(target):
                    raise FileExistsError(f'Protected playlist copy differs: {target.name}')
            else:
                publish(item, target, preserve_times=True)
            count += 1
    return count


async def rebuild(args):
    output = Path(args.out).resolve()
    original = Path(args.original).resolve()
    if output == original or original in output.parents or output in original.parents:
        raise ValueError('The corrected output must be separate from the original export.')
    output.mkdir(parents=True, exist_ok=True)
    protected = getattr(args, 'protected_playlist', None)
    protected_count = (0 if not protected or getattr(args, 'no_protected_copy', False) else
                       await asyncio.to_thread(copy_protected_playlist, original, output, protected))
    print(f'Copied {protected_count} protected playlist files unchanged.', flush=True)
    lockdown = await create_using_usbmux(serial=args.serial, autopair=False, connection_type='USB')
    if not lockdown.paired:
        await lockdown.close()
        raise RuntimeError('The source phone is not paired with this computer.')
    afc = AfcService(lockdown)
    await afc.connect()
    try:
        with tempfile.TemporaryDirectory(prefix='music-rebuild-') as tmp:
            tmp = Path(tmp)
            snapshot = tmp / 'MediaLibrary.sqlitedb'
            await afc.pull(DB_PATH, str(snapshot), progress_bar=False)
            try:
                await afc.pull(DB_PATH + '-wal', str(snapshot) + '-wal', progress_bar=False)
            except AfcFileNotFoundError:
                pass
            library, metadata = metadata_from_db(snapshot)
            details = read_metadata(snapshot)
            mapping, playlists = destinations(library, getattr(args, 'only_playlist', None),
                                              include_protected=(protected_count == 0), protected=protected)
            if getattr(args, 'only_playlist', None) and not playlists:
                raise ValueError('The requested playlist was not found in the phone library.')
            ids = list(mapping)
            if args.limit:
                ids = ids[:args.limit]
            print(f'Preparing {len(ids)} source tracks with {args.jobs} transfers.', flush=True)
            records, failures = {}, []
            gate = asyncio.Semaphore(args.jobs)
            lock = asyncio.Lock()

            def progress():
                if getattr(args, 'progress_json', False):
                    print('PROGRESS ' + json.dumps({'completed': len(records), 'failed': len(failures),
                                                  'total': len(ids)}), flush=True)
            progress()

            async def process(tid):
                async with gate:
                    track, meta = library.tracks[tid], metadata[tid]
                    try:
                        with tempfile.TemporaryDirectory(prefix='track-', dir=tmp) as td:
                            td = Path(td)
                            src = td / ('source' + Path(track.path).suffix.lower())
                            info = await afc.stat(track.path)
                            expected = int(info['st_size'])
                            await afc.pull(track.path, str(src), progress_bar=False)
                            if src.stat().st_size != expected:
                                raise ValueError('Incomplete audio transfer')
                            image, art_state = None, 'not available in library'
                            rel = meta.get('art_path') or ''
                            if re.fullmatch(r'[0-9a-fA-F]{2}/[0-9a-fA-F]{38}', rel):
                                try:
                                    image = await afc.get_file_contents(ART_ROOT + rel)
                                    if not picture_type(image):
                                        image, art_state = None, 'unsupported original artwork format'
                                    else:
                                        art_state = 'phone original'
                                except AfcFileNotFoundError:
                                    art_state = 'phone original artwork not accessible'
                            tagged = td / ('tagged' + src.suffix)
                            shutil.copyfile(src, tagged)
                            try:
                                await asyncio.to_thread(embed_tags, tagged, meta, image)
                            except Exception:
                                tagged.unlink(missing_ok=True)
                                await asyncio.to_thread(fallback_remux, src, tagged, meta, image, td)
                            covers_before = await asyncio.to_thread(read_artwork, tagged)
                            await asyncio.to_thread(update_tags, tagged, details[tid])
                            if covers_before != await asyncio.to_thread(read_artwork, tagged):
                                raise ValueError('Artwork changed while writing metadata')
                            if image is None:
                                embedded = await asyncio.to_thread(has_embedded_art, tagged)
                                if embedded is True:
                                    art_state = 'original embedded cover retained'
                                elif embedded is None:
                                    art_state = 'embedded cover could not be verified'
                            digest = await asyncio.to_thread(sha256, tagged)
                            for relative in mapping[tid]:
                                target = output / relative
                                if target.exists():
                                    if await asyncio.to_thread(sha256, target) != digest:
                                        raise FileExistsError(f'Existing file differs: {relative}')
                                else:
                                    await asyncio.to_thread(publish, tagged, target,
                                                            date_added=details[tid]['date_added'])
                            async with lock:
                                records[tid] = {'id': tid, 'files': [str(x) for x in mapping[tid]],
                                                'sha256': digest, 'artwork': art_state,
                                                'phone_date_added_2001_seconds': details[tid]['date_added']}
                                progress()
                                if len(records) % 100 == 0:
                                    print(f'Completed {len(records)}/{len(ids)} tracks', flush=True)
                    except Exception as exc:
                        async with lock:
                            failures.append({'id': tid, 'error': f'{type(exc).__name__}: {exc}'})
                            progress()

            await asyncio.gather(*(process(tid) for tid in ids))
            for name, entries in playlists.items():
                playlist_file = output / (sanitize(name) + '.m3u8')
                content = '#EXTM3U\n' + ''.join(str(dest).replace('\\', '/') + '\n'
                                               for tid, dest in entries if tid in records)
                if playlist_file.exists() and playlist_file.read_text(encoding='utf-8-sig') != content:
                    failures.append({'playlist': name, 'error': 'Existing playlist differs; left untouched'})
                elif not playlist_file.exists():
                    playlist_file.write_text(content, encoding='utf-8-sig')
            report = {'tracks_completed': len(records), 'failures': failures,
                      'protected_playlist_copied_unchanged': protected_count,
                      'artwork_originals': sum(v['artwork'] == 'phone original' for v in records.values()),
                      'tracks': list(records.values())}
            report_path = output / '_rebuild_report.json'
            if report_path.exists():
                report_path = output / f'_rebuild_report_{uuid.uuid4().hex}.json'
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f"Finished: {len(records)} tracks; {report['artwork_originals']} phone-original covers; {len(failures)} failures.", flush=True)
            return 1 if failures else 0
    finally:
        await afc.close()
        await lockdown.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--serial', required=True)
    p.add_argument('--original', default='MusicExport')
    p.add_argument('--out', default='MusicExport_Corrected')
    p.add_argument('--jobs', type=int, default=4)
    p.add_argument('--limit', type=int)
    p.add_argument('--only-playlist', help='rebuild only the named phone playlist')
    p.add_argument('--no-protected-copy', action='store_true', help='do not copy the existing protected folder')
    p.add_argument('--protected-playlist', help='name of playlist folder to preserve from the existing export')
    p.add_argument('--progress-json', action='store_true', help=argparse.SUPPRESS)
    args = p.parse_args()
    if args.jobs < 1 or args.jobs > 8 or (args.limit is not None and args.limit < 1):
        p.error('Use 1–8 jobs and a positive limit.')
    raise SystemExit(asyncio.run(rebuild(args)))


if __name__ == '__main__':
    main()
