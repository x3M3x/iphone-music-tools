#!/usr/bin/env python3
"""
phone_music_pull.py - read-only iPhone/iPad music exporter with playlist folders.

Copies music off a USB-connected iPhone/iPad over the AFC media channel (the
same channel Finder/iTunes/iMazing use for media), keeping:
  - one folder per playlist, tracks numbered in playlist order
  - an "All the rest" folder for tracks that are in no playlist
  - existing embedded artwork preserved inside copied files
  - a report of exported / skipped (DRM-protected) tracks

DEVICE SAFETY - this tool is strictly read-only against the device:
  - lockdown is used to read device information only
  - AFC usage is limited to listdir/stat/get_file_contents/pull (reads)
  - no AFC write/rename/mkdir/remove call exists anywhere in this file
  - no restore, backup, sync, pairing, or firmware API is used
  - autopair=False: if the computer is not already paired with the device,
    the tool aborts rather than triggering anything on the phone

Requires: Python 3.9+, pymobiledevice3, mutagen
"""

from __future__ import annotations

import os
import argparse
import asyncio
import posixpath
import re
import shutil
import sqlite3
import struct
import sys
import tempfile
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path


def _use_bundled_venv():
    """Re-run this script with the .venv bundled next to it, if present.

    Makes './phone_music_pull.py' work from any shell regardless of which
    Python is associated with .py files: the bundled virtualenv carries the
    required libraries.
    """
    venv_python = Path(__file__).resolve().parent / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
        os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]])


if __name__ == "__main__":
    _use_bundled_venv()


try:
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService
except ImportError:
    sys.exit(
        "pymobiledevice3 is not installed.\n"
        "Fix: run  .venv\\Scripts\\pip install -r requirements.txt\n"
        "(from this folder)")

try:
    import mutagen
except ImportError:
    mutagen = None


# ---------------------------------------------------------------------------
# iTunesDB binary format parser
#
# Layout verified against libgpod's src/itdb_itunesdb.c / itdb.h (the
# reference open-source implementation of this format):
#   mhbd -> mhsd* ; mhsd type 1 = mhlt -> mhit* (tracks, each with mhod
#   string children); mhsd type 2 = mhlp -> mhyp* (playlists, each with an
#   mhod title child followed by mhip* items; each mhip carries the track id
#   plus an mhod type-100 child holding the position in the playlist).
# ---------------------------------------------------------------------------

MHOD_STRING_TYPES = {
    1: "title",
    2: "path",
    3: "album",
    4: "artist",
    5: "genre",
    6: "filetype",
    8: "comment",
    12: "composer",
    13: "grouping",
    22: "albumartist",
}


def _u16(buf: bytes, off: int) -> int:
    return struct.unpack_from("<H", buf, off)[0]


def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _u64(buf: bytes, off: int) -> int:
    return struct.unpack_from("<Q", buf, off)[0]


def _magic(buf: bytes, off: int) -> bytes:
    return buf[off : off + 4]


@dataclass
class Track:
    track_id: int = 0
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    genre: str = ""
    path: str = ""  # device path, e.g. /iTunes_Control/Music/F00/ABCD.m4a
    filetype: str = ""
    protected: bool = False

    @property
    def is_drm(self) -> bool:
        return self.protected or self.filetype.upper() == "M4P" or self.path.lower().endswith(".m4p")


@dataclass
class Playlist:
    name: str = ""
    is_master: bool = False
    sort_order: int = 0
    items: list = field(default_factory=list)  # [(position, track_id)]


@dataclass
class ItunesDb:
    tracks: dict = field(default_factory=dict)  # track_id -> Track
    playlists: list = field(default_factory=list)


def _parse_mhod_string(buf: bytes, off: int):
    """Parse an mhod string record at 'off'. Returns the decoded string."""
    if _magic(buf, off) != b"mhod":
        return None
    encoding = _u32(buf, off + 24)
    length = _u32(buf, off + 28)
    data_off = off + 40
    if length == 0 or data_off + length > len(buf):
        return ""
    raw = buf[data_off : data_off + length]
    if encoding == 2:
        return raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
    try:
        return raw.decode("utf-16-le", "replace").split("\x00", 1)[0]
    except Exception:
        return ""


