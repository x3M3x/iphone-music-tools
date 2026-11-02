"""Read the saved ArtworkDB allocation namespace, without changing it.

Adapted from rustunes, MIT licensed, copyright 2026 Feim Biba. A caller must
also check the current phone SQL catalog and its own journal reservations.
"""
import struct


class ArtworkInventoryError(Exception):
    pass


def parse(data):
    if not 132 <= len(data) <= 32 * 1024 * 1024:
        raise ArtworkInventoryError('ArtworkDB size is invalid')
    if data[:4] == b'mhfd':
        endian = '<'
    elif data[:4] == b'dfhm':
        endian = '>'
    else:
        raise ArtworkInventoryError('ArtworkDB signature is invalid')

    def number(buf, offset, fmt):
        try:
            return struct.unpack_from(endian + fmt, buf, offset)[0]
        except struct.error as exc:
            raise ArtworkInventoryError('Truncated ArtworkDB') from exc

    def header(buf, magic, minimum):
        expected = magic if endian == '<' else magic[::-1]
        if len(buf) < minimum or buf[:4] != expected:
            raise ArtworkInventoryError('Invalid ArtworkDB record')
        length = number(buf, 4, 'I')
        if not minimum <= length <= len(buf):
            raise ArtworkInventoryError('Invalid ArtworkDB header length')
        return length

    def record(buf, magic, minimum):
        head = header(buf, magic, minimum)
        length = number(buf, 8, 'I')
        if length < head or length > len(buf) or length % 4:
            raise ArtworkInventoryError('Invalid ArtworkDB record length')
        return buf[:length], head

    root, offset = record(data, b'mhfd', 132)
    if len(root) != len(data) or number(root, 12, 'I') != 0:
        raise ArtworkInventoryError('Unsupported ArtworkDB version or length')
    count, next_id = number(root, 20, 'I'), number(root, 28, 'I')
    if not 1 <= count <= 3:
        raise ArtworkInventoryError('Invalid ArtworkDB section count')
    sections, ids = set(), set()
    budget = 500000
    for _ in range(count):
        section, head = record(root[offset:], b'mhsd', 96)
        kind = number(section, 12, 'H')
        if kind not in (1,2,3) or kind in sections:
            raise ArtworkInventoryError('Unsupported ArtworkDB section')
        sections.add(kind)
        if kind == 1:
            entries = section[head:]
            at = header(entries, b'mhli', 92)
            size = number(entries, 8, 'I')
            if size > 100000:
                raise ArtworkInventoryError('ArtworkDB image count is too large')
            for _ in range(size):
                image, child_at = record(entries[at:], b'mhii', 152)
                art_id, children = number(image, 16, 'I'), number(image, 12, 'I')
                if not art_id or art_id in ids or children > budget:
                    raise ArtworkInventoryError('Invalid or duplicate ArtworkDB image')
                ids.add(art_id)
                budget -= children
                for _ in range(children):
                    child, _ = record(image[child_at:], b'mhod', 24)
                    child_at += len(child)
                if child_at != len(image):
                    raise ArtworkInventoryError('Invalid ArtworkDB child records')
                at += len(image)
            if at != len(entries):
                raise ArtworkInventoryError('Invalid ArtworkDB image records')
        offset += len(section)
    if offset != len(root) or 1 not in sections or not 100 <= next_id < 2**32 - 1:
        raise ArtworkInventoryError('ArtworkDB allocation state is invalid')
    if any(value >= next_id for value in ids):
        raise ArtworkInventoryError('ArtworkDB allocation counter is stale')
    return next_id, frozenset(ids)


def reserve(next_id, saved_ids, live_ids, journal_ids):
    """Choose an ID above both saved and current catalog/journal IDs."""
    all_ids = set(saved_ids) | set(live_ids) | set(journal_ids)
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 or n >= 2**32 for n in all_ids):
        raise ArtworkInventoryError('Invalid existing artwork ID')
    candidate = max([next_id - 1, *all_ids], default=99) + 1
    if not 100 <= candidate < 2**32 - 1:
        raise ArtworkInventoryError('Artwork ID space is exhausted')
    return candidate
