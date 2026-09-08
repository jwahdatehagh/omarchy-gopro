// Run with: node --test tests/model.test.js
const test = require("node:test")
const assert = require("node:assert")
const Model = require("../Model.js")

// Captured verbatim from `gopro.py status` against a HERO9 on USB, partway
// through a sync, so the shapes below are the real ones and not invented.
const LIVE = '{"camera": {"connected": true, "ip": "172.22.194.51", "serial": "C3441328099294", "model": "HERO9"}, "dest": "/home/jalil/Pictures/GoPro", "job": {"schema": 1, "status": "done", "pid": 54421, "totalFiles": 1, "totalBytes": 55487106, "doneFiles": 1, "doneBytes": 55487106, "currentFile": "", "rateBps": 8660244.44, "etaSec": 0, "failures": [], "vanished": [], "warnings": []}, "battery": 54, "batteryBars": 2, "sdRemainingBytes": 230050304, "busy": true, "fingerprint": "fc500f0254358968", "days": [{"day": "2026-09-08", "count": 47, "bytes": 15381623275, "syncedCount": 34, "syncedBytes": 11870300940, "photos": 1, "videos": 46, "complete": false, "pendingCount": 13, "pendingBytes": 3511322335}, {"day": "2026-09-07", "count": 11, "bytes": 3585818920, "syncedCount": 11, "syncedBytes": 3585818920, "photos": 0, "videos": 11, "complete": true, "pendingCount": 0, "pendingBytes": 0}], "totals": {"files": 58, "bytes": 18967442195, "pendingFiles": 13, "pendingBytes": 3511322335, "photos": 1, "videos": 57}}'

test("parses a live camera payload", () => {
  const s = Model.parseStatus(LIVE)
  assert.equal(s.connected, true)
  assert.equal(s.model, "HERO9")
  assert.equal(s.battery, 54)
  assert.equal(s.days.length, 2)
  assert.equal(s.totals.pendingFiles, 13)
})

test("bad input degrades instead of throwing", () => {
  assert.equal(Model.parseStatus("").connected, false)
  assert.equal(Model.parseStatus("not json").ok, false)
  assert.equal(Model.parseStatus(null).connected, false)
  assert.equal(Model.parseJob("{{{"), null)
})

test("a disconnected camera keeps its explanation", () => {
  const s = Model.parseStatus(JSON.stringify({
    camera: { connected: false, reason: "no-http", message: "Set USB Connection to GoPro Connect." }
  }))
  assert.equal(s.connected, false)
  assert.equal(s.reason, "no-http")
  assert.match(s.message, /GoPro Connect/)
})

test("formats byte sizes in the decimal units cameras advertise", () => {
  assert.equal(Model.formatBytes(0), "0 B")
  assert.equal(Model.formatBytes(999), "999 B")
  assert.equal(Model.formatBytes(18967442195), "19 GB")
  assert.equal(Model.formatBytes(230050304), "230 MB")
  assert.equal(Model.formatBytes(-5), "0 B")
})

test("durations stay coarse", () => {
  assert.equal(Model.formatDuration(0.2), "1s")
  assert.equal(Model.formatDuration(45), "45s")
  assert.equal(Model.formatDuration(600), "10m")
  assert.equal(Model.formatDuration(3600), "1h")
  assert.equal(Model.formatDuration(5400), "1h 30m")
  assert.equal(Model.formatDuration(null), "")
})

test("day labels are relative near today", () => {
  const now = Date.parse("2026-09-08T12:00:00")
  assert.equal(Model.dayLabel("2026-09-08", now), "Today")
  assert.equal(Model.dayLabel("2026-09-07", now), "Yesterday")
  assert.equal(Model.dayLabel("2026-06-01", now), "Jun 1")
  assert.equal(Model.dayLabel("2025-09-03", now), "Sep 3, 2025")
  assert.equal(Model.dayLabel("garbage", now), "Undated")
})

test("progress is computed from bytes, never file counts", () => {
  // Thirteen small files left out of 47 is 8% of the bytes but 28% of the
  // count. Reporting the count would promise a finish that never comes.
  const s = Model.parseStatus(LIVE)
  const day = s.days[0]
  assert.ok(Math.abs(Model.dayProgress(day) - 0.7717) < 0.001)

  const job = { status: "running", totalBytes: 1000, doneBytes: 250, totalFiles: 2, doneFiles: 1 }
  assert.equal(Model.jobProgress(job), 0.25)
  assert.equal(Model.jobProgress({ status: "running", totalBytes: 0 }), 0)
  assert.equal(Model.jobProgress(null), 0)
})

test("progress fractions never leave 0..1", () => {
  assert.equal(Model.jobProgress({ totalBytes: 100, doneBytes: 500 }), 1)
  assert.equal(Model.jobProgress({ totalBytes: 100, doneBytes: -20 }), 0)
})

