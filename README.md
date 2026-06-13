# WebRTC_Z1

Low-latency RICOH THETA Z1 USB live-streaming sender for a Khadas Edge2, plus a minimal PC-side WebSocket signaling relay for a Unity WebRTC viewer.

The Edge2 sender reads the THETA Z1 H.264 UVC stream through `libuvc-theta`, packetizes it with GStreamer, and publishes it through `webrtcbin`. The Unity side is the viewer. The sender waits for a `viewer-ready` signaling message before opening the camera.

## Repository Layout

```text
edge2_theta_z1_webrtc_sender.py      Edge2 WebRTC sender
edge2_run_theta_z1_webrtc_sender.sh  Edge2 launcher
edge2_probe_theta_z1.sh              Edge2 camera/GStreamer probe helper
edge2_unity_command_receiver.py      Edge2 UDP receiver for Unity AdaptiveFly drone commands
edge2_mavlink_to_z1att.py            Optional MAVLink ATTITUDE to Z1 IMU side-channel helper
theta_z1_uvc_stdout.c                THETA Z1 H.264 stdout bridge
thetauvc.c / thetauvc.h              THETA UVC helper code
theta_z1_uvc_stdout.mk               Build rules for the stdout bridge
bin/theta_z1_uvc_stdout              Prebuilt aarch64 binary tested on Edge2
tools/theta_z1_signal_server.py      PC-side WebSocket signaling relay
```

## Tested Hardware

- Khadas Edge2 running Linux aarch64
- RICOH THETA Z1 over USB in live-streaming/UVC mode
- Windows PC running Unity WebRTC viewer
- Edge2 and PC on the same LAN

The current tested 2K stream is `1920x960` at about `44 Mbps` source H.264 bitrate. 4K is supported but is much heavier.

## Edge2 System Setup

Install base packages:

```bash
sudo apt update
sudo apt install -y \
  git build-essential pkg-config cmake libusb-1.0-0-dev usbutils v4l-utils \
  python3 python3-pip python3-gi python3-websockets \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gir1.2-gst-plugins-bad-1.0 \
  libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
```

Install RICOH's THETA UVC library:

```bash
cd ~
git clone https://github.com/ricohapi/libuvc-theta.git
cd libuvc-theta
mkdir -p build
cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
make -j"$(nproc)"
sudo make install
sudo ldconfig
```

Clone this repository on the Edge2:

```bash
cd ~
git clone https://github.com/Nero-Kong/WebRTC_Z1.git
cd WebRTC_Z1
chmod +x edge2_run_theta_z1_webrtc_sender.sh edge2_probe_theta_z1.sh edge2_theta_z1_webrtc_sender.py edge2_mavlink_to_z1att.py
chmod +x bin/theta_z1_uvc_stdout
```

The repository includes a tested aarch64 bridge binary at `bin/theta_z1_uvc_stdout`. To rebuild it locally instead:

```bash
cd ~/WebRTC_Z1
make clean
make
```

The launcher uses `./theta_z1_uvc_stdout` if it exists. Otherwise it falls back to `./bin/theta_z1_uvc_stdout`. You can override the path with:

```bash
export THETA_Z1_UVC_BINARY=/path/to/theta_z1_uvc_stdout
```

## Edge2 Validation

Plug the THETA Z1 into the Edge2, put it in live-streaming/UVC mode, then run:

```bash
cd ~/WebRTC_Z1
./edge2_probe_theta_z1.sh
```

Expected signs:

- `lsusb` shows a RICOH/THETA device.
- GStreamer tools are available.
- V4L2 probing may show devices, but the default sender path uses `libuvc-theta`.

Check Python and GStreamer WebRTC dependencies:

```bash
python3 - <<'PY'
import gi
import websockets
gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
from gi.repository import Gst, GstWebRTC, GstSdp
Gst.init(None)
print("Python/GStreamer/WebRTC imports OK")
PY

gst-inspect-1.0 webrtcbin h264parse rtph264pay fdsrc queue
```

Test the camera bridge directly:

```bash
cd ~/WebRTC_Z1
timeout 6s ./bin/theta_z1_uvc_stdout --mode z1-2k > /tmp/theta_z1_2k.h264
ls -lh /tmp/theta_z1_2k.h264
```

The timeout exit code is expected. The output file should grow to many MB.

## PC Signaling Server

On the PC that runs Unity, install Python dependency once:

```powershell
python -m pip install --user -r requirements-pc.txt
```

Start the signaling relay:

```powershell
python tools\theta_z1_signal_server.py --host 0.0.0.0 --port 8765
```

If Windows Firewall asks, allow Python on the current network.

Unity's THETA Z1 WebRTC receiver should use:

```text
ws://127.0.0.1:8765
```

when the signaling server runs on the same PC as Unity.

## Start Streaming From Edge2

Find the PC LAN IP. On Windows:

```powershell
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -match '^192\.168\.' } | Select-Object IPAddress
```

Start a conservative 2K stream from the Edge2:

```bash
cd ~/WebRTC_Z1
./edge2_run_theta_z1_webrtc_sender.sh ws://<PC_IP>:8765 z1-2k auto exact yes
```

Example:

```bash
./edge2_run_theta_z1_webrtc_sender.sh ws://192.168.68.58:8765 z1-2k auto exact yes
```

Start a 4K stream:

```bash
./edge2_run_theta_z1_webrtc_sender.sh ws://<PC_IP>:8765 z1-4k auto exact yes
```

Arguments:

