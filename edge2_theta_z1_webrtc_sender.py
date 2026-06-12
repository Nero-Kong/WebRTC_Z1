#!/usr/bin/env python3
import argparse
import asyncio
import glob
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time

import gi
import websockets

gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")

from gi.repository import GLib, Gst, GstSdp, GstWebRTC


PRESETS = {
    "z1-4k": {"width": 3840, "height": 1920, "fps": "30000/1001"},
    "z1-2k": {"width": 1920, "height": 960, "fps": "30000/1001"},
}


def run_text(command, timeout=8):
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        return result.stdout or ""
    except Exception:
        return ""


def parse_v4l2_device_blocks(text):
    blocks = []
    current_name = None
    current_devices = []
    for line in text.splitlines():
        if line and not line.startswith((" ", "\t")):
            if current_name or current_devices:
                blocks.append((current_name or "", current_devices))
            current_name = line.rstrip(":")
            current_devices = []
        else:
            match = re.search(r"(/dev/video\d+)", line)
            if match:
                current_devices.append(match.group(1))
    if current_name or current_devices:
        blocks.append((current_name or "", current_devices))
    return blocks


def device_supports_h264(device, width, height):
    text = run_text(["v4l2-ctl", "-d", device, "--list-formats-ext"], timeout=5)
    lowered = text.lower()
    if "h264" not in lowered and "h.264" not in lowered:
        return False
    return f"{width}x{height}" in lowered or (str(width) in lowered and str(height) in lowered)


def find_video_device(requested, width, height):
    if requested != "auto":
        return requested

    devices_text = run_text(["v4l2-ctl", "--list-devices"], timeout=5)
    blocks = parse_v4l2_device_blocks(devices_text)
    theta_candidates = []
    for name, devices in blocks:
        if re.search(r"theta|ricoh|z1", name, re.IGNORECASE):
            theta_candidates.extend(devices)

    for device in theta_candidates:
        if device_supports_h264(device, width, height):
            return device

    for device in sorted(glob.glob("/dev/video*")):
        if device_supports_h264(device, width, height):
            return device

    if theta_candidates:
        return theta_candidates[0]
    return "/dev/video0"


