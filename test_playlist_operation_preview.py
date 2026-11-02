import plistlib
import unittest
from datetime import datetime, timezone

from playlist_operation_preview import create_playlist_preview, update_playlist_preview


class PlaylistOperationPreviewTests(unittest.TestCase):
    def setUp(self):
        self.args = dict(database_id=9, library_id=10, anchor=4, playlist_id=11,
                         name='Arabic', ordered_track_ids=[13, 2**64 - 1, 12],
                         occupied_playlist_ids=[10, 99], when=datetime(2026, 1, 1, tzinfo=timezone.utc))

    def test_binary_plist_preserves_playlist_member_order_and_signed_ids(self):
        data = create_playlist_preview(**self.args)
        root = plistlib.loads(data)
        self.assertTrue(data.startswith(b'bplist00'))
        self.assertEqual(root['revision'], 5)
        self.assertEqual(root['operations'][0]['db_info']['primary_container_pid'], 10)
        playlist = root['operations'][1]
        self.assertEqual(playlist['operation'], 'insert_playlist')
        self.assertEqual(playlist['container']['name'], 'Arabic')
        self.assertEqual(playlist['item_to_container'], [13, -1, 12])

    def test_rejects_unverified_duplicate_ids_and_invalid_targets(self):
        for changes in ({'playlist_id': 99}, {'playlist_id': 10},
                        {'ordered_track_ids': [12, 12]}, {'ordered_track_ids': [0]},
                        {'anchor': 2**32 - 1}, {'name': ''},
                        {'when': datetime(2026, 1, 1)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                create_playlist_preview(**(self.args | changes))

    def test_update_keeps_existing_tracks_and_requested_order(self):
        args = self.args.copy()
        args.pop('occupied_playlist_ids')
        args.update(existing_track_ids=[12], created=datetime(2020, 1, 1, tzinfo=timezone.utc))
        op = plistlib.loads(update_playlist_preview(**args))['operations'][1]
        self.assertEqual(op['operation'], 'update_playlist')
        self.assertEqual(op['item_to_container'], [13, -1, 12])
        self.assertEqual(op['container']['date_created'], datetime(2020, 1, 1))
        args['existing_track_ids'] = [999]
        with self.assertRaisesRegex(ValueError, 'remove existing'):
            update_playlist_preview(**args)


if __name__ == '__main__':
    unittest.main()
