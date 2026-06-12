#!/usr/bin/env bash
set -euo pipefail

echo "[theta-z1-probe] USB devices"
lsusb || true
echo

echo "[theta-z1-probe] V4L2 devices"
v4l2-ctl --list-devices || true
echo

for dev in /dev/video*; do
  [[ -e "$dev" ]] || continue
  echo "[theta-z1-probe] formats for $dev"
  v4l2-ctl -d "$dev" --list-formats-ext || true
  echo
done

echo "[theta-z1-probe] GStreamer video sources"
gst-device-monitor-1.0 Video/Source || true
