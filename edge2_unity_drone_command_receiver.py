#!/usr/bin/env python3
import argparse
import json
import math
import socket
import sys
import time


def clamp(value, limit):
    limit = abs(float(limit))
    if not math.isfinite(value):
        return 0.0
    return max(-limit, min(limit, value))


def clean_zero(value):
    return 0.0 if abs(value) < 1e-9 else value


def number(payload, key, default=0.0):
    value = payload.get(key, default)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def parse_endpoint(value):
    if not value:
        return None
    if ":" not in value:
        raise ValueError("endpoint must be HOST:PORT")
    host, port_text = value.rsplit(":", 1)
    return host, int(port_text)


def now_s():
    return time.monotonic()


class UnityCommandReceiver:
    def __init__(self, args):
        self.args = args
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((args.listen_host, args.listen_port))
        self.socket.settimeout(0.02)

        self.forward_socket = None
        self.forward_endpoint = parse_endpoint(args.forward_udp)
        if self.forward_endpoint:
            self.forward_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        self.last_packet_time = None
        self.last_output_time = 0.0
        self.in_timeout = False
        self.packet_count = 0
        self.bad_packet_count = 0

    def log(self, message):
        timestamp = time.strftime("%H:%M:%S")
        print(f"{timestamp} [unity-drone-rx] {message}", flush=True)

    def run(self):
        self.log(
            f"listening udp://{self.args.listen_host}:{self.args.listen_port} "
            f"timeout={self.args.timeout_ms}ms "
            f"limits planar={self.args.max_planar_mps:.3f}m/s "
            f"vertical={self.args.max_vertical_mps:.3f}m/s "
            f"yaw={self.args.max_yaw_deg_s:.1f}deg/s"
        )

        if self.forward_endpoint:
            self.log(f"forwarding sanitized JSON to udp://{self.forward_endpoint[0]}:{self.forward_endpoint[1]}")

        try:
            while True:
                self.poll_once()
                self.check_timeout()
        except KeyboardInterrupt:
            self.log("stopped by user")

    def poll_once(self):
        try:
            data, address = self.socket.recvfrom(65535)
        except socket.timeout:
            return

        receive_time = now_s()
        self.last_packet_time = receive_time
        self.in_timeout = False

        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception as exc:
            self.bad_packet_count += 1
            self.emit_hover("bad-json", address, error=str(exc))
            return

        self.packet_count += 1
        command = self.sanitize(payload, address, receive_time)
        self.emit(command)

    def sanitize(self, payload, address, receive_time):
        valid = bool(payload.get("valid", False))
        force_hover = self.args.require_valid and not valid

        up_mps = number(payload, "up_mps", 0.0)
        down_mps = number(payload, "down_mps", -up_mps)

        forward_frd = 0.0 if force_hover else clamp(number(payload, "forward_mps"), self.args.max_planar_mps)
        right_frd = 0.0 if force_hover else clamp(number(payload, "right_mps"), self.args.max_planar_mps)
        down_frd = 0.0 if force_hover else clamp(down_mps, self.args.max_vertical_mps)
        yaw_frd_deg_s = 0.0 if force_hover else clamp(number(payload, "yaw_deg_s"), self.args.max_yaw_deg_s)

        command = {
            "type": "adaptivefly_drone_control_sanitized",
            "source": f"{address[0]}:{address[1]}",
            "received_time_s": time.time(),
            "age_ms": 0.0,
            "input_seq": payload.get("seq"),
            "input_valid": valid,
            "has_body_anchor": bool(payload.get("has_body_anchor", False)),
            "using_hmd_fallback": bool(payload.get("using_hmd_fallback", False)),
            "state": "active" if not force_hover else "hover",
            "reason": "valid" if not force_hover else "invalid-input",
            "frame": "body_flu",
            "x_mps": clean_zero(forward_frd),
            "y_mps": clean_zero(-right_frd),
            "z_mps": clean_zero(-down_frd),
            "yaw_deg_s": clean_zero(-yaw_frd_deg_s),
            "input_frame": "body_frd",
            "input_forward_mps": forward_frd,
            "input_right_mps": right_frd,
            "input_down_mps": down_frd,
            "input_yaw_deg_s": yaw_frd_deg_s,
        }
        command["yaw_rad_s"] = math.radians(command["yaw_deg_s"])
        command["packets_received"] = self.packet_count
        command["bad_packets"] = self.bad_packet_count
        command["_received_monotonic"] = receive_time
        return command

    def check_timeout(self):
        if self.last_packet_time is None:
            return

        age_ms = (now_s() - self.last_packet_time) * 1000.0
        if age_ms < self.args.timeout_ms:
            return

        should_repeat_hover = self.args.print_json or self.forward_socket is not None
        if self.in_timeout and not should_repeat_hover:
            return

        if self.in_timeout and (now_s() - self.last_output_time) < self.output_interval_s():
            return

        self.in_timeout = True
        command = self.hover_command("timeout", age_ms=age_ms)
        self.emit(command)

    def emit_hover(self, reason, address=None, error=None):
        command = self.hover_command(reason, address=address, error=error)
        self.emit(command)

    def hover_command(self, reason, age_ms=0.0, address=None, error=None):
        command = {
            "type": "adaptivefly_drone_control_sanitized",
            "source": f"{address[0]}:{address[1]}" if address else "",
            "received_time_s": time.time(),
            "age_ms": age_ms,
            "input_seq": None,
            "input_valid": False,
            "has_body_anchor": False,
            "using_hmd_fallback": False,
            "state": "hover",
            "reason": reason,
            "frame": "body_flu",
            "x_mps": 0.0,
            "y_mps": 0.0,
            "z_mps": 0.0,
            "yaw_deg_s": 0.0,
            "yaw_rad_s": 0.0,
            "input_frame": "body_frd",
            "input_forward_mps": 0.0,
            "input_right_mps": 0.0,
            "input_down_mps": 0.0,
            "input_yaw_deg_s": 0.0,
            "packets_received": self.packet_count,
            "bad_packets": self.bad_packet_count,
        }
        if error:
            command["error"] = error
        return command

    def emit(self, command):
        command.pop("_received_monotonic", None)
        self.last_output_time = now_s()

        if self.forward_socket and self.forward_endpoint:
            data = json.dumps(command, separators=(",", ":")).encode("utf-8")
            self.forward_socket.sendto(data, self.forward_endpoint)

        if self.args.print_json:
            print(json.dumps(command, separators=(",", ":"), sort_keys=True), flush=True)
            return

        if not self.should_log(command):
            return

        self.log(
            f"state={command['state']} reason={command['reason']} "
            f"seq={command.get('input_seq')} valid={command.get('input_valid')} "
            f"x={command['x_mps']:+.3f} y={command['y_mps']:+.3f} "
            f"z={command['z_mps']:+.3f} yaw={command['yaw_deg_s']:+.1f} "
            f"anchor={command['has_body_anchor']} fallback={command['using_hmd_fallback']} "
            f"age={command['age_ms']:.0f}ms"
        )

    def should_log(self, command):
        if command["state"] != "active":
            return True
        return self.packet_count <= 3 or (self.args.log_every > 0 and self.packet_count % self.args.log_every == 0)

    def output_interval_s(self):
        return 1.0 / max(1.0, self.args.output_rate_hz)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Receive AdaptiveFly Unity UDP commands and emit sanitized body-frame drone velocity setpoints."
    )
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, default=14560)
    parser.add_argument("--timeout-ms", type=float, default=300.0)
    parser.add_argument("--max-planar-mps", type=float, default=0.5)
    parser.add_argument("--max-vertical-mps", type=float, default=0.5)
    parser.add_argument("--max-yaw-deg-s", type=float, default=30.0)
    parser.add_argument("--output-rate-hz", type=float, default=30.0)
    parser.add_argument("--log-every", type=int, default=30, help="Log every N active packets; non-active packets always log.")
    parser.add_argument("--require-valid", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--print-json", action="store_true", help="Print every sanitized command as JSON.")
    parser.add_argument("--forward-udp", default="", help="Optional HOST:PORT for sanitized JSON forwarding.")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.listen_port <= 0 or args.listen_port > 65535:
        print("listen port must be 1..65535", file=sys.stderr)
        return 2

    receiver = UnityCommandReceiver(args)
    receiver.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
