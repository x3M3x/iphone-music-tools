"""Check selected files locally before asking to import anything."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from types import SimpleNamespace

from direct_audio import inspect
from atc_staging import MAX_AUDIO
from direct_inventory import AllocationState
from mutagen import File as AudioFile
from music_importer import ImportErrorSafe, file_date_ns, content_hash
from music_importer import newest_first
from track_operation_preview import insert_preview


def scan(songs, stop, progress, workers=8):
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 16:
        raise ImportErrorSafe('Choose 1 to 16 format-check workers.')
    def check(song):
        try:
            if file_date_ns(song.path) != song.file_date_ns:
                raise ValueError('Saved file date changed; refresh the preview')
            if not 0 < song.path.stat().st_size <= MAX_AUDIO:
                raise ValueError('The song is empty or exceeds the 8 GiB transfer limit')
            source = inspect(song.path)
            insert_preview(source, database_id=1, library_id=2, anchor=1, track_id=3,
                           album_id=4, artist_id=5, artwork_id=100 if source.cover else None,
                           timestamp=datetime.now(timezone.utc))
            return None
        except Exception as exc:
            return {'file':str(song.path), 'error':str(exc)}

    failures=[]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(check,song) for song in songs]
        for i,future in enumerate(as_completed(futures),1):
            if stop.is_set():
                for remaining in futures:
                    remaining.cancel()
                raise ImportErrorSafe('Stopped during local file checks.')
            error=future.result()
            if error:
                failures.append(error)
            progress(i,len(songs),len(failures))
    return failures


def grouping_scan(songs, initial, verified_by_path=None):
    """Predict unsafe group-name conflicts before uploading any songs."""
    state=AllocationState(initial)
    failures=[]
    for song in reversed(newest_first(songs)):
        try:
            # A song already verified on this phone is reused, not inserted a
            # second time. Recheck its hash before excluding it from simulation.
            verified_sha = (verified_by_path or {}).get(str(song.path))
            if verified_sha is not None and content_hash(song) == verified_sha:
                continue
            audio=AudioFile(song.path,easy=True)
            if audio is None:
                raise ValueError('Cannot read audio metadata')
            get=lambda key: str((audio.get(key) or [''])[0])
            source=SimpleNamespace(title=get('title'),artist=get('artist'),album=get('album'),
                tags=tuple((str(k),tuple(map(str,v))) for k,v in audio.items()))
            track,album,artist,source,_=state.allocate(source)
        except Exception as exc:
            failures.append({'file':str(song.path),'error':str(exc)})
    return failures
