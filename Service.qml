import QtQuick
import Quickshell
import Quickshell.Io
import "Model.js" as Model

// Everything the panel knows about the camera, and the only place that talks
// to gopro.py. Transfers deliberately run as a detached process rather than a
// child of the shell: a 19 GB copy takes the better part of an hour and must
// survive the panel closing, the bar reloading, or the shell restarting.
Item {
  id: root

  property var settings: ({})

  property var status: Model.defaultStatus()
  property var files: []
  property var thumbs: ({})
  property var job: null

  property bool refreshing: false
  property string actionStatus: ""
  property string lastError: ""

  readonly property bool connected: status.connected === true
  readonly property bool syncing: Model.jobActive(job)
  readonly property bool busy: statusProcess.running || actionProcess.running

  readonly property int refreshIntervalSec: intSetting("refreshIntervalSec", 20, 5, 300)
  readonly property int thumbnailsPerDay: intSetting("thumbnailsPerDay", 4, 0, 10)
  readonly property string destination: String(setting("destination", "") || "")

  // Qt hands back a file:// URL for the plugin's own directory, which is how
  // the script is found no matter where the plugin was installed.
  readonly property string scriptPath: {
    var url = Qt.resolvedUrl("gopro.py").toString()
    return url.indexOf("file://") === 0 ? url.substring(7) : url
  }

  property bool _seenCamera: false
  property string _lastJobStatus: ""
  property bool _primed: false

  signal transferFinished(string statusText)

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function boolSetting(name, fallback) {
    var value = setting(name, fallback)
    return value === true || value === "true"
  }

  function intSetting(name, fallback, min, max) {
    var n = parseInt(String(setting(name, fallback)), 10)
    if (!isFinite(n)) n = fallback
    return Math.max(min, Math.min(max, n))
  }

  function baseArgs() {
    var args = ["python3", scriptPath]
    if (destination !== "") args = args.concat(["--dest", destination])
    return args
  }

  function refresh() {
    if (statusProcess.running) return
    refreshing = true
    statusProcess.command = baseArgs().concat(["status", "--files"])
    statusProcess.running = true
  }

  function refreshThumbs() {
    if (thumbProcess.running || thumbnailsPerDay <= 0 || !connected) return
    thumbProcess.command = baseArgs().concat(["thumbs", "--limit", String(thumbnailsPerDay)])
    thumbProcess.running = true
  }

  function applyStatus(raw) {
    var parsed = Model.parseStatus(raw)
    if (!parsed.ok) {
      lastError = parsed.message || "Could not read the camera status."
      return
    }

    var wasConnected = _seenCamera
    var parsedJson = {}
    try { parsedJson = JSON.parse(String(raw)) } catch (e) { parsedJson = {} }

    status = parsed
    files = Array.isArray(parsedJson.files) ? parsedJson.files : []
    job = parsed.job
    lastError = parsed.connected ? "" : String(parsed.message || "")

    // A camera that just appeared is worth announcing once; one that was
    // already there when the shell started is not.
    if (parsed.connected && !wasConnected && _primed && boolSetting("notifyOnConnect", true))
      notifyConnected(parsed)
    _seenCamera = parsed.connected
    _primed = true

    trackJobTransition()
    if (parsed.connected) refreshThumbs()
  }

  function trackJobTransition() {
    var now = job ? String(job.status || "") : ""
    if (_lastJobStatus === "running" && now !== "running" && now !== "") {
      var text = now === "done"
        ? "Copied " + (job.doneFiles || 0) + " file(s) to " + (job.dest || destination)
        : (now === "canceled" ? "Transfer canceled."
                              : "Transfer finished with " + ((job.failures || []).length) + " failure(s).")
      transferFinished(text)
      if (boolSetting("notifyOnFinish", true))
        notify(now === "done" ? "GoPro copy finished" : "GoPro copy incomplete", text,
               now === "done" ? "normal" : "critical")
    }
    _lastJobStatus = now
  }

  function notifyConnected(parsed) {
    var totals = parsed.totals || {}
    var pending = Number(totals.pendingFiles || 0)
    var body = pending > 0
      ? pending + " file(s), " + Model.formatBytes(totals.pendingBytes)
        + " — about " + Model.estimateDuration(totals.pendingBytes) + " to copy"
      : "Everything on the card is already on disk."
    notify((parsed.model || "GoPro") + " connected", body, "normal")
  }

  function notify(summary, body, urgency) {
    Quickshell.execDetached(["notify-send", "-a", "GoPro", "-u", urgency || "normal",
                             String(summary), String(body)])
  }

  function syncAll() { runAction(["sync", "--all"], "Starting transfer…") }

  function syncDay(day) {
    if (!day) return
    runAction(["sync", "--day", String(day)], "Copying " + Model.dayLabel(day) + "…")
  }

  function cancel() { runAction(["cancel"], "Stopping…") }

  function eraseCard() {
    // The script refuses on its own if anything is unverified; this is the
    // second lock, not the only one.
    runAction(["format", "--yes"], "Erasing the card…")
  }

  function deleteDay(day) {
    if (!day) return
    runAction(["delete", "--day", String(day), "--yes"],
              "Removing " + Model.dayLabel(day) + " from the camera…")
  }

  function openArchive(path) {
    var args = baseArgs().concat(["open"])
    if (path) args.push(String(path))
    Quickshell.execDetached(args)
  }

  function openDay(day) {
    var root_ = status.dest || destination
    if (!root_ || !day) { openArchive(""); return }
    openArchive(root_ + "/" + day)
  }

  function runAction(args, message) {
    if (actionProcess.running) return
    actionStatus = message || ""
    lastError = ""
    actionProcess.command = baseArgs().concat(args)
    actionProcess.running = true
  }

  function elide(text) {
    var value = String(text || "").replace(/\s+/g, " ").trim()
    return value.length > 160 ? value.substring(0, 157) + "…" : value
  }

  Timer {
    id: statusTimer
    // While a transfer runs the media list barely changes and every request
    // competes with the copy for the same USB link, so back off.
    interval: (root.syncing ? Math.max(60, root.refreshIntervalSec * 3) : root.refreshIntervalSec) * 1000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Timer {
    // The job file is local and cheap; poll it fast enough for a live bar.
    id: jobTimer
    interval: 1000
    repeat: true
    running: root.syncing
    onTriggered: jobProcess.running || (jobProcess.command = root.baseArgs().concat(["job"]),
                                        jobProcess.running = true)
  }

  Timer {
    id: settleTimer
    interval: 900
    repeat: false
    onTriggered: root.refresh()
  }

  Process {
    id: statusProcess
    running: false
    command: []
    stdout: StdioCollector { id: statusOut; waitForEnd: true }
    stderr: StdioCollector { id: statusErr; waitForEnd: true }
    onExited: function(exitCode) {
      root.refreshing = false
      var text = String(statusOut.text || "")
      if (text.trim() !== "") root.applyStatus(text)
      else root.lastError = root.elide(statusErr.text || "Could not reach gopro.py")
    }
  }

  Process {
    id: jobProcess
    running: false
    command: []
    stdout: StdioCollector { id: jobOut; waitForEnd: true }
    onExited: function() {
      var parsed = Model.parseJob(jobOut.text)
      if (parsed) {
        root.job = parsed
        root.trackJobTransition()
      }
    }
  }

  Process {
    id: thumbProcess
    running: false
    command: []
    stdout: StdioCollector { id: thumbOut; waitForEnd: true }
    onExited: function() {
      try {
        var map = JSON.parse(String(thumbOut.text || "{}"))
        if (map && typeof map === "object") root.thumbs = map
      } catch (e) { /* thumbnails are decoration; never fail the panel over one */ }
    }
  }

  Process {
    id: actionProcess
    running: false
    command: []
    stdout: StdioCollector { id: actionOut; waitForEnd: true }
    stderr: StdioCollector { id: actionErr; waitForEnd: true }
    onExited: function(exitCode) {
      var out = String(actionOut.text || "")
      var err = String(actionErr.text || "")
      var payload = null
      try { payload = JSON.parse(out) } catch (e) { payload = null }

      if (exitCode !== 0) {
        // The script explains itself in JSON when it refuses on purpose
        // (unverified files, no camera); prefer that over a raw stderr dump.
        root.lastError = root.elide(
          (payload && payload.message) ? payload.message : (err || out || "The command failed."))
        root.actionStatus = ""
      } else {
        root.actionStatus = (payload && payload.message) ? String(payload.message) : ""
        root.lastError = ""
        if (root.actionStatus !== "") actionStatusTimer.restart()
      }
      settleTimer.restart()
    }
  }

  Timer {
    id: actionStatusTimer
    interval: 6000
    repeat: false
    onTriggered: root.actionStatus = ""
  }
}
