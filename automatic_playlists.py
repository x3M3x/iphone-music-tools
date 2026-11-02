"""Resolve folder names against a fresh phone catalog without writing."""
from direct_phone import PhoneReadError


def match_playlist(catalog, name):
    if not isinstance(name,str) or not name.strip() or len(name)>255 or any(ord(c)<32 for c in name):
        raise PhoneReadError('A playlist folder has an invalid name.')
    key=name.strip().casefold()
    names=catalog.all_playlist_names or tuple((p.id,p.name) for p in catalog.playlists)
    matches=[(pid,label) for pid,label in names if label.strip().casefold()==key]
    if len(matches)>1:
        raise PhoneReadError(f'More than one phone playlist is named {name}; resolve the duplicate names first.')
    if not matches:
        return None
    pid,label=matches[0]
    eligible=[p for p in catalog.playlists if p.id==pid and p.name==label]
    if len(eligible)!=1:
        raise PhoneReadError(f'The existing playlist {name} cannot be safely edited by this importer.')
    return eligible[0]


def check_folder_names(names):
    seen=set()
    for name in names:
        key=name.strip().casefold()
        if not key or key in seen:
            raise PhoneReadError('Playlist folder names are empty or ambiguous; rename them first.')
        seen.add(key)
