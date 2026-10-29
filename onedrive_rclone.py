#!/usr/bin/env python3
"""onedrive-rclone: copy a list of files to OneDrive with rclone, and prove each one arrived.

  onedrive-rclone push   --remote onedrive: --dest backups --list files.jsonl [--manifest out.jsonl]
  onedrive-rclone pull   --remote onedrive: --remote-path backups/a/b.pdf --to ./restored
  onedrive-rclone remove --remote onedrive: --remote-path backups/a/b.pdf [--quarantine _removed]

push reads one JSON object per line: {"local_path": ..., "remote_path": ..., "sha256": optional},
where remote_path is relative to --dest. Each file's SHA-256 is checked first: a file that no longer
matches the one given is not uploaded, so a damaged local copy never replaces a good offsite one.
OneDrive's own hash is QuickXorHash, not MD5, so rclone computes QuickXorHash on both sides and the
tool compares them. Files already there with the same hash are skipped. The rest go up in chunks
with `rclone copy`, and each is checked afterwards. One JSON line per file goes to the manifest as
soon as its result is known.

pull copies one file down into a folder and never overwrites anything there. remove never deletes:
it moves the file into a dated quarantine folder on the same OneDrive, which you empty yourself.

Exit codes: 0 every file copied or skipped, 7 OneDrive is full or throttling (free space or wait,
then run the same list again; the manifest says which files were left), 1 some files failed,
2 bad arguments. Needs Python 3.9+ and rclone 1.60+ with a OneDrive remote. Nothing else.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import posixpath
import subprocess
import sys
import tempfile

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_CAP = 0, 1, 2, 7
HASH_TYPE = "quickxor"
FAILED = {"missing", "local_mismatch", "too_large", "verify_failed"}
MAX_FILE = 250 * 2**30  # OneDrive's per-file limit
CAP_SIGNS = ("quotaLimitReached", "insufficientStorage", "Insufficient Space", "activityLimitReached")
RCLONE = os.environ.get("RCLONE", "rclone")
COPY_FLAGS = ["--transfers", "4", "--checkers", "8", "--stats", "60s", "--stats-one-line"]


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"{now()} {msg}", file=sys.stderr, flush=True)


def _run(cmd):
    """Run rclone and capture its output (listings)."""
    return subprocess.run(cmd, capture_output=True, text=True)


def _stream(cmd):
    """Run rclone with its progress going to our stderr. Returns (exit code, last lines), so a
    full or throttling OneDrive can be told apart from other failures."""
    p = subprocess.Popen(cmd, stdout=sys.stderr, stderr=subprocess.PIPE, text=True, errors="replace")
    tail = []
    for line in p.stderr:
        sys.stderr.write(line)
        tail = (tail + [line])[-200:]
    return p.wait(), "".join(tail)


def remote_root(remote: str, path: str) -> str:
    """'onedrive' or 'onedrive:' plus 'backups/x' -> 'onedrive:backups/x'."""
    return remote.rstrip(":") + ":" + path.strip("/")


def read_list(path: str) -> list:
    items = []
    f = sys.stdin if path == "-" else open(path, encoding="utf-8")
    try:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                it = json.loads(line)
            except ValueError:
                raise SystemExit(f"--list line {n}: not JSON")
            if not it.get("local_path") or not it.get("remote_path"):
                raise SystemExit(f"--list line {n}: needs local_path and remote_path")
            it["remote_path"] = it["remote_path"].replace("\\", "/").strip("/")
            items.append(it)
    finally:
        if f is not sys.stdin:
            f.close()
    return items


def sha256_file(path: str) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(8 << 20):
            sha.update(block)
    return sha.hexdigest()


def source_root(it):
    """The local folder that local_path sits in at remote_path, so a whole chunk goes up in one
    `rclone copy`. None when the two paths don't line up; that file goes up on its own."""
    local = os.path.normpath(it["local_path"])
    rel = os.path.normpath(it["remote_path"])
    if local.endswith(os.sep + rel):
        return local[: -len(rel)]
    return None


def plan_chunks(items, max_files: int, max_bytes: float):
    """[(root or None, [item, ...]), ...]: at most max_files files or max_bytes bytes per chunk."""
    groups = {}
    for it in items:
        groups.setdefault(source_root(it), []).append(it)
    chunks = []
    for root, group in groups.items():
        chunk, size = [], 0
        for it in group:
            chunk.append(it)
            size += it.get("size") or 0
            if len(chunk) >= max_files or size >= max_bytes:
                chunks.append((root, chunk))
                chunk, size = [], 0
        if chunk:
            chunks.append((root, chunk))
    return chunks


