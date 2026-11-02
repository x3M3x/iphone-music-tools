import plistlib
import unittest
from pathlib import Path

from atc_signing import HOST_REQUEST, sign_metadata


class SigningTests(unittest.TestCase):
    def test_reference_vectors_and_limits(self):
        fixture = Path(__file__).resolve().parent / 'tests-fixtures/grappa-fixed-vectors.plist'
        vectors = plistlib.loads(fixture.read_bytes())
        self.assertEqual(len(vectors), 4)
        for vector in vectors:
            self.assertEqual(HOST_REQUEST, vector['request'])
            self.assertEqual(sign_metadata(vector['reply'], vector['operation']), vector['sidecar'])
            with self.assertRaises(ValueError):
                sign_metadata(vector['reply'][:-1], vector['operation'])
            with self.assertRaises(ValueError):
                sign_metadata(vector['reply'], b'')


if __name__ == '__main__':
    unittest.main()
