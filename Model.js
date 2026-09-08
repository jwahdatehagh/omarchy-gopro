// Pure presentation logic for the GoPro panel. No QML imports here on
// purpose: everything below runs unchanged under node, so tests/model.test.js
// can exercise it without a shell.

function defaultStatus() {
  return {
    ok: true,
    connected: false,
    reason: "",
    message: "",
    model: "",
    ip: "",
    battery: -1,
    sdRemainingBytes: 0,
    dest: "",
    days: [],
    totals: { files: 0, bytes: 0, pendingFiles: 0, pendingBytes: 0, photos: 0, videos: 0 },
    job: null,
    fingerprint: ""
  }
}

function parseStatus(raw) {
  var text = String(raw || "").trim()
  if (text === "") return defaultStatus()
  var parsed
  try {
    parsed = JSON.parse(text)
  } catch (e) {
    var failed = defaultStatus()
    failed.ok = false
    failed.message = "Could not read the camera status."
    return failed
  }
  if (!parsed || typeof parsed !== "object") return defaultStatus()

  var camera = parsed.camera || {}
  var status = defaultStatus()
  status.connected = camera.connected === true
  status.reason = String(camera.reason || "")
  status.message = String(camera.message || parsed.error || "")
  status.model = String(camera.model || "")
  status.ip = String(camera.ip || "")
  status.serial = String(camera.serial || "")
  status.battery = parsed.battery === undefined || parsed.battery === null ? -1 : Number(parsed.battery)
  status.sdRemainingBytes = Number(parsed.sdRemainingBytes || 0)
  status.dest = String(parsed.dest || "")
  status.fingerprint = String(parsed.fingerprint || "")
  status.days = Array.isArray(parsed.days) ? parsed.days : []
  status.totals = parsed.totals && typeof parsed.totals === "object" ? parsed.totals : status.totals
  status.job = parsed.job || null
  return status
}

function parseJob(raw) {
  var text = String(raw || "").trim()
  if (text === "") return null
  try {
    var job = JSON.parse(text)
    return job && typeof job === "object" ? job : null
  } catch (e) {
    return null
  }
}

// Decimal units, matching how camera and card capacities are advertised.
function formatBytes(bytes) {
  var value = Number(bytes || 0)
  if (!isFinite(value) || value <= 0) return "0 B"
  var units = ["B", "KB", "MB", "GB", "TB"]
  var index = 0
  while (value >= 1000 && index < units.length - 1) {
    value = value / 1000
    index++
  }
  var decimals = value >= 100 || index === 0 ? 0 : (value >= 10 ? 1 : 2)
  return value.toFixed(decimals).replace(/\.0+$/, "").replace(/(\.\d)0$/, "$1") + " " + units[index]
}

function formatRate(bytesPerSecond) {
  var rate = Number(bytesPerSecond || 0)
  if (!isFinite(rate) || rate <= 0) return ""
  return formatBytes(rate) + "/s"
}

// Deliberately coarse: a transfer this long does not need second-precision,
// and a jittery countdown reads as a broken estimate.
function formatDuration(seconds) {
  // null/undefined means "not known yet" -- the job reports a null eta until
  // it has enough samples to measure a rate, and that must read as blank
  // rather than as "1s left".
  if (seconds === null || seconds === undefined || seconds === "") return ""
  var total = Number(seconds)
  if (!isFinite(total) || total < 0) return ""
  if (total < 60) return Math.max(1, Math.round(total)) + "s"
  var minutes = Math.round(total / 60)
  if (minutes < 60) return minutes + "m"
  var hours = Math.floor(minutes / 60)
  var rest = minutes % 60
  return rest === 0 ? hours + "h" : hours + "h " + rest + "m"
}

function isoDay(date) {
  function pad(n) { return (n < 10 ? "0" : "") + n }
  return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate())
}

function dayLabel(day, nowMs) {
  var value = String(day || "")
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return "Undated"
  var now = nowMs === undefined ? Date.now() : Number(nowMs)
  var today = new Date(now)
  if (value === isoDay(today)) return "Today"
  var yesterday = new Date(now - 86400000)
  if (value === isoDay(yesterday)) return "Yesterday"

  var parts = value.split("-")
  var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
  var label = months[parseInt(parts[1], 10) - 1] + " " + parseInt(parts[2], 10)
  if (parts[0] !== String(today.getFullYear())) label += ", " + parts[0]
  return label
}

// What the day holds. Kept free of sync state so the panel can elide this
// half of the line without ever losing the status, which is the part you
// actually need before deciding to erase anything.
function dayMeta(day) {
  if (!day) return ""
  var shots = []
  if (Number(day.videos || 0) > 0) shots.push(day.videos + (day.videos === 1 ? " video" : " videos"))
  if (Number(day.photos || 0) > 0) shots.push(day.photos + (day.photos === 1 ? " photo" : " photos"))
  return (shots.length ? shots.join(", ") : day.count + " files") + " · " + formatBytes(day.bytes)
}

// Where that day stands against the archive. Rendered separately and never
// elided.
function dayStatus(day) {
  if (!day) return ""
  if (day.complete) return "On disk"
  var synced = Number(day.syncedCount || 0)
  if (synced > 0) return synced + " of " + day.count + " copied"
  return "Not copied"
}

function dayProgress(day) {
  if (!day) return 0
  var total = Number(day.bytes || 0)
  if (total <= 0) return 0
  return Math.max(0, Math.min(1, Number(day.syncedBytes || 0) / total))
}

function jobActive(job) {
  return !!job && job.status === "running"
}

