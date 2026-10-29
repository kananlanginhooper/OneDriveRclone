"""onedrive-rclone end to end against a fake rclone that keeps "OneDrive" in a temp folder.

Run from the repo root:  python tests/test_onedrive_rclone.py -v
No network and no real rclone. The fake stands in SHA-1 for QuickXorHash on both sides; the tool
only compares what rclone reports, so any stable hash proves the same logic.
"""
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import onedrive_rclone as tool  # noqa: E402


class FakeRclone:
    """Understands the few rclone commands the tool sends. "od:" maps to a temp folder."""

    def __init__(self, drive, cap_after=None, corrupt=()):
        self.od = drive
        self.cap_after = cap_after  # upload this many files, then answer like a full OneDrive
        self.corrupt = set(corrupt)  # remote paths that arrive damaged
        self.uploaded = []
        self.calls = []

    def path(self, spec):
        return os.path.join(self.od, spec.split(":", 1)[1]) if spec.startswith("od:") else spec

    def upload(self, src, dst_spec):
        if self.cap_after is not None and len(self.uploaded) >= self.cap_after:
            return False
        dst = self.path(dst_spec)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        rel = os.path.relpath(dst, self.od).replace(os.sep, "/")
        if rel in self.corrupt or any(rel.endswith("/" + c) for c in self.corrupt):
            with open(dst, "ab") as f:
                f.write(b"!")
        self.uploaded.append(rel)
        return True

    def files(self, cmd):
        with open(cmd[cmd.index("--files-from-raw") + 1], encoding="utf-8") as f:
            return [line for line in f.read().splitlines() if line]

    def run(self, cmd):
        self.calls.append(cmd[1])
        out = ""
        if cmd[1] == "hashsum":
            assert cmd[2] == "quickxor", cmd
            base = self.path(cmd[3])
            for rel in self.files(cmd):
                p = os.path.join(base, rel)
                if os.path.isfile(p):
                    with open(p, "rb") as f:
                        out += f"{hashlib.sha1(f.read()).hexdigest()}  {rel}\n"
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")

    def stream(self, cmd):
        self.calls.append(cmd[1])
        full = (1, "ERROR : quotaLimitReached: Insufficient Space Available\n")
        if cmd[1] == "copy":
            for rel in self.files(cmd):
                if not self.upload(os.path.join(cmd[2], rel), f"{cmd[3]}/{rel}"):
                    return full
            return 0, ""
        if cmd[1] == "copyto":
            if cmd[2].startswith("od:"):  # pull
                shutil.copyfile(self.path(cmd[2]), cmd[3])
                return 0, ""
            return (0, "") if self.upload(cmd[2], cmd[3]) else full
        if cmd[1] == "moveto":
            src, dst = self.path(cmd[2]), self.path(cmd[3])
            if not os.path.isfile(src):
                return 3, "ERROR : not found\n"
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(src, dst)
            return 0, ""
        raise AssertionError(cmd)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="onedrive-rclone-test-")
        self.local = os.path.join(self.tmp, "local")
        self.od = os.path.join(self.tmp, "drive")
        os.makedirs(self.od)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def file(self, rel, text):
        p = os.path.join(self.local, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def item(self, rel, text, **kw):
        return dict(local_path=self.file(rel, text), remote_path=rel, **kw)

    def push(self, items, fake=None, *extra):
        fake = fake or FakeRclone(self.od)
        lst = os.path.join(self.tmp, "list.jsonl")
        with open(lst, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(it) + "\n" for it in items)
        manifest = os.path.join(self.tmp, "manifest.jsonl")
        if os.path.exists(manifest):
            os.unlink(manifest)
        rc = self.main(fake, "push", "--remote", "od:", "--dest", "base", "--list", lst, "--manifest", manifest, *extra)
        with open(manifest, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        return rc, {os.path.basename(r["local_path"]): r for r in rows}, fake

    def main(self, fake, *argv):
        with mock.patch.object(tool, "_run", fake.run), mock.patch.object(tool, "_stream", fake.stream), \
             redirect_stderr(io.StringIO()):
            return tool.main(list(argv))


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class Push(Base):
    def test_copies_and_verifies_each_file(self):
        rc, rows, fake = self.push([self.item("a/one.txt", "1"), self.item("a/b/two.txt", "2")])
        self.assertEqual(rc, tool.EXIT_OK)
        self.assertEqual({k: r["status"] for k, r in rows.items()}, {"one.txt": "copied", "two.txt": "copied"})
        self.assertEqual(rows["two.txt"]["remote_path"], "base/a/b/two.txt")
        self.assertEqual(rows["one.txt"]["sha256"], sha("1"))
        self.assertEqual(rows["one.txt"]["remote_hash"], hashlib.sha1(b"1").hexdigest())
        self.assertEqual(rows["one.txt"]["hash_type"], "quickxor")
        self.assertEqual(fake.calls.count("copy"), 1)  # one chunk, one rclone copy

    def test_damaged_local_copy_is_never_uploaded(self):
        rc, rows, fake = self.push([self.item("a/one.txt", "changed", sha256=sha("original"))])
        self.assertEqual(rc, tool.EXIT_FAILED)
        self.assertEqual(rows["one.txt"]["status"], "local_mismatch")
        self.assertEqual(fake.uploaded, [])

    def test_files_already_there_are_skipped(self):
        items = [self.item("a/one.txt", "1"), self.item("a/two.txt", "2")]
        self.push(items[:1])
        rc, rows, fake = self.push(items)
        self.assertEqual({k: r["status"] for k, r in rows.items()}, {"one.txt": "skipped", "two.txt": "copied"})
        self.assertEqual(fake.uploaded, ["base/a/two.txt"])

    def test_a_bad_upload_is_not_reported_as_copied(self):
        rc, rows, _ = self.push([self.item("a/one.txt", "1"), self.item("a/two.txt", "2")],
                                FakeRclone(self.od, corrupt={"a/two.txt"}))
        self.assertEqual(rc, tool.EXIT_FAILED)
        self.assertEqual(rows["one.txt"]["status"], "copied")
        self.assertEqual(rows["two.txt"]["status"], "verify_failed")

    def test_full_onedrive_stops_the_run_with_exit_7(self):
        items = [self.item(f"a/f{i}.txt", str(i)) for i in range(5)]
        rc, rows, _ = self.push(items, FakeRclone(self.od, cap_after=2), "--max-files", "2")
        self.assertEqual(rc, tool.EXIT_CAP)
        statuses = sorted(r["status"] for r in rows.values())
        self.assertEqual(statuses, ["copied", "copied", "quota_stopped", "quota_stopped", "quota_stopped"])
        # the same list again tomorrow picks up where it stopped
        rc, rows, fake = self.push(items)
        self.assertEqual(rc, tool.EXIT_OK)
        self.assertEqual(sorted(r["status"] for r in rows.values()), ["copied"] * 3 + ["skipped"] * 2)

    def test_other_rclone_errors_are_failures_not_a_full_drive(self):
        fake = FakeRclone(self.od)
        fake.stream = lambda cmd: (1, "ERROR : something else\n")
        rc, rows, _ = self.push([self.item("a/one.txt", "1")], fake)
        self.assertEqual(rc, tool.EXIT_FAILED)
        self.assertEqual(rows["one.txt"]["status"], "verify_failed")

    def test_files_over_the_onedrive_limit_are_not_sent(self):
        with mock.patch.object(tool, "MAX_FILE", 3):
            rc, rows, fake = self.push([self.item("a/big.txt", "1234"), self.item("a/ok.txt", "1")])
        self.assertEqual(rows["big.txt"]["status"], "too_large")
        self.assertEqual(rows["ok.txt"]["status"], "copied")
        self.assertEqual(fake.uploaded, ["base/a/ok.txt"])

    def test_missing_file_is_reported(self):
        rc, rows, _ = self.push([dict(local_path=os.path.join(self.local, "gone.txt"), remote_path="gone.txt")])
        self.assertEqual(rc, tool.EXIT_FAILED)
        self.assertEqual(rows["gone.txt"]["status"], "missing")

    def test_paths_that_dont_line_up_go_up_one_by_one(self):
        it = dict(local_path=self.file("x/local-name.txt", "1"), remote_path="elsewhere/remote-name.txt")
        rc, rows, fake = self.push([it])
        self.assertEqual(rows["local-name.txt"]["status"], "copied")
        self.assertEqual(fake.uploaded, ["base/elsewhere/remote-name.txt"])
        self.assertIn("copyto", fake.calls)

    def test_dry_run_touches_nothing(self):
        rc, rows, fake = self.push([self.item("a/one.txt", "1")], None, "--dry-run")
        self.assertEqual(rows["one.txt"]["status"], "dry_run")
        self.assertEqual(fake.calls, [])

    def test_chunks_respect_the_file_limit(self):
        items = [self.item(f"a/f{i}.txt", str(i)) for i in range(5)]
        _, _, fake = self.push(items, None, "--max-files", "2")
        self.assertEqual(fake.calls.count("copy"), 3)


class PullAndRemove(Base):
    def test_pull_restores_and_never_overwrites(self):
        self.push([self.item("a/one.txt", "1")])
        out = os.path.join(self.tmp, "restored")
        fake = FakeRclone(self.od)
        with redirect_stdout(io.StringIO()) as so:
            rc = self.main(fake, "pull", "--remote", "od:", "--remote-path", "base/a/one.txt", "--to", out)
        self.assertEqual(rc, tool.EXIT_OK)
        self.assertEqual(json.loads(so.getvalue())["status"], "restored")
        with redirect_stdout(io.StringIO()):
            rc = self.main(fake, "pull", "--remote", "od:", "--remote-path", "base/a/one.txt", "--to", out)
        self.assertEqual(rc, tool.EXIT_FAILED)

    def test_remove_moves_to_quarantine_and_deletes_nothing(self):
        self.push([self.item("a/one.txt", "1")])
        fake = FakeRclone(self.od)
        with redirect_stdout(io.StringIO()) as so:
            rc = self.main(fake, "remove", "--remote", "od:", "--remote-path", "base/a/one.txt")
        self.assertEqual(rc, tool.EXIT_OK)
        row = json.loads(so.getvalue())
        self.assertEqual(row["status"], "quarantined")
        self.assertFalse(os.path.exists(os.path.join(self.od, "base", "a", "one.txt")))
        self.assertTrue(os.path.isfile(os.path.join(self.od, *row["quarantine_path"].split("/"))))
        self.assertNotIn("delete", fake.calls)


if __name__ == "__main__":
    unittest.main()
