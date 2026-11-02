import plistlib
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import patch

from album_repair import RepairError, TABLES, operation, plan, verify


def fixture():
    tables={name:((name,1),) for name in TABLES+('db_info',)}
    tables['album']=((10,77,'',2,0),(11,77,'',2,3),(12,88,'other',4,5))
    return {'tables':tables,'album_columns':['album_pid','sync_id','album','album_artist_pid','representative_item_pid'],
            'albums':[(10,77,'',2,0,0),(11,77,'',2,3,1),(12,88,'other',4,5,1)],
            'tracks':[(99,3,11,'Test song')],'occupied':{1,2,77,88,99}}


class RepairTests(unittest.TestCase):
    def test_exact_one_row_rekey_preserves_other_records(self):
        before=fixture()
        repair=plan(before,99,'Test song',1001)
        msg=plistlib.loads(operation(repair,1,2,3,datetime(2026,1,1,tzinfo=timezone.utc)))
        self.assertEqual(msg['operations'][1],{'operation':'update_track','pid':99,'item':{'album_pid':1001}})
        after=deepcopy(before)
        after['albums'][1]=(11,1001,'',2,3,1)
        after['tables']['album']=((10,77,'',2,0),(11,1001,'',2,3),(12,88,'other',4,5))
        self.assertTrue(verify(repair,after))
        after['tables']['container']=(('changed',1),)
        with self.assertRaisesRegex(RepairError,'container changed'):
            verify(repair,after)

    def test_duplicate_filled_rows_are_rekeyed_in_one_session(self):
        import plistlib
        from album_repair import plan_duplicates, operation_duplicates, verify_duplicates
        from copy import deepcopy
        before=fixture()
        before['albums']=[(10,77,'',2,7,1),(11,77,'',2,3,1),(12,88,'other',4,5,1)]
        before['tracks']=[(99,3,11,'Test song'),(98,7,10,'Other song')]
        before['occupied']={1,2,77,88,98,99}
        before['tables']['album']=((10,77,'',2,7),(11,77,'',2,3),(12,88,'other',4,5))
        with patch('album_repair.secrets.randbits',return_value=1001):
            rekeys,view=plan_duplicates(before)
        self.assertEqual([(r['track_id'],r['album_row'],r['old_id'],r['new_id']) for r in rekeys],
                         [(99,11,77,1001)])
        message=plistlib.loads(operation_duplicates(rekeys,1,2,3))
        self.assertEqual(message['operations'][1],{'operation':'update_track','pid':99,'item':{'album_pid':1001}})
        after=deepcopy(before)
        after['albums'][1]=(11,1001,'',2,3,1)
        after['tables']['album']=((10,77,'',2,7),(11,1001,'',2,3),(12,88,'other',4,5))
        self.assertTrue(verify_duplicates(rekeys,before,after))
        after['tables']['item']=(('item','changed'),)
        with self.assertRaisesRegex(RepairError,'item changed'):
            verify_duplicates(rekeys,before,after)

    def test_empty_plus_filled_duplicates_keep_the_empty_row(self):
        from album_repair import plan_duplicates, verify_duplicates
        before=fixture()
        before['albums']=[(10,77,'',2,0,0),(11,77,'',2,3,1),(12,88,'other',4,5,1)]
        before['tracks']=[(99,3,11,'Test song')]
        before['tables']['album']=((10,77,'',2,0),(11,77,'',2,3),(12,88,'other',4,5))
        with patch('album_repair.secrets.randbits',return_value=1001):
            rekeys,_=plan_duplicates(before)
        self.assertEqual([(r['track_id'],r['album_row'],r['old_id'],r['new_id']) for r in rekeys],
                         [(99,11,77,1001)])
        after=deepcopy(before)
        after['albums'][1]=(11,1001,'',2,3,1)
        after['tables']['album']=((10,77,'',2,0),(11,1001,'',2,3),(12,88,'other',4,5))
        self.assertTrue(verify_duplicates(rekeys,before,after))

    def test_rejects_unrelated_members_or_colliding_new_id(self):
        before=fixture()
        for identifier in (77,99,0):
            with self.subTest(identifier=identifier), self.assertRaises(RepairError):
                plan(before,99,'Test song',identifier)
        before['albums'][1]=(11,77,'',2,3,2)
        with self.assertRaisesRegex(RepairError,'single-track'):
            plan(before,99,'Test song',1001)

    def test_split_repair_preserves_all_other_data(self):
        from album_repair import verify_split_duplicates
        before=fixture()
        before['item_columns']=['item_pid','album_pid','title']
        before['tables']['item']=((3,11,'song'),)
        after=deepcopy(before)
        after['tables']['item']=((3,13,'song'),)
        after['tables']['album']=((10,77,'',2,0),(11,77,'',2,0),(12,88,'other',4,5),(13,1001,'',2,3))
        after['tracks']=[(99,3,13,'Test song')]
        rekeys=[{'track_id':99,'album_row':11,'old_id':77,'new_id':1001}]
        self.assertTrue(verify_split_duplicates(rekeys,before,after))
        after['tables']['item']=((3,13,'different'),)
        with self.assertRaises(RepairError):
            verify_split_duplicates(rekeys,before,after)


if __name__=='__main__':
    unittest.main()
