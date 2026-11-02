"""Inspect the user's original local tags and embedded cover; no writes."""
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import json
from fractions import Fraction
import subprocess

from mutagen import File as audio_file
from PIL import Image

NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class AudioError(Exception):
    pass


@dataclass(frozen=True)
class SourceAudio:
    path: Path
    codec: str
    sample_rate: int
    channels: int
    bit_rate_kbps: int
    duration: float
    coded_samples: int
    title: str
    artist: str
    album: str
    cover: bytes | None
    cover_format: str | None
    cover_checksum: int
    tags: tuple[tuple[str, tuple[str, ...]], ...]


def inspect(path):
    path = Path(path).resolve(strict=True)
    try:
        raw = audio_file(path)
        easy = audio_file(path, easy=True)
        if raw is None or easy is None or raw.info is None:
            raise AudioError('File is not a recognized audio file.')
        result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
            '-show_entries', 'stream=codec_name,profile,sample_rate,channels,bit_rate,duration,duration_ts,time_base',
            '-of', 'json', str(path)], capture_output=True, check=True, timeout=30,
            creationflags=NO_WINDOW)
        streams = json.loads(result.stdout)['streams']
        if len(streams) != 1:
            raise AudioError('Exactly one audio stream is required.')
        stream = streams[0]
        rate, channels = int(stream['sample_rate']), int(stream['channels'])
        bitrate = int(stream['bit_rate'])
        duration = float(stream['duration'])
        sample_count = Fraction(int(stream['duration_ts'])) * Fraction(stream['time_base']) * rate
        if rate <= 0 or channels not in (1, 2) or bitrate <= 0 or not 0 < duration < 86400:
            raise AudioError('The audio format or duration is not supported.')
        if sample_count <= 0:
            raise AudioError('The audio duration is invalid.')
        # Some MP3 containers expose an estimated duration in a time base that
        # does not divide evenly into samples; round to the nearest sample.
        sample_count = round(sample_count)
        codec = stream['codec_name']
        if codec == 'aac' and path.suffix.lower() == '.m4a' and stream.get('profile') in ('LC','HE-AAC','HE-AACv2'):
            format_name = {'LC':'aac-lc','HE-AAC':'he-aac','HE-AACv2':'he-aac-v2'}[stream['profile']]
        elif codec == 'mp3' and path.suffix.lower() == '.mp3':
            version = getattr(raw.info, 'version', None)
            if version not in (1, 2, 2.5):
                raise AudioError('Unsupported MP3 version.')
            format_name = 'mpeg' + str(version) + '-layer3'
        else:
            raise AudioError('Unsupported audio codec; this file must be reviewed.')
        values = []
        for field in ('title', 'artist', 'album'):
            values.append(str((easy.get(field) or [''])[0]))
        preserved = tuple(sorted((str(key), tuple(str(value) for value in values))
                                 for key, values in easy.items()))
        tags = raw.tags
        if path.suffix.lower() == '.m4a':
            pictures = list(tags.get('covr', [])) if tags else []
            overhead = 17
        else:
            pictures = list(tags.getall('APIC')) if tags and hasattr(tags, 'getall') else []
            if len(pictures) > 1:
                front = [p for p in pictures if int(p.type) == 3]
                if len(front) != 1:
                    raise AudioError('Multiple embedded covers need review before import.')
                pictures = front
            pictures = [p.data for p in pictures]
            overhead = 1
        if len(pictures) > 1:
            raise AudioError('Multiple embedded covers need review before import.')
        cover = bytes(pictures[0]) if pictures else None
        cover_format = None
        if cover:
            if len(cover) > 32 * 1024 * 1024:
                raise AudioError('Embedded cover is too large.')
            with Image.open(BytesIO(cover)) as picture:
                cover_format = picture.format
                picture.verify()
            if cover_format not in ('JPEG', 'PNG'):
                raise AudioError('Embedded cover is not JPEG or PNG.')
        return SourceAudio(path, format_name, rate, channels, round(bitrate / 1000), duration, sample_count,
                           *values, cover, cover_format, len(cover) + overhead if cover else 0, preserved)
    except AudioError:
        raise
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as exc:
        raise AudioError(f'Cannot inspect the audio file: {exc}') from exc


def phone_cover(source):
    """Prepare the phone's display cover without changing the original file."""
    if source.cover is None:
        return None
    if source.cover_format == 'JPEG':
        return source.cover
    if source.cover_format != 'PNG':
        raise AudioError('Unsupported embedded cover format.')
    try:
        with Image.open(BytesIO(source.cover)) as image:
            rgba = image.convert('RGBA')
            background = Image.new('RGBA', rgba.size, (255,255,255,255))
            background.alpha_composite(rgba)
            output = BytesIO()
            background.convert('RGB').save(output, format='JPEG', quality=95, subsampling=0)
            result = output.getvalue()
            if len(result) > 32 * 1024 * 1024:
                raise AudioError('Converted display cover is too large.')
            return result
    except (OSError, ValueError) as exc:
        raise AudioError('Could not prepare the embedded cover.') from exc
