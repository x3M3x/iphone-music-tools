"""SQLite and AFC tests using local fixtures; no phone is contacted."""
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mutagen.id3 import APIC, ID3
from pymobiledevice3.exceptions import AfcFileNotFoundError
from phone_music_pull import ItunesDb, MusicLibraryError, Playlist, Track, parse_media_library, plan_export, pull_library_database, run


def make_db(path):
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
        CREATE TABLE item (item_pid, item_artist_pid, album_pid, base_location_id);
        CREATE TABLE item_extra (item_pid, title, location, media_kind);
        CREATE TABLE item_artist (item_artist_pid, item_artist);
        CREATE TABLE album (album_pid, album);
        CREATE TABLE base_location (base_location_id, path);
        CREATE TABLE item_store (item_pid, is_protected, is_subscription);
        CREATE TABLE container (container_pid, name, is_hidden, distinguished_kind);
        CREATE TABLE container_item (container_item_pid, container_pid, item_pid, position);
        INSERT INTO base_location VALUES (1, 'iTunes_Control/Music/F00'), (2, 'Purchases');
        INSERT INTO item_artist VALUES (1, 'Artist');
        INSERT INTO album VALUES (1, 'Album');
        INSERT INTO item VALUES (1, 1, 1, 1), (2, 1, 1, 1), (3, 1, 1, 1),
            (4, 1, 1, 1), (5, 1, 1, 1), (6, 1, 1, 2);
        INSERT INTO item_extra VALUES (1, 'First', 'AAAA.m4a', 1),
            (2, 'Second', 'BBBB.mp3', 1), (3, 'Podcast', 'C.mp3', 4),
            (4, 'Same', 'AA.m4a', 1), (5, 'Same', 'BBBBBBBB.m4a', 1),
            (6, 'Store', 'P.m4a', 1);
        INSERT INTO item_store VALUES (1, 0, 0), (2, 1, 0), (4, 0, 0), (5, 0, 0), (6, 0, 0);
        INSERT INTO container VALUES (1, 'Playlist', 0, 0), (2, 'History', 1, 10000);
        INSERT INTO container_item VALUES (1, 1, 2, 10), (2, 1, 1, 5), (3, 2, 1, 1);
        """)
        db.commit()


class LibraryTests(unittest.IsolatedAsyncioTestCase):
    async def test_recently_imported_does_not_remove_songs(self):
        db = ItunesDb(tracks={1: Track(track_id=1), 2: Track(track_id=2)}, playlists=[
            Playlist(name=" Recently Imported ", items=[(1, 1), (2, 2)]),
            Playlist(name="Favorites", items=[(1, 1)]),
        ])
        plan = plan_export(db)
        self.assertEqual(plan["playlists"], [("Favorites", [(1, 1)])])
        self.assertEqual(plan["rest"], [2])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = Path(self.temp.name) / "sample.db"
        make_db(self.fixture)

    async def test_reads_sqlite_snapshot_and_preserves_playlist_order(self):
        afc = AsyncMock()
        afc.stat.return_value = {"st_size": 100}
        async def pull(source, dest, **kwargs):
            if source.endswith('-wal'):
                raise AfcFileNotFoundError("missing", 8)
            shutil.copyfile(self.fixture, dest)
        afc.pull.side_effect = pull
        library = await pull_library_database(afc)
        self.assertEqual(library.tracks[1].path, "/iTunes_Control/Music/F00/AAAA.m4a")
        self.assertEqual(library.tracks[1].title, "First")
        self.assertTrue(library.tracks[2].protected)
        self.assertEqual(len(library.tracks), 5)
        self.assertEqual(library.tracks[6].path, "/Purchases/P.m4a")
        self.assertEqual(plan_export(library)["playlists"], [("Playlist", [(5, 1), (10, 2)])])

    async def test_missing_database_does_not_mean_library_is_empty(self):
        afc = AsyncMock()
        afc.stat.side_effect = AfcFileNotFoundError("missing", 8)
        afc.get_file_contents.side_effect = AfcFileNotFoundError("missing", 8)
        with self.assertRaisesRegex(MusicLibraryError, "No supported music database"):
            await pull_library_database(afc)

    async def test_invalid_sqlite_database_fails_clearly(self):
        self.fixture.write_bytes(b"not sqlite")
        with self.assertRaisesRegex(MusicLibraryError, "Unsupported or incomplete"):
            parse_media_library(self.fixture)

    async def test_export_copies_embedded_artwork_and_playlist(self):
        audio = Path(self.temp.name) / "audio.m4a"
        tags = ID3()
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=b"test-cover"))
        tags.save(audio)
        afc = AsyncMock()
        afc.stat.return_value = {"st_size": 100}
        async def pull(source, dest, **kwargs):
            if source.endswith("-wal"):
                raise AfcFileNotFoundError("missing", 8)
            shutil.copyfile(self.fixture if source.endswith(".sqlitedb") else audio, dest)
        afc.pull.side_effect = pull
        out = Path(self.temp.name) / "export"
        args = SimpleNamespace(serial=None, info=False, out=str(out), limit=None)
        with patch("phone_music_pull.choose_device", new=AsyncMock(return_value="abc")), \
             patch("phone_music_pull.connect", new=AsyncMock()), \
             patch("phone_music_pull.device_info", new=AsyncMock(return_value={})), \
             patch("phone_music_pull.AfcService", return_value=afc):
            self.assertEqual(await run(args), 0)
        files = list((out / "Playlists" / "Playlist").iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), audio.read_bytes())
        self.assertEqual(ID3(files[0]).getall("APIC")[0].data, b"test-cover")
        self.assertFalse((out / "_Artwork").exists())

    async def _run_export(self, out):
        afc = AsyncMock()
        afc.stat.return_value = {"st_size": 100}
        pulled = []
        audio = Path(self.temp.name) / "audio.m4a"
        if not audio.exists():
            tags = ID3()
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=b"cover"))
            tags.save(audio)
        async def pull(source, dest, **kwargs):
            pulled.append(source)
            if source.endswith("-wal"):
                raise AfcFileNotFoundError("missing", 8)
            shutil.copyfile(self.fixture if source.endswith(".sqlitedb") else audio, dest)
        afc.pull.side_effect = pull
        args = SimpleNamespace(serial=None, info=False, out=str(out), limit=None)
        with patch("phone_music_pull.choose_device", new=AsyncMock(return_value="abc")), \
             patch("phone_music_pull.connect", new=AsyncMock()), \
             patch("phone_music_pull.device_info", new=AsyncMock(return_value={})), \
             patch("phone_music_pull.AfcService", return_value=afc):
            self.assertEqual(await run(args), 0)
        return pulled

    async def test_resume_skips_already_exported_without_downloading(self):
        out = Path(self.temp.name) / "resume"
        done = out / "Playlists" / "Playlist" / "001. Artist - First.m4a"
        done.parent.mkdir(parents=True)
        done.write_bytes(b"x" * 100)
        pulled = await self._run_export(out)
        self.assertIn("/iTunes_Control/iTunes/MediaLibrary.sqlitedb", pulled)
        self.assertNotIn("/iTunes_Control/Music/F00/AAAA.m4a", pulled)
        self.assertEqual(done.read_bytes(), b"x" * 100)

    async def test_same_name_different_track_gets_suffix_and_both_are_kept(self):
        out = Path(self.temp.name) / "clash"
        existing = out / "Playlists" / "Playlist" / "001. Artist - First.m4a"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"other track")
        audio = Path(self.temp.name) / "audio.m4a"
        tags = ID3()
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=b"cover"))
        tags.save(audio)
        await self._run_export(out)
        renamed = out / "Playlists" / "Playlist" / "001. Artist - First (2).m4a"
        self.assertEqual(existing.read_bytes(), b"other track")
        self.assertEqual(renamed.read_bytes(), audio.read_bytes())

    async def test_parallel_export_keeps_both_same_named_tracks(self):
        out = Path(self.temp.name) / "parallel"
        afc = AsyncMock()
        afc.stat.return_value = {"st_size": 100}
        async def pull(source, dest, **kwargs):
            if source.endswith("-wal"):
                raise AfcFileNotFoundError("missing", 8)
            if source.endswith(".sqlitedb"):
                shutil.copyfile(self.fixture, dest)
            else:
                Path(dest).write_bytes(b"content:" + source.encode())
        afc.pull.side_effect = pull
        args = SimpleNamespace(serial=None, info=False, out=str(out), limit=None, jobs=4)
        with patch("phone_music_pull.choose_device", new=AsyncMock(return_value="abc")), \
             patch("phone_music_pull.connect", new=AsyncMock()), \
             patch("phone_music_pull.device_info", new=AsyncMock(return_value={})), \
             patch("phone_music_pull.AfcService", return_value=afc):
            self.assertEqual(await run(args), 0)
        rest = out / "All the rest"
        first = rest / "Artist - Same.m4a"
        second = rest / "Artist - Same (2).m4a"
        self.assertTrue(first.exists() and second.exists())
        self.assertNotEqual(first.read_bytes(), second.read_bytes())


if __name__ == "__main__":
    unittest.main()
