# iPhone Music Tools for Windows

Two native Windows tools to export locally stored music from an iPhone and import it to another iPhone, keeping music files, artwork, metadata and playlist structure where supported. They use USB and work independently of 3uTools.

**Experimental:** the importer writes to the iPhone Music library using an undocumented sync protocol. Back up your devices and music first, test with a small collection, and verify the results before relying on a large transfer. Compatibility depends on iOS, the device and its library. These tools are not affiliated with Apple.

## Setup

Install Python 3.11 or newer with Tkinter, FFmpeg and FFprobe on PATH, and a working Apple Mobile Device USB connection. Connect and unlock the iPhone and trust the computer before running the tools. In PowerShell, from this folder:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Launch **Open Music Exporter.cmd** or **Open Music Importer.cmd**. Select the intended phone carefully. The exporter reads the phone; the importer writes to the selected phone. Do not disconnect during a transfer.

## Export

Choose the source iPhone, a new output folder and **Start export**. The exporter reads available local files, metadata and phone artwork; it does not download artwork from the internet. Results are organized as `Playlists/<playlist name>/` and `All the rest/`, with M3U8 playlist files and an export report. Tracks without an accessible supported local file, and protected or subscription tracks, may be skipped. Artwork unavailable on the phone cannot be reconstructed.

If you have manually added files to one playlist in an older export, choose that export under **Keep additions** and enter the exact folder name under **Playlist to keep**. That folder is copied unchanged from the old export instead of being rebuilt from the phone. Use a separate output folder.

## Import

Choose the export folder and the destination iPhone. With **All music** and **Import all music, then link playlists** selected, the importer sends songs in batches, matches playlist folders to phone playlists by name, and creates missing playlists. The importer uses the files' saved dates to order the import, oldest first, and links playlists newest first. Identical dates use paths to break ties, so their original relative order cannot be reconstructed from dates alone. Playlist order and Music app display can vary by device and iOS.

Direct import currently accepts MP3 and supported AAC-in-M4A files. It preserves original audio bytes and validates supported metadata, saved dates, and available embedded artwork. PNG artwork is converted to JPEG for the phone display cache; the embedded source artwork is unchanged. Files with unsupported or ambiguous metadata are reported rather than silently changed.

The importer keeps `direct_import_history.sqlite` locally to recognize and verify its own prior uploads. Keep this journal when resuming. It refuses unverified existing music and stops if an operation has an uncertain result. After a disconnect or uncertain result, inspect the phone and journal before retrying; automatic recovery is not yet provided. **Stop after current batch** waits until the active batch finishes.

## Performance and limitations

The window lets you set **Format checks**, **File hashes**, and **Metadata** workers (1–16), **Phone reads** (1–8), and **Songs per batch** (1–100). Each defaults to its maximum. Progress shows the active stage, worker count, and an approximate files-per-second rate for checks. Larger worker counts can be slower on a busy disk or phone, so compare rates with the same library. Phone reads use bounded parallel readback; their performance and stability vary by device and have not yet been benchmarked on real phones.

Uploads are sent in date order in batches; transferred audio and artwork are verified by reading them back. Larger batches reduce session overhead but make interruption recovery more involved. Settings apply when a run starts and cannot be changed during it. No specific transfer rate is guaranteed. Command-line defaults can also be set with `--format-workers`, `--hash-workers`, `--metadata-workers`, `--readback-workers`, and `--batch-size` when launching the importer.

The original user library and device database, journals, reports, personal songs, and pairing records are not part of this repository. Export and import reports can contain file names and other personal information; review them before sharing.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -p "test_*.py"
```

The test suite uses synthetic fixtures and mocked phone operations; it does not establish compatibility with every device. FFmpeg is required for the relevant media test. AirTraffic signing and protocol work includes MIT-licensed material from rustunes; its notice is in `tests-fixtures/rustunes-LICENSE.txt`.
