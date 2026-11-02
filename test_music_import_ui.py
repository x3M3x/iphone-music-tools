import unittest
from unittest.mock import patch
import tkinter as tk
from music_import_ui import transfer_order
from music_import_ui import ImportWindow


class ImportUITests(unittest.TestCase):
    def test_newest_preview_is_sent_oldest_first(self):
        self.assertEqual(transfer_order(['new','middle','old'],'Newest file date first'),['old','middle','new'])

    def test_alternative_order_is_sent_as_previewed(self):
        self.assertEqual(transfer_order(['a','b'],'Filename'),['a','b'])

    def test_window_keeps_import_disabled_while_busy(self):
        root=tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        with patch.object(ImportWindow,'load_sources'):
            window=ImportWindow(root)
        root.update_idletasks()
        window.set_running(True)
        self.assertEqual(str(window.start_button['state']),'disabled')
        self.assertEqual(str(window.stop_button['state']),'normal')
        window.set_running(False)
        self.assertEqual(str(window.start_button['state']),'disabled')

    def test_start_requires_phone_destination_and_songs_without_mapping(self):
        root=tk.Tk();root.withdraw();self.addCleanup(root.destroy)
        with patch.object(ImportWindow,'load_sources'):
            window=ImportWindow(root)
        from types import SimpleNamespace
        window.devices={'new':{'id':'new-phone'}};window.device.set('new')
        window.destinations={'all':{'id':'1','kind':1}};window.destination.set('all')
        window.sources={'all':SimpleNamespace(all_music=True)};window.source_name.set('all')
        window.songs=[object()];window.batch_mode.set(True)
        window.update_start_control()
        self.assertEqual(str(window.start_button['state']),'normal')
        window.set_running(True)
        self.assertEqual(str(window.start_button['state']),'disabled')

    def test_confirmation_includes_mapping_and_does_not_start_when_cancelled(self):
        from types import SimpleNamespace
        root=tk.Tk();root.withdraw();self.addCleanup(root.destroy)
        with patch.object(ImportWindow,'load_sources'):
            window=ImportWindow(root)
        song=SimpleNamespace(file_date_ns=0,modified_ns=1,title='Song',artist='Artist')
        group=SimpleNamespace(name='Arabic')
        context={'device':{'name':'New iPhone'},'dest':{'name':'All Music'},
                 'name':None,'batch':True,'mappings':[],'batch_size':10,
                 'automatic_preview':[('Arabic','123')]}
        binding=SimpleNamespace(name='Arabic',songs=[song])
        window.set_running(True)
        with patch('music_import_ui.messagebox.askyesno',return_value=False) as confirm, \
             patch.object(window,'begin_transfer') as transfer:
            window.confirm_preflight((context,[song],[],'', [binding]))
        self.assertIn('Arabic (1 songs)',confirm.call_args.args[1])
        self.assertIn('New iPhone',confirm.call_args.args[1])
        transfer.assert_not_called()
        self.assertFalse(window.importing)

    def test_batch_flag_accepts_safe_bounds(self):
        import argparse
        from music_import_ui import batch_size_argument
        self.assertEqual(batch_size_argument('10'),10)
        self.assertEqual(batch_size_argument('100'),100)
        for value in ('0','101','abc'):
            with self.assertRaises(argparse.ArgumentTypeError):
                batch_size_argument(value)

    def test_confirmed_plan_detects_a_new_playlist_folder(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from music_import_ui import validate_confirmed_plan
        from music_importer import ImportErrorSafe
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'Playlists'/'Arabic').mkdir(parents=True)
            (root/'Playlists'/'New').mkdir()
            (root/'All the rest').mkdir()
            context={'root':root,'source':SimpleNamespace(all_music=True),'batch':True}
            bindings=[SimpleNamespace(name='Arabic',songs=[])]
            with self.assertRaisesRegex(ImportErrorSafe,'folders changed'):
                validate_confirmed_plan(context,[],bindings)


if __name__=='__main__':unittest.main()
