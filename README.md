# OneDriveRclone

`onedrive-rclone` copies a list of files to Microsoft OneDrive with [rclone](https://rclone.org) and proves each one arrived. OneDrive's own hash is QuickXorHash, not MD5, so rclone computes QuickXorHash on both sides and every file is compared before it counts as copied. It writes one JSON line per file, so whatever runs it (a cron job, a backup catalog, you) knows exactly what's offsite.

It's a single Python file with no dependencies beyond Python 3.9+ and rclone 1.60 or newer. It knows nothing about where the list came from: no database and no settings file.

## Install

```bash
pip install git+https://github.com/kananlanginhooper/OneDriveRclone
```

Or copy `onedrive_rclone.py` anywhere and run it with `python3 onedrive_rclone.py ...`.

## Set up the OneDrive remote

Create an rclone remote of type `onedrive` (`rclone config`). Using your own app registration (client id and secret from the Azure portal) instead of rclone's shared one avoids its throttling. The `docs/` folder here is the homepage and privacy policy this repo's app registration uses.

On a server without a browser, run `rclone config` there and forward the sign-in port from your own machine (`ssh -L 53682:127.0.0.1:53682 <server>`), or run `rclone authorize "onedrive"` on a machine that has a browser and paste the token back.

## Push

```bash
onedrive-rclone push --remote onedrive: --dest backups --list files.jsonl --manifest results.jsonl
```

`files.jsonl` has one JSON object per line:

```json
{"local_path": "/srv/papers/2024/tax.pdf", "remote_path": "papers/2024/tax.pdf", "sha256": "9f86d08..."}
```

- `remote_path` is relative to `--dest`.
- `sha256` is optional. When given, a file whose content no longer matches it is **not uploaded** (`local_mismatch`), so a damaged local copy never replaces a good offsite one.

For each file, in chunks of at most 300 files or 20 GB (`--max-files`, `--max-gb`):

1. It checks the SHA-256, and that the file is under OneDrive's 250 GB limit.
2. It skips the file if OneDrive already has the same QuickXorHash at that path.
3. It uploads the rest with one `rclone copy`. A file whose local path doesn't end in its `remote_path` goes up on its own with `rclone copyto`.
4. It reads OneDrive's hash back and compares.

Each result is appended to the manifest (default stdout) as soon as it's known:

```json
{"local_path": "...", "remote_path": "backups/papers/2024/tax.pdf", "size": 123, "sha256": "...",
 "remote_hash": "...", "hash_type": "quickxor", "status": "copied", "at": "2026-10-29T20:00:00Z"}
```

| status | meaning |
|---|---|
| `copied` | uploaded, and OneDrive's hash matches |
| `skipped` | already on OneDrive with the same hash |
| `local_mismatch` | the local file no longer matches the `sha256` given; not uploaded |
| `missing` | the local file isn't there |
| `too_large` | over OneDrive's 250 GB per-file limit; not uploaded |
| `verify_failed` | uploaded, but the hashes don't match (or the file isn't there) |
| `quota_stopped` | not done because OneDrive is full or throttling |
| `dry_run` | `--dry-run`: planned only |

Other options: `--bwlimit 40M` (passed to rclone) and `--dry-run` (no hashing, no rclone).

### Exit codes

| code | meaning |
|---|---|
| 0 | every file was copied or skipped |
| 7 | OneDrive is full or throttling. Free space or wait, then run the same list again: what's done is skipped. |
| 1 | some files failed (see the manifest) |
| 2 | bad arguments |

## Pull (restore)

```bash
onedrive-rclone pull --remote onedrive: --remote-path backups/papers/2024/tax.pdf --to ./restored
```

Copies one file down into `--to` and checks its hash. It never overwrites: if the file is already in `--to`, it stops.

## Remove (quarantine, never delete)

```bash
onedrive-rclone remove --remote onedrive: --remote-path backups/papers/2024/tax.pdf [--quarantine _removed]
onedrive-rclone remove --remote onedrive: --list paths.txt
```

Moves each file to `_removed/<today>/<its path>` on the same OneDrive. Nothing is deleted. Look through the quarantine folder and empty it yourself when you're sure.

## Tests

```bash
python tests/test_onedrive_rclone.py -v
```

They run the whole tool against a fake rclone that keeps "OneDrive" in a temp folder, so no network or real rclone is needed.

## License

GPL-3.0. See `LICENSE`.
