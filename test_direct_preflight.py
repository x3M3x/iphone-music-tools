import unittest
import threading
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch
from direct_inventory import Inventory

import direct_preflight
from music_importer import ImportErrorSafe


class PreflightTests(unittest.TestCase):
    def test_worker_count_and_progress_follow_completed_checks(self):
        songs=[SimpleNamespace(path=Path(str(i)),file_date_ns=5) for i in range(4)]
        active=0; peak=0; lock=threading.Lock(); progress=[]
        def inspect(path):
            nonlocal active,peak
            with lock: active+=1; peak=max(peak,active)
            time.sleep(.03)
            with lock: active-=1
            return SimpleNamespace(cover=None)
        with patch('direct_preflight.file_date_ns',return_value=5), \
             patch.object(Path,'stat',return_value=SimpleNamespace(st_size=1024)), \
             patch('direct_preflight.inspect',side_effect=inspect), \
             patch('direct_preflight.insert_preview'):
            direct_preflight.scan(songs,Event(),lambda *x:progress.append(x),workers=2)
        self.assertEqual(peak,2)
        self.assertEqual([row[0] for row in progress],[1,2,3,4])

    def test_checks_all_files_before_any_import_and_reports_errors(self):
        songs=[SimpleNamespace(path=Path('a'),file_date_ns=5),
               SimpleNamespace(path=Path('b'),file_date_ns=5)]
        progress=[]
        def inspect(path):
            if path.name=='b':
                raise ValueError('bad file')
            return SimpleNamespace(cover=None)
        from unittest.mock import Mock
        with patch('direct_preflight.file_date_ns',return_value=5), \
             patch.object(Path,'stat',return_value=SimpleNamespace(st_size=1024)), \
             patch('direct_preflight.inspect',side_effect=inspect), \
             patch('direct_preflight.insert_preview'):
            errors=direct_preflight.scan(songs,Event(),lambda *x:progress.append(x))
        self.assertEqual(errors,[{'file':'b','error':'bad file'}])
        self.assertEqual(progress[-1],(2,2,1))

    def test_stopping_preflight_never_produces_a_ready_plan(self):
        stop=Event();stop.set()
        with self.assertRaisesRegex(ImportErrorSafe,'Stopped'):
            direct_preflight.scan([SimpleNamespace(path=Path('a'),file_date_ns=5)],stop,lambda *x:None)

    def test_group_conflict_is_normalized_before_import(self):
        songs=[SimpleNamespace(path=Path('song.mp3'),file_date_ns=5,modified_ns=5)]
        tags={'title':['Song'],'artist':['PHAROZEN'],'album':['Album']}
        inventory=Inventory(frozenset({7}),((7,'Pharozen'),),(),frozenset())
        with patch('direct_preflight.AudioFile',return_value=tags):
            errors=direct_preflight.grouping_scan(songs,inventory)
        self.assertEqual(errors,[])

    def test_previously_verified_file_is_reused_only_when_its_hash_matches(self):
        song=SimpleNamespace(path=Path('song.mp3'),file_date_ns=5,modified_ns=5)
        inventory=Inventory(frozenset(),(),(),frozenset())
        with patch('direct_preflight.content_hash',return_value='correct'), \
             patch('direct_preflight.AudioFile',side_effect=ValueError('should not read tags')):
            self.assertEqual(direct_preflight.grouping_scan([song],inventory,{'song.mp3':'correct'}),[])
            self.assertIn('should not read tags',direct_preflight.grouping_scan(
                [song],inventory,{'song.mp3':'stale'})[0]['error'])


if __name__ == '__main__':
    unittest.main()
