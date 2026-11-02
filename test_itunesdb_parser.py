"""Offline test for the iTunesDB parser using a synthetic database.

Builds a minimal but structurally valid iTunesDB (mhbd -> mhsd -> mhlt/mhit
and mhlp/mhyp records exactly like the real format), then checks that
parse_itunesdb recovers tracks, metadata, playlist membership and order.
Run:  python test_itunesdb_parser.py
"""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from phone_music_pull import parse_itunesdb, sanitize  # noqa: E402


def u32(v):
    return struct.pack("<I", v)


def header(magic, hlen, tlen):
    return magic + u32(hlen) + u32(tlen)


def mhod_string(mtype, text):
    payload = text.encode("utf-16-le") + b"\x00\x00"
    body = u32(1) + u32(len(payload)) + b"\x00" * 8 + payload
    mhod = header(b"mhod", 0x18, 0x18 + len(body)) + u32(mtype) + b"\x00" * 8
    return mhod + body


def mhod100(position):
    body = u32(position) + b"\x00" * 12
    return header(b"mhod", 0x18, 0x18 + len(body)) + u32(100) + b"\x00" * 8 + body


def mhit(track_id, mhods):
    body = b"".join(mhods)
    hlen = 0x64
    tlen = hlen + len(body)
    head = header(b"mhit", hlen, tlen)
    head += u32(len(mhods))       # mhod count
    head += u32(track_id)         # id @ +16
    head += b"\x00" * (hlen - len(head))
    return head + body


def mhip(track_id, position):
    body = mhod100(position)
    hlen = 0x24
    tlen = hlen + len(body)
    head = header(b"mhip", hlen, tlen)
    head += u32(1)          # one mhod child
    head += b"\x00" * 8
    head += u32(track_id)   # @ +24
    head += b"\x00" * (hlen - len(head))
    return head + body


def mhyp(name, is_master, items):
    title = mhod_string(1, name)
    body = title + b"".join(mhip(tid, pos) for pos, tid in items)
    hlen = 0x64
    tlen = hlen + len(body)
    head = header(b"mhyp", hlen, tlen)
    head += u32(1)                                    # one mhod (title)
    head += u32(len(items))                           # track count @ +16
    head += bytes([1 if is_master else 0, 0, 0, 0])   # type @ +20
    head += b"\x00" * (hlen - len(head))
    return head + body


def container(magic, children, hlen=0x5C):
    return header(magic.encode(), hlen, hlen + len(children)) \
        + b"\x00" * (hlen - 12) + children


def mhsd(stype, children):
    hlen = 0x60
    head = header(b"mhsd", hlen, hlen + len(children))
    head += u32(0)      # padding @ +12
    head += u32(stype)  # section type @ +16
    head += b"\x00" * (hlen - len(head))
    return head + children


def build_db():
    tracks = [
        (101, "Song One", "Artist A", "Album X", "M4A",
         ":iTunes_Control:Music:F00:AAAA.m4a"),
        (202, "Song Two", "Artist B", "Album Y", "M4A",
         ":iTunes_Control:Music:F01:BBBB.m4a"),
        (303, "Protected Song", "Artist C", "Album Z", "M4P",
         ":iTunes_Control:Music:F02:CCCC.m4p"),
    ]
    mhits = b"".join(
        mhit(tid, [mhod_string(1, title), mhod_string(2, path),
                   mhod_string(3, album), mhod_string(4, artist),
                   mhod_string(6, ftype)])
        for tid, title, artist, album, ftype, path in tracks)
    section_tracks = mhsd(1, container("mhlt", mhits))

    master = mhyp("Library", True, [(1, 101), (2, 202), (3, 303)])
    gym = mhyp("Gym", False, [(2, 303), (1, 101)])  # 303 first on device
    section_playlists = mhsd(2, container("mhlp", master + gym))

    body = section_tracks + section_playlists
    hlen = 0x74
    return header(b"mhbd", hlen, hlen + len(body)) + b"\x00" * (hlen - 12) + body


def main():
    db = parse_itunesdb(build_db())

    assert len(db.tracks) == 3, f"expected 3 tracks, got {len(db.tracks)}"
    t = db.tracks[101]
    assert t.title == "Song One", t.title
    assert t.artist == "Artist A", t.artist
    assert t.album == "Album X", t.album
    assert t.path == "/iTunes_Control/Music/F00/AAAA.m4a", t.path
    assert not t.is_drm
    assert db.tracks[303].is_drm, "M4P track must be flagged as DRM"

    user_playlists = [p for p in db.playlists if not p.is_master]
    assert len(user_playlists) == 1, f"expected 1 user playlist, got {len(user_playlists)}"
    gym = user_playlists[0]
    assert gym.name == "Gym", gym.name
    order = [tid for _pos, tid in gym.items]
    assert order == [101, 303], f"playlist order wrong: {order}"
    assert [pos for pos, _tid in gym.items] == [1, 2], "positions must be 1,2"

    assert sanitize("A/B:C*D?") == "A_B_C_D_"
    assert sanitize("   ") == "Unknown"

    print("All parser tests passed.")
    print(f"  tracks: {len(db.tracks)}, playlists: {len(db.playlists)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
