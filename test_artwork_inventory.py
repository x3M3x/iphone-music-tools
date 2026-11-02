import struct
import unittest
from artwork_inventory import ArtworkInventoryError, parse, reserve


def fixture(big=False):
    endian = '>' if big else '<'
    def block(magic, head, total):
        out = bytearray(total)
        out[:4] = magic[::-1] if big else magic
        struct.pack_into(endian+'II',out,4,head,total)
        return out
    image = block(b'mhii',152,152)
    struct.pack_into(endian+'II',image,12,0,100)
    images = block(b'mhli',92,92)
    struct.pack_into(endian+'I',images,8,1)
    images.extend(image)
    section = block(b'mhsd',96,96)
    struct.pack_into(endian+'H',section,12,1)
    section.extend(images)
    struct.pack_into(endian+'I',section,8,len(section))
    root = block(b'mhfd',132,132)
    struct.pack_into(endian+'I',root,20,1)
    struct.pack_into(endian+'I',root,28,101)
    root.extend(section)
    struct.pack_into(endian+'I',root,8,len(root))
    return bytes(root)


class ArtworkInventoryTests(unittest.TestCase):
    def test_reads_endianness_and_avoids_all_occupied_ids(self):
        for big in (False, True):
            next_id, ids = parse(fixture(big))
            self.assertEqual((next_id, ids),(101,frozenset({100})))
            self.assertEqual(reserve(next_id,ids,{107},{110}),111)

    def test_rejects_truncated_or_stale_data(self):
        data = fixture()
        for size in range(len(data)):
            with self.subTest(size=size), self.assertRaises(ArtworkInventoryError):
                parse(data[:size])
        bad = bytearray(data)
        struct.pack_into('<I',bad,28,100)
        with self.assertRaisesRegex(ArtworkInventoryError,'stale'):
            parse(bytes(bad))


if __name__ == '__main__':
    unittest.main()
