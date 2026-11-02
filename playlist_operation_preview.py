"""Offline preview of an ordered AirTraffic playlist creation operation.

This module never connects to a device, signs, or sends an operation. A live
sender requires fresh catalog validation, a journal, recovery and readback.
The operation shape is documented in work/rustunes/docs/05-receiver-operations.md.
"""
from datetime import datetime, timezone
import plistlib


def _signed_pid(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2**64:
        raise ValueError('IDs must be nonzero unsigned 64-bit integers')
    return value if value < 2**63 else value - 2**64


def create_playlist_preview(*, database_id, library_id, anchor, playlist_id,
                            name, ordered_track_ids, occupied_playlist_ids, when):
    """Serialize one new playlist with explicit track order, for offline review."""
    db_pid, library_pid, playlist_pid = map(_signed_pid, (database_id, library_id, playlist_id))
    occupied = {_signed_pid(value) for value in occupied_playlist_ids}
    if playlist_pid == library_pid or playlist_pid in occupied:
        raise ValueError('Playlist ID is already occupied')
    if isinstance(anchor, bool) or not isinstance(anchor, int) or not 0 <= anchor < 2**32 - 1:
        raise ValueError('Invalid media anchor')
    if not isinstance(name, str) or not name.strip() or len(name) > 255 or any(ord(c) < 32 for c in name):
        raise ValueError('Invalid playlist name')
    if not isinstance(when, datetime) or when.tzinfo is None or when.utcoffset() is None:
        raise ValueError('Timestamp must have a timezone')
    stamp = when.astimezone(timezone.utc).replace(tzinfo=None)
    track_ids = [_signed_pid(value) for value in ordered_track_ids]
    if len(track_ids) != len(set(track_ids)):
        raise ValueError('Repeated playlist tracks require separate verification')
    operation = {
        'operation': 'insert_playlist',
        'pid': playlist_pid,
        'container': {'name': name, 'date_created': stamp, 'date_modified': stamp},
        'container_ui': {'play_order': 1, 'album_field_order': 0, 'repeat_mode': 0,
                         'is_reversed': False, 'shuffle_items': False, 'has_been_shuffled': False},
        'item_to_container': track_ids,
    }
    root = {'revision': anchor + 1, 'timestamp': stamp,
            'operations': [{'operation': 'update_db_info', 'pid': db_pid,
                            'db_info': {'primary_container_pid': library_pid}}, operation]}
    return plistlib.dumps(root, fmt=plistlib.FMT_BINARY, sort_keys=True)


def update_playlist_preview(*, database_id, library_id, anchor, playlist_id,
                            name, ordered_track_ids, existing_track_ids,
                            when, created):
    """Preview a complete ordered update, retaining every existing member.

    This is still offline-only. The caller must obtain an up-to-date playlist
    and verify the ordered update on the phone before attempting another write.
    """
    existing = [_signed_pid(value) for value in existing_track_ids]
    requested = [_signed_pid(value) for value in ordered_track_ids]
    if len(existing) != len(set(existing)):
        raise ValueError('Repeated existing members require separate verification')
    if not set(existing).issubset(requested):
        raise ValueError('Update would remove existing playlist members')
    if not isinstance(created, datetime) or created.tzinfo is None or created.utcoffset() is None:
        raise ValueError('Original playlist creation time must have a timezone')
    data = create_playlist_preview(database_id=database_id, library_id=library_id,
                                   anchor=anchor, playlist_id=playlist_id, name=name,
                                   ordered_track_ids=ordered_track_ids,
                                   occupied_playlist_ids=[], when=when)
    root = plistlib.loads(data)
    operation = root['operations'][1]
    operation['operation'] = 'update_playlist'
    operation['container']['date_created'] = created.astimezone(timezone.utc).replace(tzinfo=None)
    return plistlib.dumps(root, fmt=plistlib.FMT_BINARY, sort_keys=True)
