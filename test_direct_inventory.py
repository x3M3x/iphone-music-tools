import unittest
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from direct_audio import SourceAudio
from direct_inventory import Album, Inventory, fresh_id, track_ids, spelling_overrides, apply_spelling, parse_inventory, active_albums
from direct_phone import PhoneReadError


class IdentityAllocationTests(unittest.TestCase):
    def setUp(self):
        self.source = SourceAudio(Path('a.mp3'),'mpeg1-layer3',44100,2,128,2.0,88200,
                                  'Title','Artist','Album',None,None,0,())

    def test_reuses_exact_album_and_artist_without_reusing_track_id(self):
        inventory = Inventory(frozenset({2,3,4}), ((3,'Artist'),),
                              (Album(4,'Album',3,'Artist',False),), frozenset({100}))
        with patch('direct_inventory.secrets.randbits', return_value=1001):
            self.assertEqual(track_ids(inventory,self.source), (1001,4,3))

    def test_refuses_ambiguous_or_different_spelling(self):
        inventory = Inventory(frozenset({3}), ((3,'artist'),), (), frozenset())
        with self.assertRaisesRegex(PhoneReadError, 'spelling'):
            track_ids(inventory,self.source)

    def test_does_not_allocate_an_existing_or_small_id(self):
        with patch('direct_inventory.secrets.randbits', side_effect=[1,1001,1002]):
            self.assertEqual(fresh_id({1001}),1002)

    def test_blank_tags_are_not_invented_and_blank_groups_are_not_reused(self):
        source = replace(self.source, artist='', album='')
        inventory = Inventory(frozenset({3,4}), ((3,' '),),
                              (Album(4,' ',3,' ',False),), frozenset())
        with patch('direct_inventory.secrets.randbits', side_effect=[1001,1002,1003]):
            self.assertEqual(track_ids(inventory,source), (1001,1003,1002))

    def test_case_variant_uses_existing_artist_spelling_without_editing_source(self):
        inventory=Inventory(frozenset({3}),((3,'Artist'),),(),frozenset(),
                            item_artists=(('Artist','Artist sort'),),artist_sorts=((3,'Artist sort'),))
        original=replace(self.source,artist='ARTIST',tags=(('artist',('ARTIST',)),))
        overrides=spelling_overrides(inventory,original)
        normalized=apply_spelling(original,overrides)
        self.assertEqual(normalized.artist,'Artist')
        self.assertEqual(dict(normalized.tags)['artistsort'],('Artist sort',))
        self.assertEqual(original.artist,'ARTIST')
        with patch('direct_inventory.secrets.randbits',side_effect=[1001,1002]):
            self.assertEqual(track_ids(inventory,normalized),(1001,1002,3))

    def test_conflicting_item_and_album_artist_groups_fail_closed(self):
        inventory=Inventory(frozenset({3}),((3,'ARTIST'),),(),frozenset(),
                            item_artists=(('Artist','Artist'),),artist_sorts=((3,'ARTIST'),))
        source=replace(self.source,artist='artist')
        with self.assertRaisesRegex(PhoneReadError,'spellings disagree'):
            spelling_overrides(inventory,source)

    def test_blank_album_and_blank_artist_never_reuse_group_ids(self):
        source=replace(self.source,artist='',album='')
        inventory=Inventory(frozenset({3,4}),((3,' '),),(Album(4,' ',3,' ',False),),frozenset())
        with patch('direct_inventory.secrets.randbits',side_effect=[1001,1002,1003]):
            self.assertEqual(track_ids(inventory,source),(1001,1003,1002))

    def test_duplicate_album_identity_stops_before_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'catalog.sqlite'
            with closing(sqlite3.connect(path)) as db:
                db.executescript('''
                    CREATE TABLE item_store(sync_id INTEGER);
                    CREATE TABLE item(album_pid INTEGER);
                    INSERT INTO item VALUES(1);
                    CREATE TABLE album(sync_id INTEGER,album_pid INTEGER,album TEXT,album_artist_pid INTEGER,all_compilations INTEGER);
                    INSERT INTO album VALUES(123,1,'Named',1,0),(123,2,'Named',1,0);
                    CREATE TABLE album_artist(sync_id INTEGER,album_artist_pid INTEGER,album_artist TEXT,sort_album_artist TEXT);
                    INSERT INTO album_artist VALUES(456,1,'Artist','Artist');
                    CREATE TABLE item_artist(item_artist TEXT,sort_item_artist TEXT);
                    CREATE TABLE container(sync_id INTEGER);
                    CREATE TABLE db_info(db_pid INTEGER,primary_container_pid INTEGER);
                ''')
            with self.assertRaisesRegex(PhoneReadError,'duplicate album identities'):
                parse_inventory(path.read_bytes(),None)

    def test_orphan_duplicates_do_not_hide_live_collisions(self):
        empty=(123,'',0,'',0,0)
        active=(456,'album',8,'artist',0,1)
        self.assertEqual(active_albums([empty,empty,active]),(Album(456,'album',8,'artist',False),))
        self.assertEqual(active_albums([empty,(123,'',0,'',0,1),active]),(Album(456,'album',8,'artist',False),))
        with self.assertRaisesRegex(PhoneReadError,'duplicate album identities'):
            active_albums([(123,'',0,'',0,1),(123,'',0,'',0,1)])
        with self.assertRaisesRegex(PhoneReadError,'duplicate album identities'):
            active_albums([(123,'named',0,'',0,0),(123,'named',0,'',0,1)])


if __name__ == '__main__':
    unittest.main()
