"""Offline tests using a fake AFC device and temporary files."""
import asyncio
import io
import sqlite3
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mutagen.id3 import APIC, ID3, TIT2
from mutagen.mp4 import MP4, MP4FreeForm
from PIL import Image
from pymobiledevice3.exceptions import AfcFileNotFoundError

import rebuild_music as rebuild
from test_library_detection import make_db


class RebuildTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'library.sqlite'
        make_db(self.db)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript("""
                ALTER TABLE item ADD COLUMN disc_number;
                ALTER TABLE item ADD COLUMN track_number;
                ALTER TABLE item ADD COLUMN date_added;
                ALTER TABLE item ADD COLUMN genre_id;
                ALTER TABLE item ADD COLUMN album_artist_pid;
                ALTER TABLE item ADD COLUMN composer_pid;
                ALTER TABLE item_extra ADD COLUMN track_count;
                ALTER TABLE item_extra ADD COLUMN disc_count;
                ALTER TABLE item_extra ADD COLUMN year;
                CREATE TABLE genre (genre_id, genre);
                CREATE TABLE album_artist (album_artist_pid, album_artist);
                CREATE TABLE composer (composer_pid, composer);
                CREATE TABLE artwork_token (artwork_token, artwork_source_type, entity_pid, entity_type);
                CREATE TABLE artwork (artwork_token, artwork_source_type, relative_path);
                INSERT INTO artwork_token VALUES ('1', 300, 1, 0);
                INSERT INTO artwork VALUES ('1', 300, 'aa/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
                UPDATE item_extra SET location='AAAA.mp3' WHERE item_pid=1;
                UPDATE item SET date_added=722024698 WHERE item_pid=1;
            """)
            conn.commit()
        self.audio = self.root / 'source.mp3'
        tags = ID3()
        tags.add(TIT2(encoding=3, text='old title'))
        tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='', data=b'old-cover'))
        tags.save(self.audio)
        buffer = io.BytesIO()
        Image.new('RGB', (3, 3), color='red').save(buffer, format='PNG')
        self.cover = buffer.getvalue()

    async def test_rebuild_preserves_originals_and_protected_playlist(self):
        original = self.root / 'existing'
        protected = original / 'Playlists' / 'Favorites' / 'external.mp3'
        protected.parent.mkdir(parents=True)
        protected.write_bytes(b'unchanged external file')
        out = self.root / 'corrected'
        afc = AsyncMock()
        lockdown = SimpleNamespace(paired=True, close=AsyncMock())
        async def stat(path):
            return {'st_size': self.audio.stat().st_size}
        async def pull(source, dest, **kwargs):
            if source.endswith('-wal'):
                raise AfcFileNotFoundError('missing', 8)
            Path(dest).write_bytes((self.db if source.endswith('.sqlitedb') else self.audio).read_bytes())
        afc.stat.side_effect = stat
        afc.pull.side_effect = pull
        afc.get_file_contents.return_value = self.cover
        args = SimpleNamespace(out=str(out), original=str(original), serial='fixture', jobs=2, limit=1, protected_playlist='Favorites')
        with patch.object(rebuild, 'create_using_usbmux', new=AsyncMock(return_value=lockdown)), \
             patch.object(rebuild, 'AfcService', return_value=afc):
            self.assertEqual(await rebuild.rebuild(args), 0)
        self.assertEqual(protected.read_bytes(), b'unchanged external file')
        self.assertEqual((out / 'Playlists' / 'Favorites' / 'external.mp3').read_bytes(), protected.read_bytes())
        song = out / 'Playlists' / 'Playlist' / 'First.mp3'
        tags = ID3(song)
        self.assertEqual(str(tags['TIT2']), 'First')
        self.assertEqual(str(tags['TPE1']), 'Artist')
        self.assertEqual(tags.getall('APIC')[0].data, self.cover)
        self.assertAlmostEqual(song.stat().st_mtime, 1700331898, delta=1)
        self.assertEqual(ID3(self.audio).getall('APIC')[0].data, b'old-cover')
        self.assertIn('Playlists/Playlist/First.mp3', (out / 'Playlist.m3u8').read_text(encoding='utf-8-sig'))
        self.assertEqual(list((out / 'Playlists' / 'Playlist').glob('001.*')), [])

    def test_existing_destination_is_not_overwritten(self):
        target = self.root / 'occupied.mp3'
        target.write_bytes(b'user data')
        with self.assertRaises(FileExistsError):
            rebuild.publish(self.audio, target)
        self.assertEqual(target.read_bytes(), b'user data')

    def test_preserved_playlist_must_exist_and_stay_within_export(self):
        for name in ('missing','..','../outside'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                rebuild.copy_protected_playlist(self.root, self.root / 'output', name)

    def test_invalid_art_is_rejected(self):
        self.assertIsNone(rebuild.picture_type(b'\xff\xd8\xffbroken'))
        self.assertEqual(rebuild.picture_type(self.cover), 'image/png')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg required')
    def test_remux_preserves_gapless_metadata_and_audio(self):
        source = self.root / 'valid.m4a'
        target = self.root / 'remuxed.m4a'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=0.2', '-c:a', 'aac', str(source)],
                       check=True, capture_output=True)
        tags = MP4(source)
        if tags.tags is None:
            tags.add_tags()
        value = ' 00000000 00000840 00000000 0000000000B843C0'
        tags['----:com.apple.iTunes:iTunSMPB'] = [MP4FreeForm(value.encode())]
        tags.save()
        meta = {'title': 'Correct title', 'artist': 'Artist', 'album': 'Album'}
        rebuild.fallback_remux(source, target, meta, None, self.root)
        self.assertEqual(rebuild.probe_tags(target).get('iTunSMPB'), value)
        def audio_hash(p):
            return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(p), '-map', '0:a:0',
                                            '-c', 'copy', '-f', 'hash', '-hash', 'sha256', '-'])
        self.assertEqual(audio_hash(source), audio_hash(target))


if __name__ == '__main__':
    unittest.main()
