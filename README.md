# GoPro for Omarchy

Copy photos and video off a USB-connected GoPro without a card reader, from
the Omarchy bar.

![the panel](preview.png)

## Why this exists

A GoPro whose `Preferences → Connections → USB Connection` is set to **GoPro
Connect** does not appear as a storage device. It enumerates as a USB
CDC-Ethernet adapter, so no file manager on any OS will show it and
`mtp-detect` finds nothing. The camera is charging and apparently fine, and
its files are unreachable.

What it does do in that mode is run an HTTP server. This plugin drives it.

MTP mode makes the camera mount normally in a file manager, but it turns this
HTTP API off. The two are mutually exclusive — pick one.

## What it does

- Lists the card by day, with thumbnails pulled from the camera's own proxies.
- Copies to `~/Pictures/GoPro/YYYY-MM-DD/`, original filenames kept, photos
  and video together. Day folders come from each file's creation time in
  local time.
- Runs transfers detached, so a multi-hour copy survives the panel closing,
  the bar reloading, and the shell restarting.
- Reports live progress and an ETA computed from bytes, with per-day and
  whole-card progress bars.
- Removes files from the camera, or erases the card, once — and only once —
  every file is verified byte-for-byte on disk.

Nothing is copied or deleted until you ask. Plugging a camera in gets you a
notification saying how much is waiting, and nothing else.

## Requirements

- Omarchy 4 (`omarchy-shell`, Quickshell)
- `python3` — standard library only, no pip packages
- `notify-send` (`libnotify`) for the connect and completion notifications
- `xdg-open` for the "open folder" buttons

All four ship with a standard Omarchy install. The plugin bundles no binaries
and downloads nothing at install or run time; it talks only to the camera on
its link-local USB address.

## Install

```bash
omarchy plugin add https://github.com/jwahdatehagh/omarchy-gopro.git
omarchy plugin enable digital.1001.gopro
```

Plugins land disabled so you can read the code first.

## Remove

```bash
omarchy plugin remove digital.1001.gopro
```

That deletes the plugin checkout and drops its widget from the bar. It leaves
your copied media alone. To remove everything else the plugin wrote:

```bash
rm -rf ~/.local/state/omarchy-gopro   # transfer job state and log
rm -rf ~/.cache/omarchy-gopro         # cached thumbnails
```

Your archive in `~/Pictures/GoPro/` is never touched by removal — delete it
yourself if you want it gone.

## What it touches

- Writes media to the destination directory, `~/Pictures/GoPro/` by default.
- Writes job state to `~/.local/state/omarchy-gopro/` and thumbnails to
  `~/.cache/omarchy-gopro/`.
- Its settings live in `~/.config/omarchy/shell.json` alongside every other
  widget's, written by Omarchy rather than by the plugin.

It edits no other configuration, needs no root, and installs no services.

## Using it

Click the bar icon. Keys inside the panel:

| Key | Action |
|-----|--------|
| `s` | copy everything not yet on disk |
| `x` | stop a running copy |
| `r` | re-read the camera |
| `o` | open the archive |
| `e` | erase the card (asks first) |
| `↑` `↓` | move between days |
| `Enter` | copy that day, or open its folder when it is already complete |

Per-day buttons copy, open the folder, or remove that day from the camera.

## From the terminal

`gopro.py` is the whole implementation and works standalone:

```bash
./gopro.py detect                 # is a camera reachable, and at what address
./gopro.py status                 # camera, card and archive state as JSON
./gopro.py sync --all             # detached copy; progress lands in job.json
./gopro.py sync --day 2026-09-08  # one day
./gopro.py job                    # live progress
./gopro.py cancel
./gopro.py verify                 # archive vs manifest — the source of truth
./gopro.py delete --day 2026-09-08 --dry-run
./gopro.py format --dry-run
```

Destructive commands print what they would do and change nothing unless given
`--yes`.

## IPC

```bash
omarchy-shell gopro open|close|toggle|refresh|sync|cancel|status
omarchy-shell gopro erase          # opens the confirmation; never erases by itself
```

## How it addresses the camera

The camera derives its wired address from its serial number: `172.2X.1YZ.51`,
where `X`, `Y` and `Z` are the last three digits. Serial `…294` puts the
camera on `172.22.194.51`. The plugin reads the serial from the USB
descriptor rather than hardcoding an address, and falls back to scanning the
CDC-Ethernet interface's subnet. Do not assume the host sits on `.55` — it is
DHCP-assigned and moves.

## Things it is careful about

Every one of these came from losing something, or nearly losing it:

- **A 403 on a download means "no such file", not "the media server is
  broken".** The plugin re-reads the manifest before drawing any conclusion,
  and reports the file as having vanished rather than as an error.
- **The card can change underneath a running transfer.** The manifest is
  fingerprinted and re-checked every couple of minutes; if it changes mid-job
  the remaining queue is re-checked against the new one and the panel says so.
- **Downloads land in `<name>.part` and are renamed only when whole.** An
  interrupted transfer left under its final name looks complete to any later
  check and silently corrupts the archive.
- **A file counts as copied only when it exists and its size matches the
  manifest exactly.** That single rule makes re-running free, resumable and
  self-healing.
- **Nothing is deleted that is not verified on disk first**, and the
  filesystem-versus-manifest check — not a log file — decides what is
  verified.

Treat anything still only on the camera as ephemeral.

## Speed

Measured on a HERO9: **18.4 MB/s sustained**, verified over a 1.14 GB run
whose files came back byte-identical to earlier copies of the same clips.
That is with turbo transfer enabled, which the plugin switches on for the
duration of a sync and off again afterwards; without it the same link
sustains about 8.4 MB/s. Budget roughly a minute per gigabyte, so 19 GB takes
around 17 minutes.

The camera's link is the bottleneck, not the disk. The first estimate you see
uses the conservative 8.4 MB/s figure; after one completed transfer the panel
predicts from the rate that job actually achieved.

## Settings

Destination, thumbnails per day, refresh interval, whether to hide the icon
when no camera is connected, and the two notification toggles. All in
Setup → Plugins.

## Tests

```bash
node --test tests/model.test.js
```

## License

MIT.

## Developing

The plugin lives in `~/.config/omarchy/plugins/digital.1001.gopro/`. If that
is a symlink to a checkout elsewhere, the shell's file watcher does not follow
it, so edits need `omarchy-restart-shell` rather than picking themselves up.
A real directory there hot-reloads on save.
