import io
import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB
from mutagen.mp4 import MP4, MP4Cover
from direct_audio import AudioError, inspect, phone_cover


class DirectAudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixtures = Path(__file__).resolve().parent / 'tests-fixtures'
        out = io.BytesIO()
        Image.new('RGB', (8, 8), 'red').save(out, format='JPEG')
        self.cover = out.getvalue()

    def test_mp3_preserves_tags_and_embedded_cover(self):
        path = self.root / 'song.mp3'
        shutil.copyfile(self.fixtures / 'tone-mp3-vbr.mp3', path)
        tags = ID3()
        tags.add(TIT2(encoding=3, text='My title'))
        tags.add(TPE1(encoding=3, text='My artist'))
        tags.add(TALB(encoding=3, text='My album'))
        tags.add(APIC(encoding=3, mime='image/jpeg', type=3, data=self.cover))
        tags.save(path)
        result = inspect(path)
        self.assertEqual((result.title, result.artist, result.album), ('My title', 'My artist', 'My album'))
        self.assertEqual(dict(result.tags)['title'], ('My title',))
        self.assertEqual(result.cover, self.cover)
        self.assertEqual(result.cover_checksum, len(self.cover) + 1)

    def test_aac_preserves_embedded_cover_and_checksum(self):
        path = self.root / 'song.m4a'
        shutil.copyfile(self.fixtures / 'tone-aac.m4a', path)
        mp4 = MP4(path)
        mp4['\xa9nam'] = ['AAC title']
        mp4['covr'] = [MP4Cover(self.cover, imageformat=MP4Cover.FORMAT_JPEG)]
        mp4.save()
        result = inspect(path)
        self.assertEqual(result.title, 'AAC title')
        self.assertEqual(result.cover, self.cover)
        self.assertEqual(result.cover_checksum, len(self.cover) + 17)

    def test_ambiguous_cover_fails_instead_of_picking_one(self):
        path = self.root / 'song.m4a'
        shutil.copyfile(self.fixtures / 'tone-aac.m4a', path)
        mp4 = MP4(path)
        mp4['covr'] = [MP4Cover(self.cover), MP4Cover(self.cover)]
        mp4.save()
        with self.assertRaisesRegex(AudioError, 'Multiple embedded covers'):
            inspect(path)

    def test_png_cover_is_kept_in_file_and_converted_only_for_phone_display(self):
        path = self.root / 'song.m4a'
        shutil.copyfile(self.fixtures / 'tone-aac.m4a', path)
        image = io.BytesIO()
        Image.new('RGBA', (8,8), (255,0,0,0)).save(image,format='PNG')
        original = image.getvalue()
        mp4 = MP4(path)
        mp4['covr'] = [MP4Cover(original, imageformat=MP4Cover.FORMAT_PNG)]
        mp4.save()
        before = path.read_bytes()
        source = inspect(path)
        jpeg = phone_cover(source)
        self.assertEqual(source.cover,original)
        self.assertEqual(path.read_bytes(),before)
        with Image.open(io.BytesIO(jpeg)) as converted:
            self.assertEqual(converted.format,'JPEG')
            self.assertEqual(converted.convert('RGB').getpixel((0,0)),(255,255,255))


if __name__ == '__main__':
    unittest.main()
