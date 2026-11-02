import plistlib
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

from direct_audio import SourceAudio
from track_operation_preview import insert_preview


class TrackPreviewTests(unittest.TestCase):
    def setUp(self):
        self.source = SourceAudio(Path('test.mp3'), 'mpeg1-layer3', 44100, 2, 128, 2.0, 88200,
            'Original', 'Artist', 'Album', b'cover', 'JPEG', 6,
            (('title',('Original',)), ('artist',('Artist',)), ('album',('Album',)),
             ('tracknumber',('2/9',)), ('date',('2020',))))
        self.params = dict(database_id=1, library_id=2, anchor=5, track_id=3,
                           album_id=4, artist_id=5, artwork_id=6,
                           timestamp=datetime(2026,1,1,tzinfo=timezone.utc))
        date_patch = patch('track_operation_preview.file_date_ns', return_value=1681381588000000000)
        date_patch.start()
        self.addCleanup(date_patch.stop)

    def test_preserves_metadata_and_artwork_reference(self):
        data = plistlib.loads(insert_preview(self.source, **self.params))
        self.assertEqual(data['revision'], 6)
        op = data['operations'][1]
        self.assertEqual(op['operation'], 'insert_track')
        self.assertEqual(op['item']['title'], 'Original')
        self.assertEqual(op['item']['track_number'], 2)
        self.assertEqual(op['item']['track_count'], 9)
        self.assertEqual(op['item']['artwork_checksum'], 6)
        self.assertEqual(op['item']['artwork_cache_id'], 6)
        self.assertEqual(op['item']['date_created'], datetime(2023,4,13,10,26,28))
        self.assertEqual(op['avformat_info']['duration'], 88200)

    def test_rejects_unreserved_cover_and_colliding_ids(self):
        for changed in ({'artwork_id':None}, {'track_id':4}, {'anchor':2**32-1}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                insert_preview(self.source, **(self.params | changed))

    def test_saved_date_is_floored_to_phone_second(self):
        with patch('track_operation_preview.file_date_ns',return_value=1681381588999999999):
            op=plistlib.loads(insert_preview(self.source,**self.params))['operations'][1]
        self.assertEqual(op['item']['date_created'],datetime(2023,4,13,10,26,28))


if __name__ == '__main__':
    unittest.main()