def _iter_mhods(buf: bytes, start: int, count: int):
    """Yield (offset, type, total_len) for 'count' mhod children at 'start'."""
    off = start
    for _ in range(count):
        if off + 24 > len(buf) or _magic(buf, off) != b"mhod":
            return
        hlen = _u32(buf, off + 4)
        tlen = _u32(buf, off + 8)
        if tlen < hlen or off + tlen > len(buf):
            return
        yield off, _u32(buf, off + 12), tlen
        off += tlen


def _parse_mhit(buf: bytes, off: int):
    if _magic(buf, off) != b"mhit":
        return None
    hlen = _u32(buf, off + 4)
    tlen = _u32(buf, off + 8)
    mhod_count = _u32(buf, off + 12)
    track = Track(track_id=_u32(buf, off + 16))
    for mhod_off, mhod_type, _tl in _iter_mhods(buf, off + hlen, mhod_count):
        key = MHOD_STRING_TYPES.get(mhod_type)
        if key == "path":
            raw = _parse_mhod_string(buf, mhod_off) or ""
            track.path = "/" + raw.lstrip(":").replace(":", "/") if raw else ""
        elif key:
            setattr(track, key, _parse_mhod_string(buf, mhod_off) or "")
    if tlen <= 0:
        return None
    track._tlen = tlen  # internal: record size for walking
    return track


def _parse_mhip(buf: bytes, off: int):
    """Return (track_id, position, total_len) for an mhip playlist item."""
    if _magic(buf, off) != b"mhip":
        return None
    hlen = _u32(buf, off + 4)
    tlen = _u32(buf, off + 8)
    mhod_count = _u32(buf, off + 12)
    track_id = _u32(buf, off + 24)
    position = 0
    for mhod_off, mhod_type, _tl in _iter_mhods(buf, off + hlen, mhod_count):
        if mhod_type == 100:
            position = _u32(buf, mhod_off + 24)
    return track_id, position, tlen


def _parse_mhyp(buf: bytes, off: int):
    if _magic(buf, off) != b"mhyp":
        return None
    hlen = _u32(buf, off + 4)
    tlen = _u32(buf, off + 8)
    mhod_count = _u32(buf, off + 12)
    item_count = _u32(buf, off + 16)
    pl = Playlist()
    pl.is_master = _u32(buf, off + 20) & 0xFF == 1
    pl.sort_order = _u32(buf, off + 44)
    end = off + tlen

    cursor = off + hlen
    for mhod_off, mhod_type, mhod_tlen in _iter_mhods(buf, cursor, mhod_count):
        if mhod_type == 1:
            pl.name = _parse_mhod_string(buf, mhod_off) or ""
        cursor = mhod_off + mhod_tlen

    for _ in range(item_count):
        parsed = _parse_mhip(buf, cursor)
        if parsed is None:
            break
        track_id, position, mhip_tlen = parsed
        if mhip_tlen <= hlen and position == 0:
            # very old DBs mis-set the item length; recover via mhod walk
            nxt = cursor + hlen
            guard = 0
            while nxt + 24 <= end and _magic(buf, nxt) != b"mhip" and guard < 64:
                if _magic(buf, nxt) != b"mhod":
                    break
                nxt += _u32(buf, nxt + 8)
                guard += 1
            mhip_tlen = nxt - cursor
        if mhip_tlen <= 0:
            break
        pl.items.append((position, track_id))
        cursor += mhip_tlen

    pl.items.sort()
    pl._tlen = tlen
    return pl


