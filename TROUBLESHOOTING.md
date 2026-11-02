# Troubleshooting

## Phone is not listed

Connect the intended iPhone over USB, unlock it and establish trust with the computer. Ensure the Apple Mobile Device USB connection works, then select **Check iPhone** or **Refresh**. The tools do not automatically pair devices.

## Import stopped or connection terminated

Keep the phone connected and inspect the error and local import report. The journal can contain a pending operation even when the cable stayed connected. Do not delete the journal, manually delete phone tracks, or start the same write again until the phone state and pending operation are reconciled. The current public importer does not yet provide automatic interrupted-write recovery; recovery requires an exact inspection of the target phone and saved plan.

The phone may end a sync session during a long transfer. The importer reads and answers phone sync pings and sends transfer progress while audio is staged. Files larger than 256 MiB are sent individually within the normal run. A connection loss during upload remains an uncertain write and is not retried blindly.

## Artist or album mismatch

The phone can group different artist spellings into one display group, including some Unicode variants. The importer stops if the readback differs from the planned metadata. Inspect the original embedded tags, the saved plan and the phone's actual display group; do not silently replace the original tags or disable verification. The exact spelling shown in Music may differ from the file's embedded tags. The user must choose whether an observed display difference is acceptable before reconciliation.

## Missing artwork or songs

The exporter reads artwork available locally on the phone and preserves artwork already embedded in a file when it cannot retrieve a phone original. It does not search the internet for covers. Some protected or subscription tracks and items without a supported local file cannot be exported. Check the local export report for failures or missing artwork.

## Moved music or changed files

Choose the new export folder with **Browse**. The importer derives ordering from file dates and folder membership from the current files. Moving files across drives can alter creation dates, so verify the saved dates before importing. The journal stores source paths; moved files can cause preflight to treat them as new, while it still matches actual imports by content hash. Keep the original journal and inspect any mismatch rather than deleting it or forcing a sync.

## Privacy

Music, reports, SQLite journals and backups, device databases, pairing records, and debug logs are personal. Keep them outside commits and public issues. A copy on the same drive is not an independent backup.
