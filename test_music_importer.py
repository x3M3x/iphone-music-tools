import json
import hashlib
import types
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import music_importer as imp


class Items:
    def __init__(self, values=None): self.values = list(values or [])
    @property
    def Count(self): return len(self.values)
    def Item(self, index): return self.values[index-1]
    def ItemByPlayOrder(self, index): return self.values[index-1]
    def ItemByPersistentID(self, hi, lo):
        return next((x for x in self.values if (x.hi, x.lo) == (hi, lo)), None)


class Track:
    def __init__(self, hi, name, artist, source_id):
        self.hi, self.lo = hi, 99
        self.Name, self.Artist, self.Album, self.sourceID = name, artist, 'Album', source_id
        self.DateAdded=datetime(2020,1,1,12,0,0)


class Playlist:
    def __init__(self, source, name, hi, kind=2):
        self.Source, self.Name, self.hi, self.lo = source, name, hi, 99
        self.Kind, self.Smart, self.SpecialKind = kind, False, 0
        self.Tracks = Items()
        self.add_calls = 0
        self.bad_operation = False
    def AddFile(self, path):
        self.add_calls += 1
        track = Track(100 + self.add_calls, Path(path).stem, 'Artist', self.Source.sourceID)
        self.Tracks.values.append(track)
        if self is not self.Source.library:
            self.Source.library.Tracks.values.append(track)
        return type('Operation', (), {'InProgress': False, 'Tracks': Items([] if self.bad_operation else [track])})()
    def AddTrack(self, track):
        self.Tracks.values.append(track)
        return track


class Source:
    def __init__(self):
        self.Name, self.Kind, self.sourceID, self.hi, self.lo = 'iPhone', 2, 42, 10, 20
        self.library = Playlist(self, 'Music', 11, kind=1)
        self.mix = Playlist(self, 'Arabic', 12)
        self.other = Playlist(self, 'Nasheed', 13)
        self.Playlists = Items([self.library, self.mix, self.other])


