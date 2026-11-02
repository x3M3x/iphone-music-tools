import tempfile
import unittest
from pathlib import Path

from mutagen.id3 import APIC, ID3, TIT2
from complete_metadata import artwork, digest, safely_update


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'song.mp3'
        tags = ID3()
        tags.add(TIT2(encoding=3, text='Original title'))
        tags.add(APIC(encoding=3, mime='image/png', type=3, desc='', data=b'original-image'))
        tags.save(self.path)
        self.meta = {'genre':'Pop', 'album_artist':'Artist', 'composer':'Composer',
                     'year':2022, 'track_number':3, 'track_count':11,
                     'disc_number':1, 'disc_count':2}

    def test_adds_phone_metadata_without_changing_cover_or_title(self):
        before = artwork(self.path)
        old = digest(self.path)
        new, added = safely_update(self.path, old, self.meta)
        self.assertEqual(new, digest(self.path))
        self.assertIsNone(added)
        tags = ID3(self.path)
        self.assertEqual(str(tags['TIT2']), 'Original title')
        self.assertEqual(str(tags['TCON']), 'Pop')
        self.assertEqual(str(tags['TDRC']), '2022')
        self.assertEqual(str(tags['TRCK']), '3/11')
        self.assertEqual(str(tags['TPOS']), '1/2')
        self.assertEqual(artwork(self.path), before)

    def test_changed_file_is_left_untouched(self):
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'changed since rebuild'):
            safely_update(self.path, 'incorrect-hash', self.meta)
        self.assertEqual(self.path.read_bytes(), original)

    def test_source_library_date_is_preserved(self):
        self.meta['date_added'] = 722024698
        _, added = safely_update(self.path, digest(self.path), self.meta)
        self.assertEqual(added, '2023-11-18T18:24:58+00:00')
        self.assertAlmostEqual(self.path.stat().st_mtime, 1700331898, delta=1)
        if hasattr(self.path.stat(), 'st_birthtime'):
            self.assertAlmostEqual(self.path.stat().st_birthtime, 1700331898, delta=1)

    def test_preserves_spaces_in_phone_metadata(self):
        self.meta['genre'] = 'R&B '
        self.meta['album_artist'] = ' Artist'
        safely_update(self.path, digest(self.path), self.meta)
        tags = ID3(self.path)
        self.assertEqual(str(tags['TCON']), 'R&B ')
        self.assertEqual(str(tags['TPE2']), ' Artist')


if __name__ == '__main__':
    unittest.main()