class ImuForwarder:
    """Forwards external IMU attitude/raw samples into the signaling channel."""

    def __init__(self, sender):
        self.sender = sender
        self.running = False
        self.threads = []
        self.socket = None
        self.process = None
        self.last_send_time = 0.0
        self.forwarded = 0
        self.dropped = 0
        self.last_log_time = 0.0

    def start(self):
        if self.running:
            return
        self.running = True
        if self.sender.args.imu_udp_listen_port > 0:
            thread = threading.Thread(target=self.udp_loop, daemon=True)
            thread.start()
            self.threads.append(thread)
        if self.sender.args.imu_command:
            thread = threading.Thread(target=self.command_loop, daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self):
        self.running = False
        if self.socket is not None:
            try:
                self.socket.close()
            except Exception:
                pass
            self.socket = None
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        self.threads = []

    def udp_loop(self):
        port = int(self.sender.args.imu_udp_listen_port)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", port))
            sock.settimeout(0.5)
            self.socket = sock
            self.sender.log(f"IMU UDP side-channel listening on udp://0.0.0.0:{port}")
            while self.running and self.sender.running:
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                source = f"edge2-udp:{addr[0]}"
                self.handle_text(data.decode("utf-8", errors="replace"), source)
        except Exception as exc:
            self.sender.log(f"IMU UDP side-channel failed: {exc}")

    def command_loop(self):
        command = self.sender.args.imu_command
        try:
            self.sender.log(f"starting IMU command: {command}")
            self.process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            threading.Thread(target=self.read_command_stderr, daemon=True).start()
            while self.running and self.sender.running and self.process.stdout:
                line = self.process.stdout.readline()
                if not line:
                    break
                self.handle_text(line, "edge2-command")
        except Exception as exc:
            self.sender.log(f"IMU command failed: {exc}")

    def read_command_stderr(self):
        while self.running and self.process and self.process.stderr:
            line = self.process.stderr.readline()
            if not line:
                break
            sys.stderr.write("[imu-command] " + line)
            sys.stderr.flush()

    def handle_text(self, text, source):
        for line in text.replace("\r", "\n").split("\n"):
            line = line.strip()
            if not line:
                continue
            message = self.parse_line(line, source)
            if message:
                self.maybe_send(message)

    def maybe_send(self, message):
        max_rate = max(1.0, float(self.sender.args.imu_max_rate_hz))
        now = time.time()
        if now - self.last_send_time < 1.0 / max_rate:
            self.dropped += 1
            return

        self.last_send_time = now
        self.forwarded += 1
        self.sender.send_json_threadsafe(message)
        if now - self.last_log_time > 5.0:
            self.last_log_time = now
            self.sender.log(
                f"IMU side-channel forwarded={self.forwarded} "
                f"dropped_rate_limit={self.dropped} last={message.get('type')}"
            )

    def parse_line(self, line, source):
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except Exception as exc:
                self.sender.log(f"ignored malformed IMU JSON: {exc}")
                return None
            return self.normalize_json(payload, source)

        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 5:
            return None

        kind = parts[0].upper()
        timestamp_us = self.parse_int(parts[1], int(time.time() * 1_000_000))
        if kind in {"Z1ATT", "Z1ATTITUDE", "ATT", "ATTITUDE"} and len(parts) >= 5:
            roll = self.parse_float(parts[2])
            pitch = self.parse_float(parts[3])
            yaw = self.parse_float(parts[4])
            if roll is None or pitch is None or yaw is None:
                return None
            return {
                "type": "imu-attitude",
                "source": source,
                "timestampUs": timestamp_us,
                "rollDeg": roll,
                "pitchDeg": pitch,
                "yawDeg": yaw,
            }

        if kind in {"Z1IMU", "IMU", "X5IMU"} and len(parts) >= 8:
            values = [self.parse_float(part) for part in parts[2:8]]
            if any(value is None for value in values):
                return None
            ax, ay, az, gx, gy, gz = values
            return {
                "type": "imu-sample",
                "source": source,
                "timestampUs": timestamp_us,
                "ax": ax,
                "ay": ay,
                "az": az,
                "gx": gx,
                "gy": gy,
                "gz": gz,
            }

        return None

    def normalize_json(self, payload, source):
        message_type = str(payload.get("type", "")).lower()
        timestamp_us = int(payload.get("timestampUs") or payload.get("timestamp") or time.time() * 1_000_000)
        output_source = payload.get("source") or source
        if "attitude" in message_type or message_type in {"att", "z1att"}:
            return {
                "type": "imu-attitude",
                "source": output_source,
                "timestampUs": timestamp_us,
                "rollDeg": float(payload.get("rollDeg", payload.get("roll", 0.0))),
                "pitchDeg": float(payload.get("pitchDeg", payload.get("pitch", 0.0))),
                "yawDeg": float(payload.get("yawDeg", payload.get("yaw", 0.0))),
            }

        if "imu" in message_type or all(key in payload for key in ("ax", "ay", "az")):
            return {
                "type": "imu-sample",
                "source": output_source,
                "timestampUs": timestamp_us,
                "ax": float(payload.get("ax", 0.0)),
                "ay": float(payload.get("ay", 0.0)),
                "az": float(payload.get("az", 0.0)),
                "gx": float(payload.get("gx", 0.0)),
                "gy": float(payload.get("gy", 0.0)),
                "gz": float(payload.get("gz", 0.0)),
            }

        return None

    @staticmethod
    def parse_float(value):
        try:
            return float(value)
        except Exception:
            return None

    @staticmethod
    def parse_int(value, fallback):
        try:
            return int(value)
        except Exception:
            return fallback


