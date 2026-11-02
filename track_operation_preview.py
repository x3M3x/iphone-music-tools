"""Build an offline single-song insert candidate; never send to a phone.

The caller must allocate identities from a fresh catalog, bind the file bytes,
and verify codec timing, upload, artwork and catalog readback before use.
"""
from datetime import datetime, timezone
import plistlib
import re

from playlist_operation_preview import _signed_pid
from direct_audio import phone_cover
from music_importer import file_date_ns

FORMATS = {'mpeg1-layer3': (301, 'MPEG audio file'),
           'mpeg2-layer3': (302, 'MPEG audio file'),
           'mpeg2.5-layer3': (303, 'MPEG audio file'),
           'aac-lc': (502, 'AAC audio file'), 'he-aac': (508, 'AAC audio file'),
           'he-aac-v2': (509, 'AAC audio file')}


def insert_preview(source, *, database_id, library_id, anchor, track_id,
                   album_id, artist_id, artwork_id, timestamp):
    """Construct candidate wire metadata without allocating IDs or mutating.

    Empty tags stay empty. Multiple distinct values require review; guessed
    title, artist and album metadata are never invented.
    """
    db, library, track, album, artist = map(_signed_pid,
                                             (database_id, library_id, track_id, album_id, artist_id))
    if len({track, album, artist}) != 3:
        raise ValueError('Track, album and artist IDs must be distinct')
    if isinstance(anchor, bool) or not isinstance(anchor, int) or not 0 <= anchor < 2**32 - 1:
        raise ValueError('Invalid anchor')
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError('Timestamp must have a timezone')
    if source.codec not in FORMATS or source.sample_rate <= 0 or source.coded_samples <= 0:
        raise ValueError('Unsupported audio format')
    if source.cover:
        if isinstance(artwork_id, bool) or not isinstance(artwork_id, int) or not 0 < artwork_id < 2**32:
            raise ValueError('A cover requires a reserved artwork ID')
    elif artwork_id is not None:
        raise ValueError('Artwork ID supplied without a cover')
    tags = dict(source.tags)

    def value(key):
        vals = tags.get(key, ())
        if len(vals) > 1:
            raise ValueError('Multiple ' + key + ' tags require review')
        text = vals[0] if vals else ''
        if len(text) > 4096 or any(ord(char) < 32 for char in text):
            raise ValueError('Unsupported ' + key + ' tag')
        return text

    sent_cover = phone_cover(source)
    checksum = len(sent_cover) + (17 if source.path.suffix.lower() == '.m4a' else 1) if sent_cover else 0
    item = {'album_pid':album, 'artist_pid':artist, 'is_song':True,
            'remember_bookmark':False, 'artwork_checksum':checksum,
            'total_time_ms':source.coded_samples * 1000 // source.sample_rate}
    saved_ns = file_date_ns(source.path)
    if saved_ns <= 0:
        raise ValueError('The source has no usable saved date')
    # The phone stores date_added in whole seconds; match the readback check.
    saved = datetime.fromtimestamp(saved_ns // 1_000_000_000, timezone.utc).replace(tzinfo=None)
    item['date_created'] = saved
    item['date_modified'] = saved
    for source_key, target, sort_target, explicit_sort in (
            ('title','title','sort_name','titlesort'), ('artist','artist','sort_artist','artistsort'),
            ('album','album','sort_album','albumsort'), ('albumartist','album_artist','sort_album_artist','albumartistsort'),
            ('composer','composer','sort_composer','composersort')):
        text = value(source_key)
        if text:
            item[target] = text
            item[sort_target] = value(explicit_sort) or text
    for source_key, target in (('genre','genre'), ('copyright','copyright')):
        text = value(source_key)
        if text:
            item[target] = text
    comment = tags.get('comment', ())
    if len(comment) > 1:
        raise ValueError('Multiple comment tags require review')
    if comment:
        text = comment[0]
        if len(text) > 4096 or any(ord(c) < 32 and c not in '\r\n\t' for c in text):
            raise ValueError('Unsupported comment tag')
        if text:
            item['comment'] = text
    compilation = value('compilation')
    if compilation:
        if compilation not in ('0','1'):
            raise ValueError('Unsupported compilation tag')
        if compilation == '1':
            if not value('albumartist'):
                raise ValueError('A compilation requires an album artist')
            item['is_compilation'] = True
    for source_key, target, total_key in (('tracknumber','track_number','track_count'),
                                           ('discnumber','disc_number','disc_count')):
        text = value(source_key)
        if text:
            match = re.fullmatch(r'(\d+)(?:\s*/\s*(\d+))?', text.strip())
            if not match and source_key == 'discnumber':
                continue  # The original nonnumeric tag remains embedded in the audio.
            if match and int(match[1]) == 0 and not match[2]:
                continue  # Zero means unspecified, while the source tag is retained.
            if not match or int(match[1]) == 0:
                raise ValueError('Unsupported ' + source_key + ' tag')
            item[target] = int(match[1])
            if match[2] and int(match[2]) >= int(match[1]):
                item[total_key] = int(match[2])
    date = value('date')
    if date:
        if not re.match(r'^\d{4}(?:$|[- T])', date):
            raise ValueError('Unsupported date tag')
        if int(date[:4]):
            item['year'] = int(date[:4])
    if source.cover:
        item['artwork_cache_id'] = artwork_id
    code, kind = FORMATS[source.codec]
    av = {'audio_format':code, 'bit_rate':source.bit_rate_kbps, 'sample_rate':float(source.sample_rate),
          'channels':source.channels, 'duration':source.coded_samples}
    stats = {'play_count_recent':0, 'play_count_user':0, 'skip_count_recent':0,
             'skip_count_user':0, 'has_been_played':False}
    stamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
    envelope = {'revision':anchor + 1, 'timestamp':stamp, 'operations':[
        {'operation':'update_db_info', 'pid':db, 'db_info':{'primary_container_pid':library}},
        {'operation':'insert_track', 'pid':track, 'item':item, 'item_stats':stats,
         'avformat_info':av, 'location':{'kind':kind}}]}
    return plistlib.dumps(envelope, fmt=plistlib.FMT_BINARY, sort_keys=True)