test("headline tracks the job through its states", () => {
  const s = Model.parseStatus(LIVE)
  assert.equal(Model.jobHeadline({ status: "running", doneFiles: 3, totalFiles: 13 }, s), "Copying 4 of 13")
  assert.equal(Model.jobHeadline({ status: "canceled" }, s), "Sync canceled")
  assert.equal(Model.jobHeadline({ status: "failed" }, s), "Sync incomplete")
  assert.equal(Model.jobHeadline(null, s), "13 files to copy")
  assert.equal(Model.jobHeadline(null, Model.defaultStatus()), "No camera")
})

test("headline never counts past the total on the last file", () => {
  const s = Model.parseStatus(LIVE)
  assert.equal(Model.jobHeadline({ status: "running", doneFiles: 13, totalFiles: 13 }, s), "Copying 13 of 13")
})

test("a finished card says so", () => {
  const done = JSON.parse(LIVE)
  done.totals.pendingFiles = 0
  done.totals.pendingBytes = 0
  const s = Model.parseStatus(JSON.stringify(done))
  assert.equal(Model.jobHeadline(null, s), "Everything copied")
  assert.match(Model.jobDetail(null, s), /all copied/)
})

test("running detail carries eta, rate and bytes", () => {
  const detail = Model.jobDetail(
    { status: "running", etaSec: 900, rateBps: 8400000, doneBytes: 1e9, totalBytes: 4e9 },
    Model.defaultStatus())
  assert.match(detail, /15m left/)
  assert.match(detail, /8.4 MB\/s/)
  assert.match(detail, /1 GB of 4 GB/)
})

test("erase is gated on every file being verified on disk", () => {
  const s = Model.parseStatus(LIVE)
  assert.equal(Model.canErase(s), false)
  assert.match(Model.eraseBlockedReason(s), /13 file\(s\) are not on disk yet/)

  const done = JSON.parse(LIVE)
  done.totals.pendingFiles = 0
  const ready = Model.parseStatus(JSON.stringify(done))
  assert.equal(Model.canErase(ready), true)
  assert.equal(Model.eraseBlockedReason(ready), "")
})

test("erase is refused with no camera and on an empty card", () => {
  assert.equal(Model.canErase(Model.defaultStatus()), false)
  assert.match(Model.eraseBlockedReason(Model.defaultStatus()), /No camera/)

  const empty = JSON.parse(LIVE)
  empty.totals.files = 0
  empty.totals.pendingFiles = 0
  const s = Model.parseStatus(JSON.stringify(empty))
  assert.equal(Model.canErase(s), false)
  assert.match(Model.eraseBlockedReason(s), /already empty/)
})

test("a nearly full card is flagged", () => {
  const s = Model.parseStatus(LIVE)
  assert.equal(Model.cardNearlyFull(s), true) // 230 MB free
  assert.equal(Model.cardText(s), "230 MB free")
  const roomy = JSON.parse(LIVE)
  roomy.sdRemainingBytes = 40e9
  assert.equal(Model.cardNearlyFull(Model.parseStatus(JSON.stringify(roomy))), false)
})

test("cold-sync estimate uses the measured USB ceiling", () => {
  // 19 GB over a link that really does sustain ~8.4 MB/s.
  assert.equal(Model.estimateDuration(18967442195), "38m")
})

test("day meta describes the contents, status is kept separate", () => {
  const s = Model.parseStatus(LIVE)
  assert.equal(Model.dayMeta(s.days[1]), "11 videos · 3.59 GB")
  assert.equal(Model.dayMeta(s.days[0]), "46 videos, 1 photo · 15.4 GB")
  // Split so the panel can elide the contents without ever dropping the
  // status, which is what gates the erase button.
  assert.equal(Model.dayStatus(s.days[1]), "On disk")
  assert.equal(Model.dayStatus(s.days[0]), "34 of 47 copied")
  assert.equal(Model.dayStatus({ count: 5, syncedCount: 0, complete: false }), "Not copied")
})

test("battery glyphs cover the range", () => {
  assert.equal(Model.batteryGlyph(-1), "")
  assert.notEqual(Model.batteryGlyph(95), Model.batteryGlyph(10))
})

test("day thumbnails are the newest few of that day", () => {
  const files = [
    { name: "a.MP4", day: "2026-09-08", createdTs: 100, kind: "video", path: "100GOPRO/a.MP4" },
    { name: "b.MP4", day: "2026-09-08", createdTs: 300, kind: "video", path: "100GOPRO/b.MP4" },
    { name: "c.JPG", day: "2026-09-07", createdTs: 200, kind: "photo", path: "100GOPRO/c.JPG" },
    { name: "d.MP4", day: "2026-09-08", createdTs: 200, kind: "video", path: "100GOPRO/d.MP4" }
  ]
  const picked = Model.dayThumbs(files, "2026-09-08", 2)
  assert.deepEqual(picked.map(f => f.name), ["b.MP4", "d.MP4"])
  assert.equal(Model.dayThumbs(files, "2026-09-07", 4).length, 1)
  assert.equal(Model.dayThumbs(files, "2026-09-08", 0).length, 0)
  assert.equal(Model.dayThumbs(null, "2026-09-08", 4).length, 0)
  assert.equal(Model.dayThumbs(files, "", 4).length, 0)
})
