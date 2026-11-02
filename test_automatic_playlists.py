import unittest
from direct_phone import Catalog,Playlist,PhoneReadError
from automatic_playlists import check_folder_names,match_playlist


class AutomaticPlaylistTests(unittest.TestCase):
    def test_existing_case_insensitive_and_missing(self):
        catalog=Catalog(1,2,3,(Playlist(5,'Arabic',(),1),),0,(),((5,'Arabic'),))
        self.assertEqual(match_playlist(catalog,'arabic').id,5)
        self.assertIsNone(match_playlist(catalog,'Quran'))

    def test_duplicate_or_noneditable_names_refuse(self):
        catalog=Catalog(1,2,3,(Playlist(5,'Arabic',(),1),),0,(),((5,'Arabic'),(6,'arabic')))
        with self.assertRaisesRegex(PhoneReadError,'More than one'):
            match_playlist(catalog,'Arabic')
        hidden=Catalog(1,2,3,(),0,(),((5,'Arabic'),))
        with self.assertRaisesRegex(PhoneReadError,'cannot be safely edited'):
            match_playlist(hidden,'Arabic')
        with self.assertRaisesRegex(PhoneReadError,'ambiguous'):
            check_folder_names(['Arabic',' arabic '])
