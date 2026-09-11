import hashlib
import io
import os
from pathlib import Path
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import gopro


class Response(io.BytesIO):
    def __init__(self, body, length=None):
        super().__init__(body)
        self.headers = {} if length is None else {"Content-Length": str(length)}
        self.requests = []

    def read(self, size=-1):
        if size < 0:
            raise AssertionError("Unbounded read")
        self.requests.append(size)
        return super().read(size)


class CameraLimits(unittest.TestCase):
    def test_overflow_after_multiple_chunks_never_reaches_sink(self):
        limit = 2 * gopro.IO_CHUNK
        response = Response(b"x" * (limit + 100))
        written = bytearray()
        with self.assertRaises(gopro.SizeLimitError):
            gopro._copy_bounded(response, written.extend, limit)
        self.assertEqual(len(written), limit)
        self.assertEqual(response.tell(), limit + 1)

    def test_bounded_http_with_missing_or_dishonest_length(self):
        for length in (None, 1, 9):
            with self.subTest(length=length):
                response = Response(b"x" * 100, length)
                with patch.object(gopro.urllib.request, "urlopen", return_value=response):
                    with self.assertRaises(gopro.SizeLimitError):
                        gopro.http_bytes("camera", "manifest", limit=8)
                self.assertLessEqual(sum(response.requests), 9)

    def test_exact_limit_and_invalid_length(self):
        with patch.object(gopro.urllib.request, "urlopen", return_value=Response(b"12345678", 8)):
            self.assertEqual(gopro.http_bytes("camera", "state", limit=8), b"12345678")
        for length in ("-1", "garbage"):
            with patch.object(gopro.urllib.request, "urlopen", return_value=Response(b"", length)):
                with self.assertRaises(ValueError):
                    gopro.http_bytes("camera", "state")

    def test_manifest_ceiling_applies_before_json_parsing(self):
        with patch.object(gopro, "MAX_MANIFEST_BYTES", 8), patch.object(
                gopro.urllib.request, "urlopen", return_value=Response(b" " * 9)):
            with self.assertRaises(gopro.SizeLimitError):
                gopro.fetch_manifest("camera")

    def test_manifest_paths_sizes_and_count(self):
        good = {"n": "GX010001.MP4", "s": "4", "cre": "1720000000"}
        for field, value in (("n", "../escape"), ("n", "/tmp/escape"),
                             ("n", "a?b"), ("s", "-1"),
                             ("s", str(gopro.MAX_MEDIA_BYTES + 1))):
            entry = {**good, field: value}
            with self.subTest(field=field, value=value), patch.object(
                    gopro, "http_json", return_value={"media": [{"d": "100GOPRO", "fs": [entry]}]}):
                with self.assertRaises(ValueError):
                    gopro.fetch_manifest("camera")
        with patch.object(gopro, "http_json", return_value={
                "media": [{"d": "../escape", "fs": [good]}]}):
            with self.assertRaises(ValueError):
                gopro.fetch_manifest("camera")
        with patch.object(gopro, "MAX_MANIFEST_FILES", 1), patch.object(
                gopro, "http_json", return_value={"media": [{"d": "100GOPRO", "fs": [good, good]}]}):
            with self.assertRaises(gopro.SizeLimitError):
                gopro.fetch_manifest("camera")