def hashes(base: str, rels) -> dict:
    """{rel: quickxor} for the files that exist under base (a local folder or a remote)."""
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", suffix=".lst") as lst:
        lst.write("\n".join(rels) + "\n")
    try:
        out = _run([RCLONE, "hashsum", HASH_TYPE, base, "--files-from-raw", lst.name]).stdout
    finally:
        os.unlink(lst.name)
    found = {}
    for line in out.splitlines():
        if "  " in line:
            h, rel = line.split("  ", 1)
            found[rel] = h.lower()
    return found


def local_hashes(root, items) -> dict:
    """{remote_path: quickxor} of the local files, computed by rclone."""
    if root is not None:
        return hashes(root, [it["remote_path"] for it in items])
    found = {}
    for it in items:
        folder, name = os.path.split(it["local_path"])
        h = hashes(folder, [name]).get(name)
        if h:
            found[it["remote_path"]] = h
    return found


class Manifest:
    def __init__(self, path: str, dest_path: str):
        self.f = sys.stdout if path == "-" else open(path, "a", encoding="utf-8")
        self.dest_path = dest_path
        self.counts = {}

    def emit(self, it, status: str, remote_hash=None, detail=None) -> None:
        rec = {"local_path": it["local_path"], "remote_path": posixpath.join(self.dest_path, it["remote_path"]),
               "size": it.get("size"), "sha256": it.get("sha256_local"), "remote_hash": remote_hash,
               "hash_type": HASH_TYPE, "status": status, "at": now()}
        if detail:
            rec["detail"] = detail
        self.f.write(json.dumps(rec) + "\n")
        self.f.flush()
        self.counts[status] = self.counts.get(status, 0) + 1

    def close(self) -> None:
        if self.f is not sys.stdout:
            self.f.close()


def push(a) -> int:
    items = read_list(a.list)
    dest = remote_root(a.remote, a.dest)
    out = Manifest(a.manifest, a.dest.strip("/"))
    for it in items:
        if os.path.isfile(it["local_path"]):
            it["size"] = os.path.getsize(it["local_path"])
    chunks = plan_chunks(items, a.max_files, a.max_gb * 1e9)
    log(f"{len(items)} files in {len(chunks)} chunks to {dest}")
    capped = False
    for n, (root, chunk) in enumerate(chunks, 1):
        if capped:
            for it in chunk:
                out.emit(it, "quota_stopped")
            continue
        if a.dry_run:
            for it in chunk:
                out.emit(it, "dry_run")
            continue

        ready = []
        for it in chunk:
            if "size" not in it:
                out.emit(it, "missing")
                continue
            if it["size"] > MAX_FILE:
                out.emit(it, "too_large", detail="OneDrive takes files up to 250 GB")
                continue
            it["sha256_local"] = sha256_file(it["local_path"])
            if it.get("sha256") and it["sha256"].lower() != it["sha256_local"]:
                out.emit(it, "local_mismatch", detail="local file no longer matches the sha256 given")
                continue
            ready.append(it)
        if not ready:
            continue

        local = local_hashes(root, ready)
        already = hashes(dest, [it["remote_path"] for it in ready])
        todo = []
        for it in ready:
            h = local.get(it["remote_path"])
            if h and already.get(it["remote_path"]) == h:
                out.emit(it, "skipped", h)
            else:
                todo.append(it)
        log(f"chunk {n}/{len(chunks)}: {len(chunk)} files, {len(chunk) - len(ready)} not sent,"
            f" {len(ready) - len(todo)} already there, {len(todo)} to copy")
        if not todo:
            continue

        bw = ["--bwlimit", a.bwlimit] if a.bwlimit else []
        rc, tail = 0, ""
        if root is not None:
            with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", suffix=".lst") as lst:
                lst.write("\n".join(it["remote_path"] for it in todo) + "\n")
            try:
                rc, tail = _stream([RCLONE, "copy", root, dest, "--files-from-raw", lst.name, *COPY_FLAGS, *bw])
            finally:
                os.unlink(lst.name)
        else:
            for it in todo:
                rc, tail = _stream([RCLONE, "copyto", it["local_path"], f"{dest}/{it['remote_path']}",
                                    *COPY_FLAGS, *bw])
                if rc and any(s in tail for s in CAP_SIGNS):
                    break
        full = bool(rc) and any(s in tail for s in CAP_SIGNS)
        if rc:
            log(f"rclone exit {rc}{' (OneDrive full or throttling)' if full else ''}; recording only what verifies")

        got = hashes(dest, [it["remote_path"] for it in todo])
        for it in todo:
            want, remote = local.get(it["remote_path"]), got.get(it["remote_path"])
            if want and remote == want:
                out.emit(it, "copied", remote)
            elif full:
                out.emit(it, "quota_stopped")
            else:
                out.emit(it, "verify_failed", remote, f"local {HASH_TYPE} {want}, remote {remote}")
        if full:
            log("OneDrive is full or throttling: stopping; run the same list again later")
            capped = True

    out.close()
    log("done: " + ", ".join(f"{k} {v}" for k, v in sorted(out.counts.items())))
    if capped:
        return EXIT_CAP
    return EXIT_FAILED if FAILED & out.counts.keys() else EXIT_OK