class App:
    def __init__(self):
        self.source = Source(); self.Sources = Items([self.source])
    def ITObjectPersistentIDHigh(self, obj): return obj.hi
    def ITObjectPersistentIDLow(self, obj): return obj.lo
    def CreatePlaylistInSource(self, name, source):
        p = Playlist(source, name, 50)
        source.Playlists.values.append(p)
        return p


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = App(); self.device = '10:20'
        self.journal = self.root/'history.sqlite'
        self.path = self.root/'Song.mp3'; self.path.write_bytes(b'example audio')
        s = self.path.stat()
        self.song = imp.Song(self.path, 'Song', 'Artist', 'Album', s.st_size, s.st_mtime_ns, None)
        self.events = []
        self.progress = lambda *args: self.events.append(args)
        self.stop = threading.Event()
        self.cast = patch.object(imp, 'cast_playlist', side_effect=lambda obj, kind: obj)
        self.cast.start(); self.addCleanup(self.cast.stop)

    def call(self, playlist, songs=None, skip_possible=False):
        return imp.perform_import(self.app,self.device,playlist,None,songs or [self.song],
                                  self.journal,self.progress,self.stop,skip_possible)

    def test_append_resume_and_reuse_on_second_playlist(self):
        result = self.call('12:99')
        self.assertEqual(result['added'], 1)
        self.assertEqual(self.app.source.mix.Tracks.Count, 1)
        self.assertEqual(self.app.source.other.Tracks.Count, 0)
        self.assertEqual(self.call('12:99')['already'], 1)
        self.assertEqual(self.app.source.library.add_calls, 1)
        self.assertEqual(self.call('13:99')['reused'], 1)
        self.assertEqual(self.app.source.library.Tracks.Count, 1)
        self.assertEqual(self.app.source.other.Tracks.Count, 1)

    def test_identical_file_in_other_folder_reuses_device_track(self):
        self.call('12:99')
        other=self.root/'another.mp3';other.write_bytes(self.path.read_bytes());s=other.stat()
        second=imp.Song(other,'Song','Artist','Album',s.st_size,s.st_mtime_ns,None)
        self.assertEqual(self.call('13:99',[second])['reused'],1)
        self.assertEqual(self.app.source.library.Tracks.Count,1)
        self.assertEqual(self.app.source.other.Tracks.Count,1)

    def test_copies_oldest_first_and_links_playlist_newest_first(self):
        songs=[]
        for index,label in enumerate(('old','middle','new')):
            path=self.root/(label+'.mp3');path.write_bytes(label.encode());stat=path.stat()
            songs.append(imp.Song(path,label,'Artist','Album',stat.st_size,stat.st_mtime_ns,None,index+1))
        result=self.call('12:99',songs)
        self.assertEqual([t.Name for t in self.app.source.library.Tracks.values],['old','middle','new'])
        self.assertEqual([t.Name for t in self.app.source.mix.Tracks.values],['new','middle','old'])
        self.assertIn('Newest-first relative order confirmed',result['order'])

    def test_batch_copies_whole_library_then_links_playlists_without_recopying(self):
        songs=[]
        for index,label in enumerate(('old','middle','new')):
            path=self.root/(label+'.mp3');path.write_bytes(label.encode());s=path.stat()
            songs.append(imp.Song(path,label,'Artist','Album',s.st_size,s.st_mtime_ns,None,index+1))
        bindings=[imp.PlaylistBinding('Arabic','12:99',None,[songs[0],songs[2]]),
                  imp.PlaylistBinding('Nasheed','13:99',None,[songs[1],songs[2]])]
        result=imp.perform_batch(self.app,self.device,'11:99',list(reversed(songs)),bindings,
                                 self.journal,self.progress,self.stop,skip_possible=False)
        self.assertFalse(result['stopped'])
        self.assertEqual([t.Name for t in self.app.source.library.Tracks.values],['old','middle','new'])
        self.assertEqual([t.Name for t in self.app.source.mix.Tracks.values],['new','old'])
        self.assertEqual([t.Name for t in self.app.source.other.Tracks.values],['new','middle'])
        self.assertEqual(self.app.source.library.add_calls,3)
        self.assertTrue(all(t.DateAdded==datetime(2020,1,1,12,0,0)
                            for t in self.app.source.library.Tracks.values))

    def test_batch_rejects_two_sources_mapped_to_same_playlist_before_copy(self):
        bindings=[imp.PlaylistBinding('A','12:99',None,[self.song]),
                  imp.PlaylistBinding('B','12:99',None,[self.song])]
        with self.assertRaisesRegex(imp.ImportErrorSafe,'different iPhone playlist'):
            imp.perform_batch(self.app,self.device,'11:99',[self.song],bindings,
                              self.journal,self.progress,self.stop,skip_possible=False)
        self.assertEqual(self.app.source.library.add_calls,0)

    def test_all_music_copies_oldest_first_without_creating_playlist(self):
        songs=[]
        for index,label in enumerate(('old','new')):
            path=self.root/(label+'.mp3');path.write_bytes(label.encode());s=path.stat()
            songs.append(imp.Song(path,label,'Artist','Album',s.st_size,s.st_mtime_ns,None,index))
        result=self.call('11:99',songs)
        self.assertEqual([t.Name for t in self.app.source.library.Tracks.values],['old','new'])
        self.assertEqual(self.app.source.mix.Tracks.Count,0)
        self.assertIn('oldest first',result['order'])

    def test_ambiguous_library_fails_before_any_write(self):
        extra=Playlist(self.app.source,'Other library',16,kind=1)
        self.app.source.Playlists.values.append(extra)
        with self.assertRaisesRegex(imp.ImportErrorSafe,'exactly one writable music library'):
            self.call('12:99')
        self.assertEqual(self.app.source.library.add_calls,0)
        self.assertEqual(self.app.source.mix.Tracks.Count,0)

    def test_nonmatching_playlist_order_stops_further_links(self):
        prior=Track(7,'Existing','Artist',42)
        self.app.source.mix.Tracks.values.append(prior)
        original=self.app.source.mix.AddTrack
        def add_to_front(track):
            result=original(track)
            self.app.source.mix.Tracks.values.remove(track)
            self.app.source.mix.Tracks.values.insert(0,track)
            return result
        self.app.source.mix.AddTrack=add_to_front
        with self.assertRaisesRegex(imp.ImportErrorSafe,'may have completed'):
            self.call('12:99')
        self.assertEqual(self.app.source.mix.Tracks.Count,2)
        with self.assertRaisesRegex(imp.ImportErrorSafe,'playlist result.*uncertain'):
            self.call('12:99')
        self.assertEqual(self.app.source.mix.Tracks.Count,2)

    def test_identical_files_within_one_batch_are_not_copied_twice(self):
        duplicate=self.root/'same.mp3';duplicate.write_bytes(self.path.read_bytes());s=duplicate.stat()
        second=imp.Song(duplicate,'Song','Artist','Album',s.st_size,s.st_mtime_ns,None)
        result=self.call('12:99',[self.song,second])
        self.assertEqual(result['added'],1)
        self.assertEqual(self.app.source.library.Tracks.Count,1)
        self.assertEqual(self.app.source.mix.Tracks.Count,1)

    def test_linking_verifies_all_music_date_is_unchanged(self):
        result=self.call('12:99')
        self.assertEqual(result['reused'],1)
        self.assertEqual(self.app.source.library.Tracks.Item(1).DateAdded,datetime(2020,1,1,12,0,0))

    def test_linking_stops_if_all_music_date_changes(self):
        original=self.app.source.mix.AddTrack
        def bad_link(track):
            linked=original(track)
            track.DateAdded=datetime(2026,1,1)
            return linked
        self.app.source.mix.AddTrack=bad_link
        with self.assertRaisesRegex(imp.ImportErrorSafe,'date changed'):
            self.call('12:99')
        self.assertEqual(self.app.source.library.add_calls,1)
        self.assertEqual(self.app.source.mix.Tracks.Count,1)
        with self.assertRaisesRegex(imp.ImportErrorSafe,'playlist result.*uncertain'):
            self.call('12:99')
        self.assertEqual(self.app.source.mix.Tracks.Count,1)

    def test_possible_duplicate_created_during_same_run_is_reported(self):
        another=self.root/'other.mp3';another.write_bytes(b'different audio');s=another.stat()
        second=imp.Song(another,'Song','Artist','Album',s.st_size,s.st_mtime_ns,None)
        result=self.call('12:99',[self.song,second],skip_possible=True)
        self.assertEqual(result['added'],1)
        self.assertEqual(result['possible'],1)
        self.assertEqual(self.app.source.library.add_calls,1)

    def test_existing_verified_asset_in_target_is_not_added_again(self):
        self.call('12:99')
        track=self.app.source.library.Tracks.Item(1)
        self.app.source.other.Tracks.values.append(track)
        result=self.call('13:99')
        self.assertEqual(result['already'],1)
        self.assertEqual(self.app.source.other.Tracks.Count,1)

    def test_ambiguous_existing_metadata_does_not_write(self):
        self.app.source.library.Tracks.values.append(Track(9, 'Song', 'Artist', 42))
        result = self.call('12:99', skip_possible=True)
        self.assertEqual(result['possible'], 1)
        self.assertEqual(self.app.source.mix.add_calls, 0)

    def test_uncertain_operation_is_not_retried(self):
        self.app.source.library.bad_operation = True
        with self.assertRaisesRegex(imp.ImportErrorSafe, 'last operation may have completed'):
            self.call('12:99')
        self.assertEqual(self.app.source.library.add_calls, 1)
        with self.assertRaisesRegex(imp.ImportErrorSafe, 'previous result.*uncertain'):
            self.call('12:99')
        self.assertEqual(self.app.source.library.add_calls, 1)

    def test_wrong_metadata_stops_without_retrying(self):
        original = self.app.source.library.AddFile
        def wrong_title(path):
            op=original(path)
            op.Tracks.Item(1).Name='A different song'
            return op
        self.app.source.library.AddFile=wrong_title
        with self.assertRaisesRegex(imp.ImportErrorSafe,'different name metadata'):
            self.call('12:99')
        self.assertEqual(self.app.source.library.add_calls,1)
        with self.assertRaisesRegex(imp.ImportErrorSafe,'previous result.*uncertain'):
            self.call('12:99')
        self.assertEqual(self.app.source.library.add_calls,1)

    def test_unavailable_artwork_interface_is_reported(self):
        song=imp.Song(self.song.path,self.song.title,self.song.artist,self.song.album,
                      self.song.size,self.song.modified_ns,self.song.duration,0,True)
        result=self.call('12:99',[song])
        self.assertEqual(result['artwork_unverified'],1)

    def test_missing_target_artwork_is_not_claimed_as_verified(self):
        song=imp.Song(self.song.path,self.song.title,self.song.artist,self.song.album,
                      self.song.size,self.song.modified_ns,self.song.duration,0,True)
        track=Track(1,song.title,song.artist,42);track.Artwork=Items()
        with patch.object(imp.time,'monotonic',side_effect=[0,6]):
            with self.assertRaisesRegex(imp.ImportErrorSafe,'reported none'):
                imp.verify_track_metadata(track,song)

    def test_different_device_is_not_touched(self):
        with self.assertRaisesRegex(imp.ImportErrorSafe, 'selected device changed'):
            imp.perform_import(self.app,'999:999','12:99',None,[self.song],self.journal,self.progress,self.stop)
        self.assertEqual(self.app.source.mix.add_calls, 0)

    def test_cancellation_does_not_start_an_add(self):
        self.stop.set()
        self.assertEqual(self.call('12:99')['added'], 0)
        self.assertEqual(self.app.source.mix.add_calls, 0)

    def test_cancellation_during_hash_does_not_add(self):
        original=imp.content_hash
        def stop_after_hash(song):
            result=original(song);self.stop.set();return result
        with patch.object(imp,'content_hash',side_effect=stop_after_hash):
            self.assertEqual(self.call('12:99')['added'],0)
        self.assertEqual(self.app.source.mix.add_calls,0)

    def test_new_playlist_does_not_replace_existing(self):
        with self.assertRaisesRegex(imp.ImportErrorSafe, 'already exists'):
            imp.perform_import(self.app,self.device,None,'Arabic',[self.song],self.journal,self.progress,self.stop)
        self.assertEqual(self.app.source.Playlists.Count, 3)

    def test_already_open_itunes_can_be_activated_if_not_in_running_object_table(self):
        fake=types.SimpleNamespace(info={'name':'iTunes.exe','username':'User'})
        with patch('win32com.client.GetActiveObject',side_effect=OSError('not registered')), \
             patch('win32com.client.Dispatch',return_value=self.app) as dispatch, \
             patch('psutil.Process',return_value=types.SimpleNamespace(username=lambda:'User')), \
             patch('psutil.process_iter',return_value=[fake]):
            self.assertIs(imp.open_itunes(),self.app)
            dispatch.assert_called_once_with('iTunes.Application')

    def test_itunes_is_never_launched_when_not_already_open(self):
        with patch('win32com.client.GetActiveObject',side_effect=OSError('not registered')), \
             patch('win32com.client.Dispatch') as dispatch, \
             patch('psutil.Process',return_value=types.SimpleNamespace(username=lambda:'User')), \
             patch('psutil.process_iter',return_value=[]):
            with self.assertRaisesRegex(imp.ImportErrorSafe,'Open iTunes first'):
                imp.open_itunes()
            dispatch.assert_not_called()


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.folder=self.root/'Playlists'/'Arabic'; self.folder.mkdir(parents=True)
        for name in ('a.mp3','b.mp3','extra.mp3'):
            (self.folder/name).write_bytes(name.encode())
        (self.root/'Arabic.m3u8').write_text('#EXTM3U\nPlaylists/Arabic/b.mp3\nPlaylists/Arabic/a.mp3\n',encoding='utf-8')
        self.col=imp.Collection('Arabic',self.folder.resolve())

    def test_newest_file_date_drives_order_not_m3u(self):
        dates={'a.mp3':2,'b.mp3':1,'extra.mp3':3}
        with patch.object(imp,'file_date_ns',side_effect=lambda path:dates[path.name]):
            songs,unknown,note=imp.build_plan(self.root,self.col)
        self.assertEqual([x.path.name for x in songs],['extra.mp3','a.mp3','b.mp3'])
        self.assertEqual(unknown,[])

    def test_reference_playlist_does_not_control_order(self):
        (self.root/'Arabic.m3u8').write_text('../outside.mp3\n',encoding='utf-8')
        songs,_,_=imp.build_plan(self.root,self.col)
        self.assertEqual(len(songs),3)

    def test_missing_reference_entry_does_not_hide_real_files(self):
        (self.root/'Arabic.m3u8').write_text('Playlists/Arabic/missing.mp3\n',encoding='utf-8')
        songs,_,_=imp.build_plan(self.root,self.col)
        self.assertEqual(len(songs),3)

    def test_moved_track_is_used_only_when_phone_id_and_content_match(self):
        nasheed=self.root/'Playlists'/'اناشيد'; nasheed.mkdir()
        moved=nasheed/'moved.mp3'; moved.write_bytes(b'original track')
        sha=hashlib.sha256(moved.read_bytes()).hexdigest()
        (self.root/'_metadata_report.json').write_text(json.dumps({'updated':[
            {'id':7,'file':str(Path('Playlists')/'Arabic'/'moved.mp3'),'sha256':sha}]}),encoding='utf-8')
        (self.root/'_nasheed_repair_report.json').write_text(json.dumps({'tracks':[
            {'id':7,'file':'moved.mp3','sha256':sha}]}),encoding='utf-8')
        (self.root/'Arabic.m3u8').write_text('Playlists/Arabic/moved.mp3\n',encoding='utf-8')
        dates={'a.mp3':2,'b.mp3':1,'extra.mp3':3,'moved.mp3':4}
        with patch.object(imp,'file_date_ns',side_effect=lambda path:dates[path.name]):
            songs,_,note=imp.build_plan(self.root,self.col)
        self.assertEqual(songs[0].path,moved.resolve())
        self.assertIn('1 moved track',note)
        self.assertIn(moved.resolve(),[s.path for s in imp.build_plan(self.root,self.col,'oldest')[0]])
        moved.write_bytes(b'changed track')
        self.assertNotIn(moved.resolve(),[s.path for s in imp.build_plan(self.root,self.col)[0]])

    def test_all_music_collects_all_folders(self):
        rest=self.root/'All the rest';rest.mkdir();(rest/'rest.mp3').write_bytes(b'audio')
        collection=imp.collections(self.root)[0]
        self.assertTrue(collection.all_music)
        songs,_,_=imp.build_plan(self.root,collection)
        self.assertEqual({s.path.name for s in songs},{'a.mp3','b.mp3','extra.mp3','rest.mp3'})

    def test_preflight_rescans_newly_added_playlist_songs_by_file_date(self):
        root_collection=imp.collections(self.root)[0]
        original,_,_=imp.build_plan(self.root,root_collection)
        self.assertEqual(len(original),3)
        added=self.folder/'added.mp3';added.write_bytes(b'new song')
        dates={'a.mp3':2,'b.mp3':1,'extra.mp3':3,'added.mp3':4}
        mappings=[(self.col,{'id':'12:99','name':'Arabic'})]
        with patch.object(imp,'file_date_ns',side_effect=lambda path:dates[path.name]):
            songs,_,_,bindings=imp.prepare_import_plan(self.root,root_collection,mappings,{'Arabic'})
        self.assertEqual([s.path.name for s in songs],['added.mp3','extra.mp3','a.mp3','b.mp3'])
        self.assertEqual([s.path.name for s in bindings[0].songs],['added.mp3','extra.mp3','a.mp3','b.mp3'])

    def test_new_playlist_folder_requires_remapping(self):
        root_collection=imp.collections(self.root)[0]
        (self.root/'Playlists'/'New playlist').mkdir()
        with self.assertRaisesRegex(imp.ImportErrorSafe,'map playlists again'):
            imp.prepare_import_plan(self.root,root_collection,[],{'Arabic'})


if __name__=='__main__': unittest.main()
