# Development notes

This repository contains separate Windows iPhone music exporter and importer tools. Keep both launchers working and do not assume they have been combined.

The exporter is read-only on the source device. The importer writes to the explicitly selected destination and must preserve existing music, source audio bytes, embedded artwork, metadata, dates, playlist membership and ordering wherever supported. Never bypass a pending-operation check or blindly replay an uncertain phone write. Do not use actual phone identifiers, pairing data, music, local journals, personal reports or real library databases in the public repository.

On Windows, install the dependencies from requirements.txt and put ffmpeg and ffprobe on PATH. Run `.\.venv\Scripts\python.exe -m unittest discover -p "test_*.py"`. The tests use synthetic fixtures; passing them does not prove compatibility with a particular phone. Any device test requires a clearly selected device, a recoverable test plan, and verification of the result.

Read README.md and TROUBLESHOOTING.md before changing device behavior. In particular, keep the AirTraffic reader active during audio transfer so phone pings are answered, and retain the per-batch journal and full verification of new uploads.