def pull(a) -> int:
    name = posixpath.basename(a.remote_path)
    target = os.path.join(a.to, name)
    if os.path.exists(target):
        log(f"{target} already exists; pull never overwrites")
        return EXIT_FAILED
    os.makedirs(a.to, exist_ok=True)
    rc, _ = _stream([RCLONE, "copyto", remote_root(a.remote, a.remote_path), target])
    remote = hashes(remote_root(a.remote, posixpath.dirname(a.remote_path)), [name]).get(name)
    local = hashes(a.to, [name]).get(name) if rc == 0 and os.path.isfile(target) else None
    status = "restored" if local and local == remote else "verify_failed"
    print(json.dumps({"remote_path": a.remote_path, "local_path": target, "remote_hash": remote,
                      "local_hash": local, "hash_type": HASH_TYPE, "status": status, "at": now()}), flush=True)
    return EXIT_OK if status == "restored" else EXIT_FAILED


def remove(a) -> int:
    paths = list(a.remote_path or [])
    if a.list:
        with open(a.list, encoding="utf-8") as f:
            paths += [line.strip() for line in f if line.strip()]
    day = dt.date.today().isoformat()
    failed = 0
    for rp in paths:
        rp = rp.replace("\\", "/").strip("/")
        to = posixpath.join(a.quarantine.strip("/"), day, rp)
        rc, _ = _stream([RCLONE, "moveto", remote_root(a.remote, rp), remote_root(a.remote, to)])
        failed += rc != 0
        print(json.dumps({"remote_path": rp, "quarantine_path": to,
                          "status": "quarantined" if rc == 0 else "failed", "at": now()}), flush=True)
    return EXIT_FAILED if failed else EXIT_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="onedrive-rclone", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("push", help="copy a list of files up and verify each by QuickXorHash")
    p.add_argument("--remote", required=True, help="rclone remote name, e.g. onedrive:")
    p.add_argument("--dest", required=True, help="folder on the remote that remote_path is relative to")
    p.add_argument("--list", required=True, help="JSONL file list, or - for stdin")
    p.add_argument("--manifest", default="-", help="JSONL results, appended (default: stdout)")
    p.add_argument("--max-files", type=int, default=300, help="files per rclone run (default 300)")
    p.add_argument("--max-gb", type=float, default=20, help="GB per rclone run (default 20)")
    p.add_argument("--bwlimit", help="passed to rclone, e.g. 40M")
    p.add_argument("--dry-run", action="store_true", help="plan only: no hashing, no rclone")
    p.set_defaults(fn=push)
    p = sub.add_parser("pull", help="copy one file down into a folder, never overwriting")
    p.add_argument("--remote", required=True)
    p.add_argument("--remote-path", required=True)
    p.add_argument("--to", required=True, help="local folder to restore into")
    p.set_defaults(fn=pull)
    p = sub.add_parser("remove", help="move files into a dated quarantine folder (never deletes)")
    p.add_argument("--remote", required=True)
    p.add_argument("--remote-path", action="append", help="file to quarantine; repeat for more")
    p.add_argument("--list", help="text file of remote paths, one per line")
    p.add_argument("--quarantine", default="_removed", help="quarantine folder on the remote (default _removed)")
    p.set_defaults(fn=remove)
    a = ap.parse_args(argv)
    if a.cmd == "remove" and not (a.remote_path or a.list):
        ap.error("remove needs --remote-path or --list")
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