def parse_itunesdb(data: bytes) -> ItunesDb:
    db = ItunesDb()
    if len(data) < 32 or _magic(data, 0) != b"mhbd":
        raise ValueError("not an iTunesDB file (missing mhbd header)")
    mhbd_hlen = _u32(data, 4)
    off = mhbd_hlen
    while off + 96 <= len(data):
        if _magic(data, off) != b"mhsd":
            break
        hlen = _u32(data, off + 4)
        tlen = _u32(data, off + 8)
        stype = _u32(data, off + 16)
        end = off + tlen
        child = off + hlen
        if stype == 1 and _magic(data, child) == b"mhlt":
            cursor = child + _u32(data, child + 4)
            while cursor + 104 <= end and _magic(data, cursor) == b"mhit":
                tr = _parse_mhit(data, cursor)
                if tr is None:
                    break
                db.tracks[tr.track_id] = tr
                cursor += getattr(tr, "_tlen", _u32(data, cursor + 8))
        elif stype == 2 and _magic(data, child) == b"mhlp":
            cursor = child + _u32(data, child + 4)
            while cursor + 100 <= end and _magic(data, cursor) == b"mhyp":
                pl = _parse_mhyp(data, cursor)
                if pl is None:
                    break
                db.playlists.append(pl)
                cursor += getattr(pl, "_tlen", _u32(data, cursor + 8))
        if tlen <= 0:
            break
        off += tlen
    return db


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------

ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name: str, fallback: str = "Unknown") -> str:
    name = ILLEGAL.sub("_", (name or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:170] or fallback


def track_display_name(track: Track, tags: dict):
    title = (tags or {}).get("title") or track.title or "Unknown Title"
    artist = (tags or {}).get("artist") or track.artist or "Unknown Artist"
    return sanitize(artist), sanitize(title)


def read_tags(path: Path) -> dict:
    """Read a few tags with mutagen; never raises."""
    if mutagen is None:
        return {}
    try:
        f = mutagen.File(path, easy=True)
        if f is None:
            return {}
        def get(k):
            v = f.get(k)
            return str(v[0]) if v else ""
        return {"title": get("title"), "artist": get("artist"), "album": get("album")}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Main export flow (async: pymobiledevice3 exposes an asyncio API)
# ---------------------------------------------------------------------------


async def connect(serial):
    """Connect to an ALREADY-PAIRED device. Never pairs, never writes."""
    client = await create_using_usbmux(serial=serial, autopair=False, connection_type="USB")
    if not client.paired:
        await client.close()
        raise RuntimeError("This phone is not paired with this computer; pair it separately before using this tool.")
    return client


class DeviceSelectionError(Exception):
    pass


async def choose_device(serial):
    from pymobiledevice3.usbmux import list_devices

    connected = await list_devices()
    devices = [device for device in connected if device.is_usb]
    if serial:
        matches = [device for device in devices if device.matches_udid(serial)]
        if len(matches) != 1:
            raise DeviceSelectionError("The specified USB device was not found uniquely.")
        return matches[0].serial
    if len(devices) == 1:
        return devices[0].serial
    if not devices:
        detail = " A device was detected over the network, but this tool currently requires USB." if connected else ""
        raise DeviceSelectionError(
            "No iPhone or iPad was detected over USB by Apple Mobile Device Service."
            + detail + " After any restore finishes, connect and unlock the intended phone and try again."
        )
    print("Multiple USB devices detected:")
    for device in devices:
        print(f"  {device.serial}")
    raise DeviceSelectionError("Choose the intended phone using --serial UDID.")


async def device_info(lockdown) -> dict:
    info = {}
    for key in ("DeviceName", "ProductType", "ProductVersion", "SerialNumber",
                "UniqueDeviceID"):
        try:
            info[key] = await lockdown.get_value(key=key)
        except Exception:
            info[key] = "?"
    return info


class MusicLibraryError(Exception):
    pass


def parse_media_library(path: Path) -> ItunesDb:
    """Read the iOS SQLite library snapshot without modifying it."""
    db = ItunesDb()
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            rows = conn.execute("""
                SELECT i.item_pid, e.title, e.location, e.media_kind,
                       artist.item_artist AS artist, album.album AS album,
                       b.path AS base_path, s.is_protected, s.is_subscription
                FROM item i JOIN item_extra e ON i.item_pid = e.item_pid
                LEFT JOIN item_artist artist ON artist.item_artist_pid = i.item_artist_pid
                LEFT JOIN album ON album.album_pid = i.album_pid
                LEFT JOIN base_location b ON b.base_location_id = i.base_location_id
                LEFT JOIN item_store s ON s.item_pid = i.item_pid
                WHERE e.media_kind = 1
            """)
            for row in rows:
                base = (row["base_path"] or "").replace(":", "/").strip("/")
                location = (row["location"] or "").replace(":", "/").strip("/")
                path_parts = (base + "/" + location).split("/")
                remote = ""
                if location and not any(part in ("..", ".") for part in path_parts):
                    if location.startswith(("iTunes_Control/", "Purchases/", "Podcasts/")):
                        remote = "/" + location
                    elif re.fullmatch(r"F\d{2}", base, re.IGNORECASE):
                        remote = posixpath.join("/iTunes_Control/Music", base, location)
                    elif base:
                        remote = "/" + posixpath.join(base, location)
                tid = row["item_pid"]
                db.tracks[tid] = Track(track_id=tid, title=row["title"] or "",
                    artist=row["artist"] or "", album=row["album"] or "", path=remote,
                    protected=bool(row["is_protected"] or row["is_subscription"]))
            for row in conn.execute("""
                SELECT container_pid, name FROM container
                WHERE COALESCE(is_hidden, 0) = 0 AND COALESCE(distinguished_kind, 0) = 0
                ORDER BY name COLLATE NOCASE, container_pid
            """):
                items = conn.execute("""
                    SELECT position, item_pid FROM container_item
                    WHERE container_pid = ? ORDER BY position, container_item_pid
                """, (row["container_pid"],))
                db.playlists.append(Playlist(name=row["name"] or "Playlist",
                    items=[(pos or 0, tid) for pos, tid in items if tid in db.tracks]))
    except sqlite3.Error as exc:
        raise MusicLibraryError(f"Unsupported or incomplete music database: {exc}") from exc
    return db


async def pull_library_database(afc: AfcService) -> ItunesDb:
    from pymobiledevice3.exceptions import AfcFileNotFoundError

    modern = "/iTunes_Control/iTunes/MediaLibrary.sqlitedb"
    try:
        await afc.stat(modern)
    except AfcFileNotFoundError:
        try:
            return parse_itunesdb(await afc.get_file_contents("/iTunes_Control/iTunes/iTunesDB"))
        except AfcFileNotFoundError as exc:
            raise MusicLibraryError("No supported music database was found at the expected paths.") from exc

    with tempfile.TemporaryDirectory(prefix="iphone-music-") as temp:
        local = Path(temp) / "MediaLibrary.sqlitedb"
        await afc.pull(modern, str(local), progress_bar=False)
        try:
            await afc.pull(modern + "-wal", str(local) + "-wal", progress_bar=False)
        except AfcFileNotFoundError:
            pass
        return parse_media_library(local)


def plan_export(db: ItunesDb):
    """Build the copy plan: playlist folders + everything else."""
    playlists = [p for p in db.playlists
                 if not p.is_master and p.items and p.name.strip().casefold() != "recently imported"]
    playlists.sort(key=lambda p: p.sort_order if p.sort_order else 0)

    in_any_playlist = set()
    plan = {"playlists": [], "rest": []}
    for pl in playlists:
        entries = []
        for position, tid in pl.items:
            if tid in db.tracks:
                entries.append((position, tid))
                in_any_playlist.add(tid)
        plan["playlists"].append((pl.name, entries))
    plan["rest"] = [tid for tid in db.tracks if tid not in in_any_playlist]
    return plan


async def run(args) -> int:
    lockdown = await connect(await choose_device(args.serial))
    info = await device_info(lockdown)
    print(f"Device: {info.get('DeviceName')} ({info.get('ProductType')}, "
          f"iOS {info.get('ProductVersion')}) UDID {info.get('UniqueDeviceID')}")

    afc = AfcService(lockdown)
    await afc.connect()
    db = await pull_library_database(afc)
    plan = plan_export(db)
    print(f"Parsed {len(db.tracks)} tracks, {len(plan['playlists'])} exportable playlists")
    for name, entries in plan["playlists"]:
        print(f"  playlist '{name}': {len(entries)} tracks")
    print(f"  outside playlists: {len(plan['rest'])} tracks")

    drm = [t for t in db.tracks.values() if t.is_drm]
    no_location = [t for t in db.tracks.values() if not t.path]
    print(f"  tracks with a supported local path: {sum(bool(t.path) for t in db.tracks.values())}")
    if no_location:
        print(f"  tracks without a supported local path: {len(no_location)}")
    if drm:
        print(f"  protected or subscription tracks (will be skipped): {len(drm)}")

    if args.info:
        return 0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tmp_dir = out / "_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    total = 0
    limit = args.limit if args.limit else 10**9
    report = []
    jobs = max(1, getattr(args, "jobs", 4) or 1)
    gate = asyncio.Semaphore(jobs)          # concurrent AFC pulls
    write_lock = asyncio.Lock()             # serialize naming + disk writes

    def dest_folders(tid):
        for name, entries in plan["playlists"]:
            for index, (_pos, e_tid) in enumerate(entries, 1):
                if e_tid == tid:
                    yield (f"Playlists/{sanitize(name, 'Playlist')}", f"{index:03d}. ")
        if tid in plan["rest"]:
            yield ("All the rest", "")

    async def copy_track(tid) -> bool:
        nonlocal total
        track = db.tracks[tid]
        if track.is_drm:
            report.append(f"SKIP (protected or subscription) {track.artist} - {track.title}")
            return False
        if not track.path:
            report.append(f"SKIP (no path) {track.artist} - {track.title}")
            return False
        try:
            info = await afc.stat(track.path)
        except Exception:
            report.append(f"MISSING on device: {track.path}")
            return False
        device_size = int(info.get("st_size") or 0)

        ext = posixpath.splitext(track.path)[1] or ".m4a"

        # Fast resume path: a destination file of the exact device size is
        # treated as already processed, so no bytes are pulled again.
        db_artist, db_title = track_display_name(track, {})
        pending = []
        for sub, prefix in dest_folders(tid):
            folder = out / sub
            folder.mkdir(parents=True, exist_ok=True)
            done = folder / f"{prefix}{db_artist} - {db_title}{ext}"
            if done.exists() and device_size and done.stat().st_size == device_size:
                continue
            pending.append((folder, prefix))
        if not pending:
            total += 1
            print(f"  [{total}] {db_artist} - {db_title}{ext} (already exported)")
            return True

        tmp_file = tmp_dir / f"pull_{tid}{ext}"
        await afc.pull(track.path, str(tmp_file), progress_bar=False)
        tags = read_tags(tmp_file)
        artist, title = track_display_name(track, tags)
        actual_size = tmp_file.stat().st_size

        async with write_lock:
            for folder, prefix in pending:
                base = f"{prefix}{artist} - {title}{ext}"
                dst = folder / base
                if dst.exists():
                    if dst.stat().st_size == actual_size:
                        continue  # same track already exported here
                    n = 2  # a different track with the same name: keep both
                    while True:
                        alt = folder / f"{prefix}{artist} - {title} ({n}){ext}"
                        if not alt.exists():
                            dst = alt
                            report.append(f"NAME CLASH kept as {alt.name} in {folder.name}")
                            break
                        if alt.stat().st_size == actual_size:
                            dst = None
                            break
                        n += 1
                    if dst is None:
                        continue
                shutil.copyfile(tmp_file, dst)
        tmp_file.unlink(missing_ok=True)
        total += 1
        print(f"  [{total}] {artist} - {title}{ext}")
        return True

    ordered_ids = []
    for _name, entries in plan["playlists"]:
        ordered_ids.extend(tid for _pos, tid in entries)
    ordered_ids.extend(plan["rest"])
    seen = set()
    candidates = []
    for tid in ordered_ids:
        if tid in seen or tid not in db.tracks:
            continue
        seen.add(tid)
        candidates.append(tid)
        if len(candidates) >= limit:
            break

    async def worker(tid):
        async with gate:
            await copy_track(tid)

    await asyncio.gather(*(worker(tid) for tid in candidates))

    for f in tmp_dir.iterdir():
        f.unlink(missing_ok=True)
    try:
        tmp_dir.rmdir()
    except OSError:
        pass

    lines = [f"Exported {total} tracks "
             f"({len(plan['playlists'])} playlists + All the rest)",
             f"Protected or subscription tracks in library: {len(drm)}", ""] + report
    (out / "_report.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"Done. {total} tracks exported to {out}")
    if drm:
        print(f"Note: {len(drm)} protected or subscription track(s) in the library are skipped.")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Read-only iPhone music exporter with playlist folders")
    ap.add_argument("--out", default="MusicExport", help="output directory")
    ap.add_argument("--serial", default=None, help="device UDID/serial")
    ap.add_argument("--limit", type=int, default=None,
                    help="export at most N tracks (for a test run)")
    ap.add_argument("--jobs", type=int, default=4,
                    help="parallel transfers (default 4; try 6-8 if USB allows)")
    ap.add_argument("--info", action="store_true",
                    help="only show device + library summary, copy nothing")
    args = ap.parse_args()
    try:
        sys.exit(asyncio.run(run(args)))
    except (DeviceSelectionError, MusicLibraryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