class ThetaZ1WebRtcSender:
    def __init__(self, args):
        self.args = args
        self.loop = None
        self.websocket = None
        self.pipeline = None
        self.webrtc = None
        self.mainloop = None
        self.process = None
        self.running = True
        self.answer_applied = False
        self.offer_in_flight = False
        self.last_bytes = 0
        self.last_buffers = 0
        self.bytes_seen = 0
        self.buffers_seen = 0
        self.last_time = time.time()
        self.stats_thread_started = False
        self.imu_forwarder = ImuForwarder(self)

    def log(self, message):
        print(f"[theta-z1-edge2] {message}", flush=True)

    def send_json_threadsafe(self, message):
        if not self.websocket or not self.loop:
            return
        asyncio.run_coroutine_threadsafe(self.websocket.send(json.dumps(message)), self.loop)

    def send_status(self, status, extra=None):
        message = {
            "type": "sender-status",
            "status": status,
            "preset": self.args.preset,
            "width": int(self.args.width),
            "height": int(self.args.height),
            "fpsText": self.args.fps,
        }
        if extra:
            message.update(extra)
        self.send_json_threadsafe(message)

    def apply_preset(self, preset_name):
        if preset_name not in PRESETS:
            raise ValueError(f"unsupported preset: {preset_name}")
        preset = PRESETS[preset_name]
        self.args.preset = preset_name
        self.args.width = preset["width"]
        self.args.height = preset["height"]
        self.args.fps = preset["fps"]
        self.log(
            f"selected native preset={self.args.preset} "
            f"{self.args.width}x{self.args.height}@{self.args.fps}"
        )

    def make_session_description(self, sdp_type, sdp_text):
        result, sdp_message = GstSdp.SDPMessage.new()
        if result != GstSdp.SDPResult.OK:
            raise RuntimeError("Failed to allocate SDP message")
        parse_result = GstSdp.sdp_message_parse_buffer(bytes(sdp_text, "utf-8"), sdp_message)
        if parse_result != GstSdp.SDPResult.OK:
            raise RuntimeError(f"Failed to parse SDP: {parse_result}")
        return GstWebRTC.WebRTCSessionDescription.new(sdp_type, sdp_message)

    def munged_offer_sdp(self, sdp_text):
        if self.args.sdp_profile == "compat":
            return sdp_text

        # Keep PLI/FIR keyframe feedback, but remove transport-wide congestion
        # feedback. The Z1 is already encoding a fixed UVC H.264 stream, so
        # there is no camera encoder bitrate for WebRTC to tune.
        output = []
        for raw_line in sdp_text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = raw_line.strip()
            lower = line.lower()
            if not line:
                continue
            if line.startswith("a=rtcp-fb:") and ("transport-cc" in lower or "goog-remb" in lower):
                continue
            if line.startswith("a=extmap:") and ("transport-wide-cc" in lower or "transport-cc" in lower):
                continue
            output.append(line)
        return "\r\n".join(output) + "\r\n"

    def set_optional_property(self, element, property_name, value):
        if element is None:
            return
        try:
            if element.find_property(property_name) is None:
                return
            element.set_property(property_name, value)
            self.log(f"{element.get_name()}.{property_name}={value}")
        except Exception as exc:
            self.log(f"could not set {element.get_name()}.{property_name}: {exc}")

    def caps_string(self):
        if self.args.caps_mode == "relaxed":
            return f"video/x-h264,width={self.args.width},height={self.args.height}"
        return (
            "video/x-h264,"
            f"width={self.args.width},height={self.args.height},framerate={self.args.fps}"
        )

    def build_pipeline(self):
        Gst.init(None)
        queue_ms = max(5, min(1000, int(self.args.queue_ms)))
        queue_buffers = max(1, min(120, int(self.args.queue_buffers)))
        queue_time_ns = queue_ms * 1_000_000
        mtu = max(900, min(1400, int(self.args.rtp_mtu)))
        if self.args.source == "libuvc":
            if not self.process or not self.process.stdout:
                raise RuntimeError("libuvc source requires theta_z1_uvc_stdout to be running first")
            source_fd = self.process.stdout.fileno()
            source_description = (
                f"fdsrc name=source fd={source_fd} do-timestamp=true blocksize=32768 "
                "! video/x-h264,stream-format=byte-stream "
                "! identity name=source_stats signal-handoffs=true silent=true "
            )
            self.log("source=libuvc-theta stdout")
        else:
            device = find_video_device(self.args.device, self.args.width, self.args.height)
            self.args.device = device
            caps = self.caps_string()
            source_description = (
                f"v4l2src name=source device={device} do-timestamp=true io-mode={self.args.io_mode} "
                f"! {caps} "
                "! identity name=source_stats signal-handoffs=true silent=true "
            )
            self.log(f"source=v4l2 device={device} caps={caps}")

        pipeline_description = (
            "webrtcbin name=webrtc bundle-policy=max-bundle "
            f"{source_description}"
            "! h264parse config-interval=1 "
            f"! queue max-size-time={queue_time_ns} max-size-buffers={queue_buffers} max-size-bytes=0 leaky=downstream "
            f"! rtph264pay pt=96 config-interval=1 aggregate-mode=zero-latency mtu={mtu} "
            "! application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000 "
            "! webrtc."
        )
        self.log(f"GStreamer pipeline: {pipeline_description}")
        self.pipeline = Gst.parse_launch(pipeline_description)
        self.webrtc = self.pipeline.get_by_name("webrtc")
        source_stats = self.pipeline.get_by_name("source_stats")
        if self.webrtc is None:
            raise RuntimeError("Failed to create webrtcbin")
        if source_stats is not None:
            source_stats.connect("handoff", self.on_source_handoff)

        self.set_optional_property(self.webrtc, "latency", max(0, int(self.args.webrtc_latency_ms)))
        self.webrtc.connect("on-negotiation-needed", self.on_negotiation_needed)
        self.webrtc.connect("on-ice-candidate", self.on_ice_candidate)
        self.webrtc.connect("notify::ice-connection-state", self.on_webrtc_state_changed)
        self.webrtc.connect("notify::connection-state", self.on_webrtc_state_changed)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.on_bus_message)

    def on_source_handoff(self, _identity, buffer, *_args):
        self.bytes_seen += buffer.get_size()
        self.buffers_seen += 1

    def on_bus_message(self, _bus, message):
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            self.log(f"GStreamer error: {error}; debug={debug}")
            self.stop_media()
        elif message.type == Gst.MessageType.WARNING:
            warning, debug = message.parse_warning()
            self.log(f"GStreamer warning: {warning}; debug={debug}")
        elif message.type == Gst.MessageType.EOS:
            self.log("GStreamer EOS")
            self.stop_media()

    def on_negotiation_needed(self, _element):
        if self.offer_in_flight or self.answer_applied:
            return
        self.offer_in_flight = True
        self.log("negotiation needed")
        promise = Gst.Promise.new_with_change_func(self.on_offer_created, None)
        self.webrtc.emit("create-offer", None, promise)

    def on_offer_created(self, promise, _user_data):
        reply = promise.get_reply()
        offer = reply.get_value("offer")
        original_sdp = offer.sdp.as_text()
        sdp = self.munged_offer_sdp(original_sdp)
        if sdp != original_sdp:
            try:
                offer = self.make_session_description(GstWebRTC.WebRTCSDPType.OFFER, sdp)
            except Exception as exc:
                self.log(f"SDP munging failed; using original offer: {exc}")
                sdp = original_sdp

        try:
            with open("/tmp/theta_z1_webrtc_last_offer.sdp", "w", encoding="utf-8") as handle:
                handle.write(sdp)
        except Exception:
            pass

        for line in sdp.splitlines():
            if line.startswith("m=video") or "H264" in line or line.startswith("a=fmtp:"):
                self.log(f"offer SDP: {line}")

        self.webrtc.emit("set-local-description", offer, Gst.Promise.new())
        self.send_json_threadsafe({"type": "offer", "sdp": sdp})
        self.log("sent SDP offer")

    def on_ice_candidate(self, _element, mlineindex, candidate):
        self.send_json_threadsafe({
            "type": "candidate",
            "sdpMLineIndex": int(mlineindex),
            "sdpMid": "video0",
            "candidate": candidate,
        })

    def on_webrtc_state_changed(self, element, pspec):
        try:
            value = element.get_property(pspec.name)
        except Exception as exc:
            self.log(f"{pspec.name} changed but could not be read: {exc}")
            return
        self.log(f"{pspec.name}={value.value_nick if hasattr(value, 'value_nick') else value}")

    def set_answer(self, sdp_text):
        if self.answer_applied:
            self.log("ignoring duplicate browser SDP answer")
            return
        answer = self.make_session_description(GstWebRTC.WebRTCSDPType.ANSWER, sdp_text)
        self.webrtc.emit("set-remote-description", answer, Gst.Promise.new())
        self.answer_applied = True
        self.offer_in_flight = False
        self.log("applied browser SDP answer")

    def add_candidate(self, message):
        candidate = message.get("candidate")
        if candidate:
            self.webrtc.emit("add-ice-candidate", int(message.get("sdpMLineIndex", 0)), candidate)

    def start_media(self):
        self.stop_media()
        self.answer_applied = False
        self.offer_in_flight = False
        self.bytes_seen = 0
        self.buffers_seen = 0
        self.last_bytes = 0
        self.last_buffers = 0
        self.last_time = time.time()
        self.send_status("starting")

        if self.args.source == "libuvc":
            self.start_libuvc_process()
        self.build_pipeline()
        self.pipeline.set_state(Gst.State.PLAYING)
        self.send_status("started")
        self.mainloop = GLib.MainLoop()
        threading.Thread(target=self.mainloop.run, daemon=True).start()
        if not self.stats_thread_started:
            self.stats_thread_started = True
            threading.Thread(target=self.log_stats_loop, daemon=True).start()

    def switch_preset(self, preset_name):
        if preset_name not in PRESETS:
            self.log(f"ignoring unsupported preset switch request: {preset_name}")
            self.send_status("switch-rejected", {"message": f"unsupported preset: {preset_name}"})
            return

        if preset_name == self.args.preset and self.pipeline:
            self.log(f"preset already active: {preset_name}")
            self.send_status("already-active")
            return

        self.log(f"switching native preset to {preset_name}")
        self.send_status("switching", {"preset": preset_name})
        self.stop_media()
        self.apply_preset(preset_name)
        self.start_media()

    def stop_media(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
        if self.mainloop:
            self.mainloop.quit()
        self.pipeline = None
        self.webrtc = None
        self.mainloop = None
        self.answer_applied = False
        self.offer_in_flight = False

    def start_libuvc_process(self):
        binary = os.path.abspath(self.args.libuvc_binary)
        if not os.path.exists(binary):
            raise RuntimeError(f"libuvc stdout bridge not found: {binary}")
        cmd = [binary, "--mode", self.args.preset]
        self.log("starting libuvc bridge: " + " ".join(cmd))
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        threading.Thread(target=self.read_process_stderr, daemon=True).start()

    def read_process_stderr(self):
        while self.running and self.process and self.process.stderr:
            line = self.process.stderr.readline()
            if not line:
                break
            sys.stderr.write(line.decode("utf-8", errors="replace"))
            sys.stderr.flush()

    def log_stats_loop(self):
        while self.running:
            time.sleep(1)
            now = time.time()
            delta_seconds = max(0.001, now - self.last_time)
            delta_bytes = self.bytes_seen - self.last_bytes
            delta_buffers = self.buffers_seen - self.last_buffers
            mbps = delta_bytes * 8 / delta_seconds / 1_000_000
            fps = delta_buffers / delta_seconds
            self.last_bytes = self.bytes_seen
            self.last_buffers = self.buffers_seen
            self.last_time = now
            if self.pipeline:
                self.log(
                    f"source_h264_bitrate={mbps:.2f}Mbps "
                    f"source_buffers_per_sec={fps:.1f} "
                    f"total={self.bytes_seen / (1024 * 1024):.1f}MiB"
                )

    async def handle_signaling_once(self):
        self.loop = asyncio.get_running_loop()
        async with websockets.connect(self.args.signal) as websocket:
            self.websocket = websocket
            await websocket.send(json.dumps({"type": "register", "role": "sender"}))
            self.log(f"connected signaling: {self.args.signal}")
            self.log("waiting for viewer-ready before opening the Z1 UVC stream")
            self.imu_forwarder.start()

            async for raw in websocket:
                message = json.loads(raw)
                message_type = message.get("type")
                self.log(f"received signaling message: {message_type}")
                if message_type == "viewer-ready":
                    self.start_media()
                elif message_type == "switch-preset":
                    self.switch_preset(message.get("preset", ""))
                elif message_type == "answer":
                    if self.webrtc:
                        GLib.idle_add(lambda sdp=message["sdp"]: (self.set_answer(sdp), False)[1])
                elif message_type == "candidate":
                    if self.webrtc:
                        GLib.idle_add(lambda m=message: (self.add_candidate(m), False)[1])

    async def handle_signaling(self):
        attempt = 0
        while self.running:
            try:
                await self.handle_signaling_once()
            except Exception as exc:
                self.log(f"signaling/session error: {type(exc).__name__}: {exc!r}")
            self.stop_media()
            self.websocket = None
            if not self.running:
                break
            attempt += 1
            delay = min(self.args.reconnect_max_delay, self.args.reconnect_delay * attempt)
            self.log(f"reconnecting after {delay:.1f}s")
            await asyncio.sleep(delay)

    def stop(self):
        self.running = False
        self.imu_forwarder.stop()
        self.stop_media()


def print_probe(width, height, libuvc_binary):
    print("=== v4l2 devices ===")
    print(run_text(["v4l2-ctl", "--list-devices"], timeout=5).rstrip())
    print()
    for device in sorted(glob.glob("/dev/video*")):
        print(f"=== {device} formats ===")
        print(run_text(["v4l2-ctl", "-d", device, "--list-formats-ext"], timeout=5).rstrip())
        print()
    print(f"auto-selected: {find_video_device('auto', width, height)}")
    if os.path.exists(libuvc_binary):
        print()
        print("=== libuvc-theta devices ===")
        result = subprocess.run(
            [libuvc_binary, "--list"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=8,
        )
        print(result.stdout.rstrip())
    else:
        print()
        print(f"libuvc bridge not found: {libuvc_binary}")


def parse_args():
    parser = argparse.ArgumentParser(description="Theta Z1 USB UVC H.264 to WebRTC sender for Edge2.")
    parser.add_argument("--signal", required=True, help="WebSocket URL, for example ws://192.168.68.50:8765")
    parser.add_argument("--source", choices=["libuvc", "v4l2"], default="libuvc")
    parser.add_argument("--device", default="auto", help="UVC video device, or auto")
    parser.add_argument("--libuvc-binary", default="./theta_z1_uvc_stdout")
    parser.add_argument("--preset", choices=sorted(PRESETS.keys()), default="z1-4k")
    parser.add_argument("--width", type=int, default=0)
    parser.add_argument("--height", type=int, default=0)
    parser.add_argument("--fps", default="")
    parser.add_argument("--caps-mode", choices=["exact", "relaxed"], default="exact")
    parser.add_argument("--io-mode", type=int, default=2, help="GStreamer v4l2src io-mode. 2 is mmap.")
    parser.add_argument("--rtp-mtu", type=int, default=1200)
    parser.add_argument("--queue-ms", type=int, default=80)
    parser.add_argument("--queue-buffers", type=int, default=24)
    parser.add_argument("--webrtc-latency-ms", type=int, default=0)
    parser.add_argument("--sdp-profile", choices=["compat", "low-latency"], default="low-latency")
    parser.add_argument(
        "--imu-udp-listen-port",
        type=int,
        default=0,
        help="Optional Edge2 UDP port for IMU CSV/JSON input. 0 disables it.",
    )
    parser.add_argument(
        "--imu-command",
        default="",
        help="Optional command that prints IMU CSV/JSON samples to stdout for forwarding over signaling.",
    )
    parser.add_argument(
        "--imu-max-rate-hz",
        type=float,
        default=60.0,
        help="Maximum IMU messages per second relayed to Unity.",
    )
    parser.add_argument("--reconnect-delay", type=float, default=1.5)
    parser.add_argument("--reconnect-max-delay", type=float, default=10.0)
    parser.add_argument("--probe", action="store_true", help="Print detected V4L2 devices/formats and exit.")
    args = parser.parse_args()

    preset = PRESETS[args.preset]
    if args.width <= 0:
        args.width = preset["width"]
    if args.height <= 0:
        args.height = preset["height"]
    if not args.fps:
        args.fps = preset["fps"]
    return args


def main():
    args = parse_args()
    if args.probe:
        print_probe(args.width, args.height, args.libuvc_binary)
        return

    sender = ThetaZ1WebRtcSender(args)

    def handle_signal(_signum, _frame):
        sender.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        asyncio.run(sender.handle_signaling())
    finally:
        sender.stop()


if __name__ == "__main__":
    main()
