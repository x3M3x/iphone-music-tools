import unittest
import json
import tempfile
from pathlib import Path
from music_export_ui import cover_status, parse_progress


class ProgressTests(unittest.TestCase):
    def test_progress_counts(self):
        self.assertEqual(parse_progress('PROGRESS {"completed": 12, "failed": 1, "total": 20}'), (12, 1, 20))
        self.assertEqual(parse_progress('PROGRESS {"completed": 0, "failed": 0, "total": 20}'), (0, 0, 20))

    def test_invalid_or_unrelated_lines_are_ignored(self):
        for line in ['hello', 'PROGRESS {}', 'PROGRESS bad',
                     'PROGRESS {"completed": 30, "failed": 0, "total": 20}',
                     'PROGRESS {"completed": -1, "failed": 0, "total": 20}']:
            self.assertIsNone(parse_progress(line))

    def test_summary_counts_unavailable_original_art(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertIsNone(cover_status(root))
            (root / '_rebuild_report.json').write_text(json.dumps({'tracks': [
                {'artwork': 'phone original'}, {'artwork': 'not available in library'},
                {'artwork': 'original embedded cover retained'}]}), encoding='utf-8')
            self.assertEqual(cover_status(root), 1)

    def test_missing_art_count_excludes_removed_track(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '_rebuild_report.json').write_text(json.dumps({'tracks': [
                {'artwork': 'not available in library', 'files': ['removed.mp3']},
                {'artwork': 'not available in library', 'files': ['kept.mp3']}]}), encoding='utf-8')
            (root / 'kept.mp3').touch()
            self.assertEqual(cover_status(root), 1)


if __name__ == '__main__':
    unittest.main()