// Bytes, never file counts: a card mixing 5 MB stills with 4 GB clips makes
// any count-based fraction lie badly.
function jobProgress(job) {
  if (!job) return 0
  var total = Number(job.totalBytes || 0)
  if (total <= 0) return 0
  var done = Number(job.doneBytes || 0)
  return Math.max(0, Math.min(1, done / total))
}

function jobHeadline(job, status) {
  if (jobActive(job)) {
    var done = Number(job.doneFiles || 0) + 1
    var total = Number(job.totalFiles || 0)
    return "Copying " + Math.min(done, total) + " of " + total
  }
  if (job && job.status === "canceled") return "Sync canceled"
  if (job && job.status === "failed") return "Sync incomplete"
  if (!status || !status.connected) return "No camera"
  var pending = Number((status.totals || {}).pendingFiles || 0)
  if (pending === 0) return "Everything copied"
  return pending + (pending === 1 ? " file to copy" : " files to copy")
}

function jobDetail(job, status) {
  if (jobActive(job)) {
    var bits = []
    var eta = formatDuration(job.etaSec)
    if (eta) bits.push(eta + " left")
    var rate = formatRate(job.rateBps)
    if (rate) bits.push(rate)
    bits.push(formatBytes(job.doneBytes) + " of " + formatBytes(job.totalBytes))
    return bits.join(" · ")
  }
  // A whole-job error (an unwritable destination, a full disk) is the thing
  // the user has to act on, so it outranks the per-file failure count.
  if (job && job.error) return String(job.error)
  if (job && job.status === "failed" && Array.isArray(job.failures) && job.failures.length)
    return job.failures.length + " file(s) failed — open the panel to retry"
  if (!status || !status.connected) return ""
  var totals = status.totals || {}
  if (Number(totals.pendingBytes || 0) > 0)
    return formatBytes(totals.pendingBytes) + " · about " + estimateDuration(totals.pendingBytes)
  if (Number(totals.files || 0) > 0)
    return formatBytes(totals.bytes) + " on the card, all copied"
  return "The card is empty"
}

// USB 2.0 CDC-Ethernet tops out around 8.4 MB/s and the camera, not the disk,
// is the bottleneck — so a static rate predicts a cold sync well enough.
var ASSUMED_BYTES_PER_SEC = 8.4 * 1000 * 1000

function estimateDuration(bytes) {
  return formatDuration(Number(bytes || 0) / ASSUMED_BYTES_PER_SEC)
}

function batteryGlyph(percent) {
  var value = Number(percent)
  if (!isFinite(value) || value < 0) return ""
  if (value >= 90) return "󰁹"
  if (value >= 70) return "󰂀"
  if (value >= 45) return "󰁾"
  if (value >= 20) return "󰁻"
  return "󰁺"
}

function cardText(status) {
  if (!status || !status.connected) return ""
  var remaining = Number(status.sdRemainingBytes || 0)
  return formatBytes(remaining) + " free"
}

// The card being nearly full is the whole reason to offer an erase, so the
// panel says so rather than making it a number to interpret.
function cardNearlyFull(status, thresholdBytes) {
  if (!status || !status.connected) return false
  var limit = thresholdBytes === undefined ? 1000 * 1000 * 1000 : Number(thresholdBytes)
  return Number(status.sdRemainingBytes || 0) < limit
}

function canErase(status) {
  if (!status || !status.connected) return false
  var totals = status.totals || {}
  return Number(totals.files || 0) > 0 && Number(totals.pendingFiles || 0) === 0
}

function eraseBlockedReason(status) {
  if (!status || !status.connected) return "No camera connected."
  var totals = status.totals || {}
  if (Number(totals.files || 0) === 0) return "The card is already empty."
  var pending = Number(totals.pendingFiles || 0)
  if (pending > 0)
    return pending + " file(s) are not on disk yet. Copy everything first."
  return ""
}

// The newest few files of a day, for the panel's thumbnail strip. Newest
// first because that is what you just shot and what you are looking for.
function dayThumbs(files, day, limit) {
  if (!Array.isArray(files) || !day) return []
  var cap = limit === undefined ? 4 : Number(limit)
  if (!isFinite(cap) || cap <= 0) return []
  var rows = []
  for (var i = 0; i < files.length; i++) {
    if (files[i] && files[i].day === day) rows.push(files[i])
  }
  rows.sort(function(a, b) { return Number(b.createdTs || 0) - Number(a.createdTs || 0) })
  return rows.slice(0, cap)
}

function fileGlyph(kind) {
  if (kind === "photo") return "󰋩"
  if (kind === "video") return "󰈫"
  return "󰈔"
}

if (typeof module !== "undefined") {
  module.exports = {
    defaultStatus: defaultStatus,
    parseStatus: parseStatus,
    parseJob: parseJob,
    formatBytes: formatBytes,
    formatRate: formatRate,
    formatDuration: formatDuration,
    isoDay: isoDay,
    dayLabel: dayLabel,
    dayMeta: dayMeta,
    dayStatus: dayStatus,
    dayProgress: dayProgress,
    jobActive: jobActive,
    jobProgress: jobProgress,
    jobHeadline: jobHeadline,
    jobDetail: jobDetail,
    estimateDuration: estimateDuration,
    batteryGlyph: batteryGlyph,
    cardText: cardText,
    cardNearlyFull: cardNearlyFull,
    canErase: canErase,
    eraseBlockedReason: eraseBlockedReason,
    dayThumbs: dayThumbs,
    fileGlyph: fileGlyph
  }
}