class SafeFiles(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.thumbs = self.root / "cache" / "thumbs"
        for name, value in {
            "STATE_DIR": self.state, "THUMB_DIR": self.thumbs,
            "JOB_PATH": self.state / "job.json", "LOCK_PATH": self.state / "job.lock",
            "CANCEL_PATH": self.state / "cancel",
        }.items():
            p = patch.object(gopro, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.victim = self.root / "victim"
        self.victim.write_bytes(b"keep me")

    def test_atomic_write_ignores_old_predictable_temp(self):
        self.state.mkdir()
        (self.state / "job.json.tmp").symlink_to(self.victim)
        gopro.write_job({"status": "done"})
        self.assertEqual(gopro.read_job(), {"status": "done"})
        self.assertEqual(self.victim.read_bytes(), b"keep me")
        self.assertEqual(stat.S_IMODE(gopro.JOB_PATH.stat().st_mode), 0o600)
        self.assertEqual(list(self.state.glob(".gopro-*.tmp")), [])

    def test_existing_symlink_hardlink_fifo_and_writable_files_rejected(self):
        self.state.mkdir()
        for kind in ("symlink", "hardlink", "fifo", "writable"):
            with self.subTest(kind=kind):
                target = gopro.JOB_PATH
                if kind == "symlink":
                    target.symlink_to(self.victim)
                elif kind == "hardlink":
                    os.link(self.victim, target)
                elif kind == "fifo":
                    os.mkfifo(target)
                else:
                    target.write_bytes(b"{}")
                    target.chmod(0o666)
                self.assertIsNone(gopro.read_job())
                with self.assertRaises(OSError):
                    gopro.write_job({"status": "done"})
                target.unlink()
        self.assertEqual(self.victim.read_bytes(), b"keep me")

    def test_wrong_owner_rejected(self):
        info = self.victim.stat()
        with patch.object(gopro.os, "getuid", return_value=info.st_uid + 1):
            with self.assertRaises(PermissionError):
                gopro._check_file(info)

    def test_parent_symlink_and_shared_directory_rejected(self):
        self.state.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            gopro.write_job({})
        self.assertFalse((self.root / "job.json").exists())
        self.state.unlink()
        self.state.mkdir()
        self.state.chmod(0o777)
        try:
            with self.assertRaises(PermissionError):
                gopro.write_job({})
        finally:
            self.state.chmod(0o700)

    def test_failure_preserves_target_and_cleans_temp(self):
        target = self.root / "media.mp4"
        target.write_bytes(b"original")
        with self.assertRaises(RuntimeError):
            with gopro._atomic_file(target) as out:
                out.write(b"partial")
                raise RuntimeError("connection lost")
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(list(self.root.glob(".gopro-*.tmp")), [])

    def test_destination_swap_cannot_redirect_write(self):
        target = self.root / "media.mp4"
        with self.assertRaises(OSError):
            with gopro._atomic_file(target) as out:
                out.write(b"new")
                target.symlink_to(self.victim)
        self.assertEqual(self.victim.read_bytes(), b"keep me")
        self.assertEqual(list(self.root.glob(".gopro-*.tmp")), [])

    def test_parent_swap_keeps_write_in_opened_directory(self):
        parent = self.root / "archive"
        parent.mkdir()
        moved = self.root / "original-archive"
        with gopro._atomic_file(parent / "movie.mp4") as out:
            out.write(b"media")
            parent.rename(moved)
            parent.symlink_to(self.root, target_is_directory=True)
        self.assertEqual((moved / "movie.mp4").read_bytes(), b"media")
        self.assertFalse((self.root / "movie.mp4").exists())

    def test_exclusive_temp_creation_rejects_collision(self):
        temp = self.root / ".gopro-collision.tmp"
        temp.symlink_to(self.victim)
        with patch.object(gopro.secrets, "token_hex", return_value="collision"):
            with self.assertRaises(FileExistsError):
                with gopro._atomic_file(self.root / "target"):
                    self.fail("Opened an existing temporary file")
        self.assertEqual(self.victim.read_bytes(), b"keep me")

    def test_bounded_job_read_and_write(self):
        self.state.mkdir()
        gopro.JOB_PATH.write_bytes(b" " * 9)
        with patch.object(gopro, "MAX_JOB_BYTES", 8):
            self.assertIsNone(gopro.read_job())
            with self.assertRaises(gopro.SizeLimitError):
                gopro.write_job({"long": "value"})
        gopro.JOB_PATH.write_text("[]")
        self.assertIsNone(gopro.read_job())

    def test_lock_cancel_log_and_destination_probe(self):
        self.state.mkdir()
        for target, action in (
            (gopro.LOCK_PATH, gopro._acquire_lock),
            (gopro.CANCEL_PATH, lambda: gopro.do_cancel(None)),
        ):
            target.symlink_to(self.victim)
            with self.assertRaises(OSError):
                action()
            target.unlink()
        log = self.state / "sync.log"
        log.symlink_to(self.victim)
        with self.assertRaises(OSError):
            with gopro._open_file(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT):
                self.fail("Opened a symlink")
        (self.root / ".omarchy-gopro-write-test").symlink_to(self.victim)
        self.assertEqual(gopro.check_destination(self.root), (True, ""))
        lock = gopro._acquire_lock()
        self.assertIsNotNone(lock)
        try:
            self.assertIsNone(gopro._acquire_lock())
        finally:
            lock.close()
        self.assertEqual(self.victim.read_bytes(), b"keep me")

    def test_local_symlink_never_counts_as_verified(self):
        row = {"day": "2026-09-11", "name": "photo.jpg", "sizeBytes": 7}
        folder = self.root / row["day"]
        folder.mkdir()
        (folder / row["name"]).symlink_to(self.victim)
        self.assertEqual(gopro.verified_removable([row], self.root), [])

    def test_download_overflow_short_read_cancel_and_success(self):
        row = {"path": "100GOPRO/movie.mp4", "sizeBytes": 8}
        target = self.root / "day" / "movie.mp4"
        target.parent.mkdir()
        target.write_bytes(b"original")
        for body, cancel, error in ((b"x" * 100, False, gopro.SizeLimitError),
                                    (b"short", False, OSError),
                                    (b"12345678", True, KeyboardInterrupt),
                                    (b"12345678", False, None)):
            with self.subTest(body=body, cancel=cancel), patch.object(
                    gopro.urllib.request, "urlopen", return_value=Response(body)):
                def download():
                    return gopro._download("camera", row, target, gopro.Progress(8, 1),
                                           lambda _: None, lambda: cancel)
                if error:
                    with self.assertRaises(error):
                        download()
                    self.assertEqual(target.read_bytes(), b"original")
                else:
                    self.assertEqual(download(), 8)
                    self.assertEqual(target.read_bytes(), body)
                self.assertEqual(list(target.parent.glob(".gopro-*.tmp")), [])

    def test_sync_copies_media_and_publishes_completed_job(self):
        row = {"path": "100GOPRO/movie.mp4", "dir": "100GOPRO", "name": "movie.mp4",
               "day": "2026-09-11", "createdTs": 1, "sizeBytes": 8, "kind": "video"}
        destination = self.root / "archive"
        with patch.object(gopro, "detect", return_value={"connected": True, "ip": "camera"}), \
                patch.object(gopro, "fetch_manifest", return_value=[row]), \
                patch.object(gopro, "http_ok", return_value=(True, 200, "")), \
                patch.object(gopro.urllib.request, "urlopen", return_value=Response(b"12345678")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(gopro.run_sync(dest_root=destination, everything=True), 0)
        self.assertEqual((destination / row["day"] / row["name"]).read_bytes(), b"12345678")
        job = gopro.read_job()
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["doneBytes"], 8)
        self.assertEqual(job["doneFiles"], 1)
        self.assertEqual(list(self.root.rglob(".gopro-*.tmp")), [])

    def test_thumbnails_bounded_and_cached_files_checked(self):
        camera_path = "100GOPRO/photo.jpg"
        target = self.thumbs / (hashlib.sha256(camera_path.encode()).hexdigest() + ".jpg")
        self.thumbs.mkdir(parents=True)
        (self.thumbs / "100GOPRO_photo.jpg.jpg.part").symlink_to(self.victim)
        with patch.object(gopro, "MAX_THUMB_BYTES", 8), patch.object(
                gopro.urllib.request, "urlopen", return_value=Response(b"x" * 100)):
            with self.assertRaises(gopro.SizeLimitError):
                gopro._thumbnail("camera", camera_path, camera_path)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.thumbs.glob(".gopro-*.tmp")), [])
        with patch.object(gopro.urllib.request, "urlopen", return_value=Response(b"jpeg")):
            self.assertEqual(gopro._thumbnail("camera", camera_path, camera_path), target)
        with patch.object(gopro.urllib.request, "urlopen") as request:
            self.assertEqual(gopro._thumbnail("camera", camera_path, camera_path), target)
            request.assert_not_called()
        target.unlink()
        target.symlink_to(self.victim)
        with self.assertRaises(OSError):
            gopro._thumbnail("camera", camera_path, camera_path)
        self.assertEqual(self.victim.read_bytes(), b"keep me")

    def test_cache_budget_evicts_oldest(self):
        self.thumbs.mkdir(parents=True)
        old = self.thumbs / "old.jpg"
        old.write_bytes(b"12345678")
        with patch.object(gopro, "MAX_THUMB_BYTES", 8), patch.object(
                gopro, "MAX_CACHE_BYTES", 8), patch.object(
                gopro.urllib.request, "urlopen", return_value=Response(b"12345678")):
            target = gopro._thumbnail("camera", "100GOPRO/new.jpg", "new")
        self.assertFalse(old.exists())
        self.assertEqual(target.read_bytes(), b"12345678")


if __name__ == "__main__":
    unittest.main()
