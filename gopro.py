#!/usr/bin/env python3
"""Ingest media from a USB-connected GoPro over its HTTP API.

A GoPro whose USB Connection setting is "GoPro Connect" enumerates as a
CDC-Ethernet adapter rather than a storage device, so no file manager will
ever show it. It does run an HTTP server, and this drives it.

Usable standalone:

    gopro.py status            # camera + card + what is already on disk
    gopro.py sync --all        # detached background copy, progress in job.json
    gopro.py job               # current job state
    gopro.py verify            # filesystem vs manifest, the source of truth
"""

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path

PORT = 8080
SYS_USB = Path("/sys/bus/usb/devices")
GOPRO_VENDOR = "2672"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omarchy-gopro"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "omarchy-gopro"
THUMB_DIR = CACHE_DIR / "thumbs"
JOB_PATH = STATE_DIR / "job.json"
LOCK_PATH = STATE_DIR / "job.lock"
CANCEL_PATH = STATE_DIR / "cancel"
DEFAULT_DEST = Path(os.environ.get("OMARCHY_GOPRO_DEST", Path.home() / "Pictures/GoPro"))

VIDEO_EXT = {".mp4", ".lrv", ".thm", ".360"}
PHOTO_EXT = {".jpg", ".jpeg", ".gpr", ".raw"}

# No-data timeout on a socket, not a total-transfer deadline: a 4 GB file at
# 8 MB/s legitimately takes eight minutes and must not be killed for it.
READ_TIMEOUT = 60
RETRIES = 3
# Local filesystem problems that retrying cannot fix. Grinding through 47
# files to report the same permission error 47 times helps nobody, so these
# abort the whole job at the first occurrence.
FATAL_ERRNOS = {errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOSPC,
                errno.EDQUOT, errno.ENOENT}
# Re-read the manifest this often mid-sync. The card can be swapped or
# formatted underneath a running job; that has really happened.
MANIFEST_RECHECK_SEC = 120


# --------------------------------------------------------------- camera discovery

def _usb_devices():
    """Every GoPro currently on the USB bus, as (serial, product) pairs."""
    found = []
    if not SYS_USB.is_dir():
        return found
    for dev in sorted(SYS_USB.iterdir()):
        try:
            if (dev / "idVendor").read_text().strip() != GOPRO_VENDOR:
                continue
            serial = (dev / "serial").read_text().strip()
            product = (dev / "product").read_text().strip()
        except OSError:
            continue
        if serial:
            found.append((serial, product))
    return found


def ip_from_serial(serial):
    """GoPro derives its wired IP from the serial: 172.2X.1YZ.51.

    X, Y and Z are the last three digits of the serial number. Never hardcode
    the result -- it differs per camera, and the host's own address is not a
    reliable way back to it either.
    """
    digits = [c for c in str(serial) if c.isdigit()]
    if len(digits) < 3:
        return None
    x, y, z = digits[-3:]
    return "172.2{}.1{}{}.51".format(x, y, z)


def _iface_candidates():
    """Fallback: any CDC-Ethernet link in a 172.2x.1yz.0/24 gets .51 tried."""
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True,
                             text=True, timeout=4).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    ips = []
    for line in out.splitlines():
        m = re.search(r"inet (172\.2\d+\.1\d+)\.(\d+)/", line)
        if m:
            ips.append(m.group(1) + ".51")
    return ips


def probe(ip, timeout=4):
    try:
        http_json(ip, "gopro/camera/state", timeout=timeout)
        return True
    except Exception:
        return False