```text
1  signaling URL, for example ws://192.168.68.58:8765
2  preset: z1-4k or z1-2k
3  device: auto
4  caps mode: exact or relaxed
5  stop existing sender first: yes or no
6  RTP MTU, default 1200
7  queue time in ms, default 80
8  queue buffers, default 24
9  webrtcbin latency ms, default 0
10 SDP profile: low-latency or compat
11 optional IMU UDP listen port, default 0
12 optional IMU command
13 IMU max relay rate Hz, default 60
```

## Expected Logs

PC signaling relay:

```text
registered sender
registered viewer
sender -> viewer offer
viewer -> sender answer
sender -> viewer candidate
viewer -> sender candidate
```

Edge2 sender:

```text
connected signaling: ws://<PC_IP>:8765
waiting for viewer-ready before opening the Z1 UVC stream
received signaling message: viewer-ready
starting libuvc bridge: ... --mode z1-2k
sent SDP offer
applied browser SDP answer
ice-connection-state=connected
connection-state=connected
source_h264_bitrate=...
```

Unity receiver:

```text
Connected signaling: ws://127.0.0.1:8765
Registered as viewer; waiting for Edge2 offer
Offer summary ...
Answer sent to Edge2 sender.
ICE state: Connected
Peer state: Connected
First frame: 1920x960, texture=Texture2D
```

## Unity Drone Command Receiver

`edge2_unity_command_receiver.py` listens for AdaptiveFly Unity UDP JSON commands and emits sanitized body-frame velocity setpoints. Unity input uses body FRD fields; the sanitized output is body FLU so downstream PX4/ROS code can consume `x_mps`, `y_mps`, `z_mps`, and `yaw_rad_s` directly. It is intentionally conservative by default: it requires `valid=true`, clamps again on the Edge2, and outputs hover if no packet arrives within 300 ms.

Default receiver settings:

```text
listen: 0.0.0.0:14560
timeout: 300 ms
max forward/right: +/-0.3 m/s
max up/down: +/-0.2 m/s
max yaw: +/-30 deg/s
output frame: body_flu
```

Start it on the Edge2:

```bash
cd ~/WebRTC_Z1
python3 edge2_unity_command_receiver.py
```

For JSON output that another bridge can consume:

```bash
python3 edge2_unity_command_receiver.py --print-json
```

To forward sanitized JSON to another local bridge:

```bash
python3 edge2_unity_command_receiver.py --forward-udp 127.0.0.1:14600
```

Unity must send to the Edge2 IP, not `127.0.0.1`. In `AdaptiveFlyDroneCommandBroadcaster` set:

```text
destinationHost = <EDGE2_IP>
destinationPort = 14560
```

For the current test network the Edge2 IP was:

```text
192.168.68.57
```

The receiver consumes these Unity input fields:

```text
valid
has_body_anchor
using_hmd_fallback
forward_mps
right_mps
down_mps
yaw_deg_s
```

Unity input is body FRD:

```text
forward_mps  positive forward
right_mps    positive right
down_mps     positive down
yaw_deg_s    yaw-rate command in degrees/second
```

The sanitized output is body FLU:

```text
x_mps      positive forward
y_mps      positive left
z_mps      positive up
yaw_deg_s  positive about +Z/up
yaw_rad_s  positive about +Z/up
```

Conversion:

```text
x_mps = forward_mps
y_mps = -right_mps
z_mps = -down_mps
yaw_deg_s = -input_yaw_deg_s
```

For PX4/ROS bridges that only need four values, read:

```text
x_mps
y_mps
z_mps
yaw_rad_s
```

This script does not arm a vehicle or bypass flight-controller safety. A real MAVLink/ROS bridge should consume the sanitized output, enforce its own deadman/mode checks, and hover on timeout.

## Optional MAVLink IMU Side-Channel

`edge2_mavlink_to_z1att.py` can read MAVLink `ATTITUDE` messages and print Z1 attitude lines for the sender to relay to Unity:

```bash
python3 -m pip install --user pymavlink
python3 edge2_mavlink_to_z1att.py --connect udpin:0.0.0.0:14550 --rate-hz 60
```

To run it through the sender:

```bash
./edge2_run_theta_z1_webrtc_sender.sh \
  ws://<PC_IP>:8765 z1-2k auto exact yes 1200 80 24 0 low-latency 0 \
  "python3 edge2_mavlink_to_z1att.py --connect udpin:0.0.0.0:14550 --rate-hz 60"
```

## Troubleshooting

If the Edge2 log stays at `waiting for viewer-ready`, the Unity viewer did not connect to the same signaling server. Check Unity's URL and the PC firewall.

If Unity logs `Unity rejected remote ICE candidate`, make sure the sender includes `sdpMid: video0` in candidate messages. This repository includes that fix.

If Unity reaches `First frame` but the view looks black, the WebRTC link is working. Check the Unity sky dome material, camera direction, and whether another window is covering the Game view.

If `theta_z1_uvc_stdout` cannot find the camera, check the USB cable, THETA live-streaming mode, and permissions. Logging out and back in after group changes may be required:

```bash
sudo usermod -aG video,plugdev "$USER"
```

If GStreamer cannot find `webrtcbin`, install `gstreamer1.0-plugins-bad` and `gir1.2-gst-plugins-bad-1.0`.

## References

- RICOH libuvc-theta: https://github.com/ricohapi/libuvc-theta
- RICOH libuvc-theta sample: https://github.com/ricohapi/libuvc-theta-sample
