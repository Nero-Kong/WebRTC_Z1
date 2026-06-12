#!/usr/bin/env bash
set -euo pipefail

SIGNAL_URL="${1:-}"
PRESET="${2:-z1-4k}"
DEVICE="${3:-auto}"
CAPS_MODE="${4:-exact}"
STOP_EXISTING="${5:-no}"
RTP_MTU="${6:-1200}"
QUEUE_MS="${7:-80}"
QUEUE_BUFFERS="${8:-24}"
WEBRTC_LATENCY_MS="${9:-0}"
SDP_PROFILE="${10:-low-latency}"
IMU_UDP_LISTEN_PORT="${11:-0}"
IMU_COMMAND="${12:-}"
IMU_MAX_RATE_HZ="${13:-60}"

if [[ -z "$SIGNAL_URL" ]]; then
  echo "Usage: $0 <ws://pc-ip:8765> [z1-4k|z1-2k] [device=auto] [caps=exact|relaxed] [stop=yes|no] [rtp_mtu] [queue_ms] [queue_buffers] [webrtc_latency_ms] [sdp_profile] [imu_udp_listen_port=0] [imu_command=''] [imu_max_rate_hz=60]" >&2
  exit 1
fi

cd "$(dirname "$0")"

if [[ "$STOP_EXISTING" == "yes" ]]; then
  pkill -f edge2_theta_z1_webrtc_sender.py || true
  sleep 0.5
fi

LIBUVC_BINARY="${THETA_Z1_UVC_BINARY:-./theta_z1_uvc_stdout}"
if [[ ! -x "$LIBUVC_BINARY" && -x ./bin/theta_z1_uvc_stdout ]]; then
  LIBUVC_BINARY="./bin/theta_z1_uvc_stdout"
fi

if [[ ! -x "$LIBUVC_BINARY" ]]; then
  echo "[theta-z1-run] theta_z1_uvc_stdout is missing; build it first with the README/libuvc setup steps." >&2
fi

IMU_ARGS=()
if [[ -n "$IMU_UDP_LISTEN_PORT" && "$IMU_UDP_LISTEN_PORT" != "0" ]]; then
  IMU_ARGS+=(--imu-udp-listen-port "$IMU_UDP_LISTEN_PORT")
fi
if [[ -n "$IMU_COMMAND" ]]; then
  IMU_ARGS+=(--imu-command "$IMU_COMMAND")
fi
IMU_ARGS+=(--imu-max-rate-hz "$IMU_MAX_RATE_HZ")

python3 edge2_theta_z1_webrtc_sender.py \
  --signal "$SIGNAL_URL" \
  --libuvc-binary "$LIBUVC_BINARY" \
  --preset "$PRESET" \
  --device "$DEVICE" \
  --caps-mode "$CAPS_MODE" \
  --rtp-mtu "$RTP_MTU" \
  --queue-ms "$QUEUE_MS" \
  --queue-buffers "$QUEUE_BUFFERS" \
  --webrtc-latency-ms "$WEBRTC_LATENCY_MS" \
  --sdp-profile "$SDP_PROFILE" \
  "${IMU_ARGS[@]}"