def detect():
    """Locate a reachable camera, or explain precisely why there isn't one."""
    devices = _usb_devices()
    if not devices:
        return {"connected": False, "reason": "no-usb",
                "message": "No GoPro on USB."}

    serial, product = devices[0]
    tried = []
    derived = ip_from_serial(serial)
    if derived:
        tried.append(derived)
    for ip in _iface_candidates():
        if ip not in tried:
            tried.append(ip)

    for ip in tried:
        if probe(ip):
            return {"connected": True, "ip": ip, "serial": serial, "model": product}

    # Plugged in, but not answering: almost always the USB mode setting.
    return {
        "connected": False,
        "reason": "no-http",
        "serial": serial,
        "model": product,
        "triedIps": tried,
        "message": ("%s is plugged in but its HTTP server is not answering. "
                    "Set Preferences > Connections > USB Connection to "
                    "GoPro Connect (MTP mode disables this API)." % product),
    }


# ------------------------------------------------------------------------ http

def _url(ip, path):
    return "http://{}:{}/{}".format(ip, PORT, path)


def http_bytes(ip, path, timeout=READ_TIMEOUT):
    req = urllib.request.Request(_url(ip, path), headers={"Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_json(ip, path, timeout=15):
    return json.loads(http_bytes(ip, path, timeout=timeout).decode("utf-8", "replace"))


def http_ok(ip, path, timeout=15):
    """Fire an endpoint for effect. Returns (ok, http_status_or_None, detail)."""
    try:
        req = urllib.request.Request(_url(ip, path))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, resp.status, resp.read(400).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return False, e.code, e.read(400).decode("utf-8", "replace")
    except Exception as e:
        return False, None, str(e)


# -------------------------------------------------------------------- manifest

def human_bytes(value):
    value = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return "{:.0f} {}".format(value, unit) if unit == "B" or value >= 100 \
                else "{:.1f} {}".format(value, unit)
        value /= 1000
    return "{:.1f} TB".format(value)


def media_kind(name):
    ext = os.path.splitext(str(name))[1].lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in PHOTO_EXT:
        return "photo"
    return "other"


def fetch_manifest(ip):
    """Flatten /gopro/media/list into plain rows.

    Note the directory comes from each group's `d` field. Cameras roll over to
    101GOPRO, 102GOPRO and so on once a folder fills; hardcoding 100GOPRO
    silently loses everything past the first thousand files.
    """
    raw = http_json(ip, "gopro/media/list", timeout=20)
    rows = []
    for group in raw.get("media", []):
        directory = str(group.get("d") or "100GOPRO")
        for entry in group.get("fs", []):
            name = str(entry.get("n") or "")
            if not name:
                continue
            # Every numeric field arrives as a string. Cast or suffer.
            created = int(entry.get("cre") or entry.get("mod") or 0)
            size = int(entry.get("s") or 0)
            rows.append({
                "name": name,
                "dir": directory,
                "path": "{}/{}".format(directory, name),
                "createdTs": created,
                "sizeBytes": size,
                "kind": media_kind(name),
                "day": datetime.fromtimestamp(created).strftime("%Y-%m-%d") if created else "unknown",
            })
    rows.sort(key=lambda r: (r["createdTs"], r["name"]))
    return rows


def fingerprint(rows):
    """Identity of the card's contents, so a swap mid-job is detectable."""
    h = hashlib.sha256()
    for r in sorted(rows, key=lambda r: r["path"]):
        h.update("{}:{}:{}\n".format(r["path"], r["sizeBytes"], r["createdTs"]).encode())
    return h.hexdigest()[:16]


def dest_for(row, dest_root):
    return Path(dest_root) / row["day"] / row["name"]


def local_state(rows, dest_root):
    """Annotate each row with what is actually on disk right now.

    A file counts as present only when it exists AND its size matches the
    manifest exactly. That single rule is the whole idempotency story: it makes
    re-running free, self-healing after an interruption, and immune to a
    truncated file that merely looks complete.
    """
    for r in rows:
        target = dest_for(r, dest_root)
        try:
            actual = target.stat().st_size
        except OSError:
            r["synced"] = False
            r["localBytes"] = 0
            continue
        r["localBytes"] = actual
        r["synced"] = actual == r["sizeBytes"]
        if not r["synced"]:
            r["localPartial"] = True
    return rows


def group_days(rows):
    days = {}
    for r in rows:
        d = days.setdefault(r["day"], {
            "day": r["day"], "count": 0, "bytes": 0,
            "syncedCount": 0, "syncedBytes": 0,
            "photos": 0, "videos": 0,
        })
        d["count"] += 1
        d["bytes"] += r["sizeBytes"]
        if r["kind"] == "photo":
            d["photos"] += 1
        elif r["kind"] == "video":
            d["videos"] += 1
        if r.get("synced"):
            d["syncedCount"] += 1
            d["syncedBytes"] += r["sizeBytes"]
    out = sorted(days.values(), key=lambda d: d["day"], reverse=True)
    for d in out:
        d["complete"] = d["syncedCount"] == d["count"]
        d["pendingCount"] = d["count"] - d["syncedCount"]
        d["pendingBytes"] = d["bytes"] - d["syncedBytes"]
    return out


# ------------------------------------------------------------------------- job

def read_job():
    try:
        with JOB_PATH.open() as fh:
            job = json.load(fh)
    except (OSError, ValueError):
        return None
    # A job whose process died without writing a terminal status would
    # otherwise show as running forever.
    if job.get("status") == "running" and not _pid_alive(job.get("pid")):
        job["status"] = "failed"
        job["error"] = job.get("error") or "Sync process exited unexpectedly."
    return job


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError) as e:
        return isinstance(e, OSError) and e.errno == errno.EPERM
    return True


def write_job(job):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = JOB_PATH.with_suffix(".json.tmp")
    with tmp.open("w") as fh:
        json.dump(job, fh)
    os.replace(tmp, JOB_PATH)


class Progress:
    """Byte-rate tracker over a trailing window.

    Rate must come from bytes, never from file counts: a card holding 5 MB
    photos next to 4 GB videos makes a count-based ETA meaningless.
    """

    WINDOW = 30.0

    def __init__(self, total_bytes, total_files):
        self.samples = deque()
        self.started = time.time()
        self.total_bytes = total_bytes
        self.total_files = total_files
        self.done_bytes = 0
        self.done_files = 0

    def note(self, done_bytes):
        now = time.time()
        self.samples.append((now, done_bytes))
        while len(self.samples) > 2 and now - self.samples[0][0] > self.WINDOW:
            self.samples.popleft()

    def rate(self):
        if len(self.samples) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
        span = t1 - t0
        return (b1 - b0) / span if span > 0.5 else 0.0

    def eta(self, current_extra=0):
        rate = self.rate()
        if rate <= 0:
            return None
        remaining = self.total_bytes - (self.done_bytes + current_extra)
        return max(0, int(remaining / rate))


# ------------------------------------------------------------------------ sync

def check_destination(dest_root):
    """Can we actually write here? Returns (ok, reason)."""
    path = Path(dest_root)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, "Cannot create {}: {}.".format(path, e.strerror or e)
    if not os.access(path, os.W_OK | os.X_OK):
        return False, "Cannot write to {}: permission denied.".format(path)
    probe = path / ".omarchy-gopro-write-test"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as e:
        return False, "Cannot write to {}: {}.".format(path, e.strerror or e)
    return True, ""


def free_space(dest_root):
    try:
        return shutil.disk_usage(str(dest_root)).free
    except OSError:
        return None


def _acquire_lock():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fh = LOCK_PATH.open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return fh


def _download(ip, row, target, prog, on_tick, cancelled):
    """Stream one file to <name>.part and rename only once it is whole.

    An interrupted transfer left in place under its final name looks complete
    to any later existence check, and silently corrupts the archive.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    part.unlink(missing_ok=True)

    req = urllib.request.Request(_url(ip, "videos/DCIM/{}".format(row["path"])))
    got = 0
    last_tick = 0.0
    with urllib.request.urlopen(req, timeout=READ_TIMEOUT) as resp, part.open("wb") as out:
        while True:
            if cancelled():
                part.unlink(missing_ok=True)
                raise KeyboardInterrupt
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            prog.note(prog.done_bytes + got)
            now = time.time()
            if now - last_tick > 1.0:
                last_tick = now
                on_tick(got)

    if got != row["sizeBytes"]:
        part.unlink(missing_ok=True)
        raise IOError("size mismatch: got {} bytes, manifest says {}".format(got, row["sizeBytes"]))
    os.replace(part, target)
    return got


def run_sync(days=None, names=None, dest_root=DEFAULT_DEST, everything=False):
    lock = _acquire_lock()
    if lock is None:
        print("A sync is already running.", file=sys.stderr)
        return 1

    CANCEL_PATH.unlink(missing_ok=True)
    cancelled = lambda: CANCEL_PATH.exists()

    cam = detect()
    if not cam.get("connected"):
        write_job({"schema": 1, "status": "failed", "error": cam.get("message", "No camera."),
                   "updatedAt": time.time()})
        print(cam.get("message"), file=sys.stderr)
        return 1

    ip = cam["ip"]

    # The destination is a precondition, not a per-file concern: an
    # unwritable path or an unmounted drive should fail once, clearly,
    # before a single byte moves.
    ok, reason = check_destination(dest_root)
    if not ok:
        write_job({"schema": 1, "status": "failed", "error": reason,
                   "dest": str(dest_root), "updatedAt": time.time()})
        print(reason, file=sys.stderr)
        return 1

    rows = local_state(fetch_manifest(ip), dest_root)
    fp = fingerprint(rows)

    def wanted(r):
        if r.get("synced"):
            return False
        if everything:
            return True
        if names:
            return r["name"] in names
        if days:
            return r["day"] in days
        return True

    queue = [r for r in rows if wanted(r)]
    prog = Progress(sum(r["sizeBytes"] for r in queue), len(queue))

    job = {
        "schema": 1,
        "status": "running",
        "pid": os.getpid(),
        "startedAt": time.time(),
        "updatedAt": time.time(),
        "dest": str(dest_root),
        "camera": cam.get("model", "GoPro"),
        "manifestFingerprint": fp,
        "manifestChanged": False,
        "totalFiles": prog.total_files,
        "totalBytes": prog.total_bytes,
        "doneFiles": 0,
        "doneBytes": 0,
        "currentFile": "",
        "currentBytes": 0,
        "currentTotal": 0,
        "rateBps": 0,
        "etaSec": None,
        "failures": [],
        "vanished": [],
        "warnings": [],
    }

    def flush(extra=0):
        job["updatedAt"] = time.time()
        job["doneBytes"] = prog.done_bytes + extra
        job["doneFiles"] = prog.done_files
        job["currentBytes"] = extra
        job["rateBps"] = prog.rate()
        job["etaSec"] = prog.eta(extra)
        write_job(job)

    free = free_space(dest_root)
    if free is not None and free < prog.total_bytes:
        job["warnings"].append(
            "This needs {} but only {} is free on the destination. The copy "
            "will stop when the disk fills.".format(
                human_bytes(prog.total_bytes), human_bytes(free)))

    flush()
    if not queue:
        job["status"] = "done"
        flush()
        print("Nothing to copy: everything on the card is already on disk.")
        return 0

    # Best-effort throughput hint; harmless where the firmware ignores it.
    http_ok(ip, "gopro/media/turbo_transfer?p=1", timeout=5)
    last_check = time.time()
    last_alive = time.time()

    try:
        for row in queue:
            if cancelled():
                job["status"] = "canceled"
                flush()
                return 0

            now = time.time()
            if now - last_alive > 30:
                last_alive = now
                http_ok(ip, "gopro/camera/keep_alive", timeout=5)

            # The card really can change underneath a running job. Re-read the
            # manifest periodically and stop trusting the plan if it has.
            if now - last_check > MANIFEST_RECHECK_SEC:
                last_check = now
                try:
                    fresh = fetch_manifest(ip)
                    if fingerprint(fresh) != fp:
                        job["manifestChanged"] = True
                        job["warnings"].append(
                            "The card's contents changed during this sync; "
                            "remaining files were re-checked against the new manifest.")
                        fp = fingerprint(fresh)
                        present = {r["path"] for r in fresh}
                        queue = [q for q in queue if q["path"] in present]
                except Exception:
                    pass

            target = dest_for(row, dest_root)
            job["currentFile"] = row["name"]
            job["currentTotal"] = row["sizeBytes"]
            flush()

            ok = False
            for attempt in range(1, RETRIES + 1):
                try:
                    _download(ip, row, target, prog, lambda got: flush(got), cancelled)
                    ok = True
                    break
                except KeyboardInterrupt:
                    job["status"] = "canceled"
                    flush()
                    return 0
                except urllib.error.HTTPError as e:
                    # A 403 here means "no such file", not a broken media
                    # server. Confirm against a fresh manifest before saying
                    # anything about the camera's health.
                    if e.code in (403, 404):
                        try:
                            fresh = fetch_manifest(ip)
                        except Exception:
                            fresh = None
                        if fresh is not None and row["path"] not in {r["path"] for r in fresh}:
                            job["vanished"].append(row["name"])
                            job["warnings"].append(
                                "%s is no longer on the card and was never copied." % row["name"])
                            break
                    if attempt == RETRIES:
                        job["failures"].append({"name": row["name"],
                                                "reason": "HTTP %s" % e.code})
                    else:
                        time.sleep(2 * attempt)
                except OSError as e:
                    if getattr(e, "errno", None) in FATAL_ERRNOS:
                        job["status"] = "failed"
                        job["error"] = "Cannot write to {}: {}.".format(
                            dest_root, e.strerror or e)
                        job["currentFile"] = ""
                        flush()
                        print(job["error"], file=sys.stderr)
                        return 1
                    if attempt == RETRIES:
                        job["failures"].append({"name": row["name"], "reason": str(e)})
                    else:
                        time.sleep(2 * attempt)
                except Exception as e:
                    if attempt == RETRIES:
                        job["failures"].append({"name": row["name"], "reason": str(e)})
                    else:
                        time.sleep(2 * attempt)

            if ok:
                prog.done_files += 1
                prog.done_bytes += row["sizeBytes"]
            job["currentBytes"] = 0
            flush()

        job["status"] = "done" if not job["failures"] else "failed"
        job["currentFile"] = ""
        flush()
    finally:
        http_ok(ip, "gopro/media/turbo_transfer?p=0", timeout=5)
        CANCEL_PATH.unlink(missing_ok=True)

    print("Copied {} of {} files to {}".format(prog.done_files, prog.total_files, dest_root))
    for f in job["failures"]:
        print("FAILED {}: {}".format(f["name"], f["reason"]), file=sys.stderr)
    return 0 if not job["failures"] else 1


def spawn_sync(args):
    """Re-exec ourselves detached: a 19 GB copy outlives any UI that started it."""
    if read_job() and read_job().get("status") == "running":
        print("A sync is already running.", file=sys.stderr)
        return 1
    cmd = [sys.executable, os.path.abspath(__file__),
           "--dest", str(args.dest), "sync", "--foreground"]
    if args.all:
        cmd.append("--all")
    for d in args.day or []:
        cmd += ["--day", d]
    for n in args.name or []:
        cmd += ["--name", n]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log = (STATE_DIR / "sync.log").open("a")
    subprocess.Popen(cmd, stdout=log, stderr=log, start_new_session=True,
                     stdin=subprocess.DEVNULL)
    print(json.dumps({"started": True, "job": str(JOB_PATH)}))
    return 0


# ------------------------------------------------------------- destructive ops

def verified_removable(rows, dest_root, days=None, names=None):
    """Only files proven byte-for-byte present on disk may ever be deleted."""
    out = []
    for r in local_state(rows, dest_root):
        if not r.get("synced"):
            continue
        if names and r["name"] not in names:
            continue
        if days and r["day"] not in days:
            continue
        out.append(r)
    return out


def do_delete(args):
    cam = detect()
    if not cam.get("connected"):
        print(cam.get("message"), file=sys.stderr)
        return 1
    ip = cam["ip"]
    rows = fetch_manifest(ip)
    targets = verified_removable(rows, args.dest, args.day or None, args.name or None)
    unsafe = [r["name"] for r in local_state(rows, args.dest) if not r.get("synced")]

    if args.dry_run or not args.yes:
        print(json.dumps({
            "wouldDelete": [r["name"] for r in targets],
            "wouldDeleteBytes": sum(r["sizeBytes"] for r in targets),
            "skippedNotVerified": unsafe,
            "confirmed": False,
        }, indent=2))
        return 0

    deleted, failed = [], []
    for r in targets:
        ok, code, _ = http_ok(ip, "gopro/media/delete/file?path={}".format(r["path"]))
        (deleted if ok else failed).append(r["name"] if ok else {"name": r["name"], "code": code})

    # Never trust the response alone: confirm against a fresh manifest.
    remaining = {r["path"] for r in fetch_manifest(ip)}
    actually_gone = [r["name"] for r in targets if r["path"] not in remaining]
    print(json.dumps({
        "requested": len(targets),
        "reportedDeleted": len(deleted),
        "confirmedGone": actually_gone,
        "failed": failed,
        "skippedNotVerified": unsafe,
    }, indent=2))
    return 0 if not failed else 1


def do_format(args):
    """Erase the card, but only once every single file is verified on disk."""
    cam = detect()
    if not cam.get("connected"):
        print(cam.get("message"), file=sys.stderr)
        return 1
    ip = cam["ip"]
    rows = local_state(fetch_manifest(ip), args.dest)
    missing = [r["name"] for r in rows if not r.get("synced")]

    if missing:
        print(json.dumps({
            "ok": False,
            "reason": "unsynced-files",
            "missingCount": len(missing),
            "missing": missing[:20],
            "message": ("Refusing to erase: %d file(s) on the card are not yet "
                        "verified on disk." % len(missing)),
        }, indent=2))
        return 1

    if args.dry_run or not args.yes:
        print(json.dumps({
            "ok": True, "confirmed": False,
            "wouldErase": len(rows),
            "wouldEraseBytes": sum(r["sizeBytes"] for r in rows),
        }, indent=2))
        return 0

    # delete/all is the documented one-shot, but firmware support for it
    # varies. Per-file deletion is known to work here, so fall back to it
    # rather than telling the user to go and use the camera's own menu.
    ok, code, body = http_ok(ip, "gopro/media/delete/all", timeout=120)
    time.sleep(2)
    try:
        remaining = fetch_manifest(ip)
    except Exception:
        remaining = []

    used_fallback = False
    if remaining:
        used_fallback = True
        # Safe by construction: every file here was verified on disk above.
        for r in remaining:
            http_ok(ip, "gopro/media/delete/file?path={}".format(r["path"]), timeout=30)
        time.sleep(2)
        try:
            remaining = fetch_manifest(ip)
        except Exception:
            remaining = []

    # The manifest, not the HTTP status, decides whether this worked.
    print(json.dumps({
        "ok": not remaining,
        "httpStatus": code,
        "response": body,
        "usedPerFileFallback": used_fallback,
        "remainingFiles": len(remaining),
        "message": ("Card erased." if not remaining else
                    "The camera kept %s file(s). Format the card from the "
                    "camera's own menu to finish clearing it." % len(remaining)),
    }, indent=2))
    return 0 if not remaining else 1


# ---------------------------------------------------------------------- thumbs

def do_thumb(args):
    cam = detect()
    if not cam.get("connected"):
        return 1
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    out = THUMB_DIR / (args.name.replace("/", "_") + ".jpg")
    if out.exists() and out.stat().st_size > 0:
        print(str(out))
        return 0
    try:
        data = http_bytes(cam["ip"], "gopro/media/thumbnail?path={}".format(args.path), timeout=20)
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1
    tmp = out.with_suffix(".jpg.part")
    tmp.write_bytes(data)
    os.replace(tmp, out)
    print(str(out))
    return 0


def do_thumbs(args):
    """Prefetch thumbnails for the panel, newest first, capped per day.

    Every thumbnail is another request on a link the sync already saturates,
    so the panel asks for a handful per day rather than the whole card.
    """
    cam = detect()
    if not cam.get("connected"):
        print(json.dumps({}))
        return 1
    THUMB_DIR.mkdir(parents=True, exist_ok=True)

    try:
        rows = fetch_manifest(cam["ip"])
    except Exception:
        print(json.dumps({}))
        return 1

    per_day = {}
    wanted = []
    for row in sorted(rows, key=lambda r: r["createdTs"], reverse=True):
        if args.day and row["day"] not in args.day:
            continue
        seen = per_day.get(row["day"], 0)
        if seen >= args.limit:
            continue
        per_day[row["day"]] = seen + 1
        wanted.append(row)

    out = {}
    for row in wanted:
        target = THUMB_DIR / (row["path"].replace("/", "_") + ".jpg")
        if not (target.exists() and target.stat().st_size > 0):
            try:
                data = http_bytes(cam["ip"], "gopro/media/thumbnail?path={}".format(row["path"]),
                                  timeout=20)
            except Exception:
                continue
            tmp = target.with_suffix(".jpg.part")
            tmp.write_bytes(data)
            os.replace(tmp, target)
        out[row["path"]] = str(target)
    print(json.dumps(out))
    return 0


# ---------------------------------------------------------------------- status

def Number_ok(value):
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def do_status(args):
    cam = detect()
    payload = {"camera": cam, "dest": str(args.dest), "job": read_job()}

    if not cam.get("connected"):
        payload["days"] = []
        payload["totals"] = {}
        print(json.dumps(payload))
        return 0

    try:
        state = http_json(cam["ip"], "gopro/camera/state")
        status = state.get("status", {})
        payload["battery"] = status.get("70")
        payload["batteryBars"] = status.get("2")
        payload["sdRemainingBytes"] = int(status.get("54") or 0)
        payload["busy"] = status.get("8") == 1
    except Exception:
        pass

    try:
        rows = local_state(fetch_manifest(cam["ip"]), args.dest)
    except Exception as e:
        payload["error"] = "Could not read the media list: %s" % e
        print(json.dumps(payload))
        return 1

    # The rate the last completed job actually achieved, so the panel can
    # predict the next one from this camera and link rather than a constant.
    last = payload.get("job")
    if last and last.get("status") == "done" and Number_ok(last.get("rateBps")):
        payload["lastRateBps"] = float(last["rateBps"])

    pending = [r for r in rows if not r.get("synced")]
    payload["fingerprint"] = fingerprint(rows)
    payload["days"] = group_days(rows)
    payload["totals"] = {
        "files": len(rows),
        "bytes": sum(r["sizeBytes"] for r in rows),
        "pendingFiles": len(pending),
        "pendingBytes": sum(r["sizeBytes"] for r in pending),
        "photos": sum(1 for r in rows if r["kind"] == "photo"),
        "videos": sum(1 for r in rows if r["kind"] == "video"),
    }
    if args.files:
        payload["files"] = rows
    print(json.dumps(payload))
    return 0


def do_verify(args):
    """Filesystem versus manifest. This, not a log file, is the truth."""
    cam = detect()
    if not cam.get("connected"):
        print(cam.get("message"), file=sys.stderr)
        return 1
    rows = local_state(fetch_manifest(cam["ip"]), args.dest)
    missing = [r for r in rows if not r.get("synced")]
    truncated = [r for r in missing if r.get("localBytes")]
    print(json.dumps({
        "total": len(rows),
        "verified": len(rows) - len(missing),
        "missing": [r["name"] for r in missing],
        "truncated": [{"name": r["name"], "onDisk": r["localBytes"],
                       "expected": r["sizeBytes"]} for r in truncated],
        "ok": not missing,
    }, indent=2))
    return 0 if not missing else 1


def do_cancel(args):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CANCEL_PATH.write_text("1")
    print("Cancel requested.")
    return 0


def do_job(args):
    print(json.dumps(read_job() or {"status": "idle"}))
    return 0


def do_open(args):
    path = Path(args.path or args.dest)
    path.mkdir(parents=True, exist_ok=True)
    for launcher in (["uwsm-app", "--", "xdg-open", str(path)], ["xdg-open", str(path)]):
        if shutil.which(launcher[0]):
            subprocess.Popen(launcher, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return 0
    return 1


# ------------------------------------------------------------------------- cli

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dest", type=Path, default=DEFAULT_DEST,
                   help="destination root (default: %(default)s)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("detect", help="is a camera reachable")
    s = sub.add_parser("status", help="camera, card and sync state as JSON")
    s.add_argument("--files", action="store_true", help="include every file row")

    s = sub.add_parser("sync", help="copy media off the camera")
    s.add_argument("--all", action="store_true", help="everything not yet on disk")
    s.add_argument("--day", action="append", help="a YYYY-MM-DD day (repeatable)")
    s.add_argument("--name", action="append", help="a single filename (repeatable)")
    s.add_argument("--foreground", action="store_true",
                   help="run here instead of detaching")

    sub.add_parser("job", help="current job state as JSON")
    sub.add_parser("cancel", help="ask a running sync to stop")
    sub.add_parser("verify", help="check the archive against the manifest")

    s = sub.add_parser("thumb", help="fetch and cache one thumbnail")
    s.add_argument("path", help="e.g. 100GOPRO/GX010485.MP4")
    s.add_argument("--name", help="cache key (defaults to the path)")

    s = sub.add_parser("thumbs", help="prefetch thumbnails for the panel")
    s.add_argument("--day", action="append", help="limit to these days")
    s.add_argument("--limit", type=int, default=4, help="thumbnails per day")

    s = sub.add_parser("delete", help="delete verified-copied files from the camera")
    s.add_argument("--day", action="append")
    s.add_argument("--name", action="append")
    s.add_argument("--yes", action="store_true", help="actually do it")
    s.add_argument("--dry-run", action="store_true")

    s = sub.add_parser("format", help="erase the card once everything is verified")
    s.add_argument("--yes", action="store_true", help="actually do it")
    s.add_argument("--dry-run", action="store_true")

    s = sub.add_parser("open", help="open the archive in the file manager")
    s.add_argument("path", nargs="?")

    args = p.parse_args()
    if args.cmd == "thumb" and not args.name:
        args.name = args.path

    if args.cmd == "detect":
        print(json.dumps(detect(), indent=2))
        return 0
    if args.cmd == "status":
        return do_status(args)
    if args.cmd == "sync":
        if args.foreground:
            return run_sync(days=args.day, names=args.name, dest_root=args.dest,
                            everything=args.all)
        return spawn_sync(args)
    if args.cmd == "job":
        return do_job(args)
    if args.cmd == "cancel":
        return do_cancel(args)
    if args.cmd == "verify":
        return do_verify(args)
    if args.cmd == "thumb":
        return do_thumb(args)
    if args.cmd == "thumbs":
        return do_thumbs(args)
    if args.cmd == "delete":
        return do_delete(args)
    if args.cmd == "format":
        return do_format(args)
    if args.cmd == "open":
        return do_open(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
