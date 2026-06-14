#!/usr/bin/env python3
import argparse
import json
import math
import socket
import sys
import time


UNITY_TO_FLU = (
    (0.0, 0.0, 1.0),
    (-1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
)


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


def unity_vector_to_flu(x, y, z):
    return z, -x, y


def matmul(a, b):
    return tuple(
        tuple(sum(a[row][k] * b[k][col] for k in range(3)) for col in range(3))
        for row in range(3)
    )


def transpose(m):
    return tuple(tuple(m[col][row] for col in range(3)) for row in range(3))


def normalize_quat(qx, qy, qz, qw):
    norm = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    if norm <= 1e-9 or not math.isfinite(norm):
        return 0.0, 0.0, 0.0, 1.0
    return qx / norm, qy / norm, qz / norm, qw / norm


def quat_to_matrix(qx, qy, qz, qw):
    qx, qy, qz, qw = normalize_quat(qx, qy, qz, qw)
    xx = qx * qx
    yy = qy * qy
    zz = qz * qz
    xy = qx * qy
    xz = qx * qz
    yz = qy * qz
    wx = qw * qx
    wy = qw * qy
    wz = qw * qz
    return (
        (1.0 - 2.0 * (yy + zz), 2.0 * (xy - wz), 2.0 * (xz + wy)),
        (2.0 * (xy + wz), 1.0 - 2.0 * (xx + zz), 2.0 * (yz - wx)),
        (2.0 * (xz - wy), 2.0 * (yz + wx), 1.0 - 2.0 * (xx + yy)),
    )


def matrix_to_quat(m):
    trace = m[0][0] + m[1][1] + m[2][2]
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        qw = 0.25 * s
        qx = (m[2][1] - m[1][2]) / s
        qy = (m[0][2] - m[2][0]) / s
        qz = (m[1][0] - m[0][1]) / s
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2.0
        qw = (m[2][1] - m[1][2]) / s
        qx = 0.25 * s
        qy = (m[0][1] + m[1][0]) / s
        qz = (m[0][2] + m[2][0]) / s
    elif m[1][1] > m[2][2]:
        s = math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2.0
        qw = (m[0][2] - m[2][0]) / s
        qx = (m[0][1] + m[1][0]) / s
        qy = 0.25 * s
        qz = (m[1][2] + m[2][1]) / s
    else:
        s = math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2.0
        qw = (m[1][0] - m[0][1]) / s
        qx = (m[0][2] + m[2][0]) / s
        qy = (m[1][2] + m[2][1]) / s
        qz = 0.25 * s
    return normalize_quat(qx, qy, qz, qw)


def unity_quat_to_flu(qx, qy, qz, qw):
    unity_matrix = quat_to_matrix(qx, qy, qz, qw)
    flu_matrix = matmul(matmul(UNITY_TO_FLU, unity_matrix), transpose(UNITY_TO_FLU))
    return matrix_to_quat(flu_matrix)


def has_any(payload, keys):
    return any(key in payload for key in keys)


class UnityArmCommandReceiver:
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
        print(f"{timestamp} [unity-arm-rx] {message}", flush=True)

    def run(self):
        self.log(
            f"listening udp://{self.args.listen_host}:{self.args.listen_port} "
            f"timeout={self.args.timeout_ms}ms "
            f"max_linear={self.args.max_linear_mps:.3f}m/s "
            f"max_position={self.args.max_position_m:.3f}m "
            f"max_angular={self.args.max_angular_rad_s:.3f}rad/s"
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
            self.emit(self.hold_command("bad-json", address=address, error=str(exc)))
            return

        self.packet_count += 1
        self.emit(self.sanitize(payload, address, receive_time))

    def sanitize(self, payload, address, receive_time):
        valid = bool(payload.get("valid", False))
        if self.args.require_valid and not valid:
            return self.hold_command("invalid-input", address=address, payload=payload)

        mode = self.command_mode(payload)
        if mode == "pose":
            return self.sanitize_pose(payload, address, receive_time, valid)
        return self.sanitize_twist(payload, address, receive_time, valid)

    def command_mode(self, payload):
        if self.args.command_mode != "auto":
            return self.args.command_mode

        message_type = str(payload.get("type", "")).lower()
        if "pose" in message_type or has_any(payload, ("qx", "qw", "qx_unity", "qw_unity")):
            return "pose"
        return "twist"

    def input_is_unity_frame(self, payload):
        frame = str(payload.get("frame", "")).lower()
        return "unity" in frame or has_any(
            payload,
            (
                "x_unity_m",
                "vx_unity_mps",
                "qx_unity",
                "wx_unity_rad_s",
            ),
        )

    def sanitize_twist(self, payload, address, receive_time, valid):
        unity_input = self.input_is_unity_frame(payload)
        if unity_input:
            vx, vy, vz = unity_vector_to_flu(
                number(payload, "vx_unity_mps"),
                number(payload, "vy_unity_mps"),
                number(payload, "vz_unity_mps"),
            )
            wx, wy, wz = unity_vector_to_flu(
                number(payload, "wx_unity_rad_s"),
                number(payload, "wy_unity_rad_s"),
                number(payload, "wz_unity_rad_s"),
            )
            input_frame = str(payload.get("frame", "unity"))
        else:
            vx = number(payload, "vx_mps", number(payload, "x_mps"))
            vy = number(payload, "vy_mps", number(payload, "y_mps"))
            vz = number(payload, "vz_mps", number(payload, "z_mps"))
            wx = number(payload, "wx_rad_s")
            wy = number(payload, "wy_rad_s")
            wz = number(payload, "wz_rad_s", number(payload, "yaw_rad_s"))
            input_frame = str(payload.get("frame", "base_flu"))

        command = self.base_command("unity_arm_ee_twist_sanitized", "twist", payload, address, receive_time, valid)
        command.update(
            {
                "frame": "base_flu",
                "vx_mps": clean_zero(clamp(vx, self.args.max_linear_mps)),
                "vy_mps": clean_zero(clamp(vy, self.args.max_linear_mps)),
                "vz_mps": clean_zero(clamp(vz, self.args.max_linear_mps)),
                "wx_rad_s": clean_zero(clamp(wx, self.args.max_angular_rad_s)),
                "wy_rad_s": clean_zero(clamp(wy, self.args.max_angular_rad_s)),
                "wz_rad_s": clean_zero(clamp(wz, self.args.max_angular_rad_s)),
                "input_frame": input_frame,
            }
        )
        return command

    def sanitize_pose(self, payload, address, receive_time, valid):
        unity_input = self.input_is_unity_frame(payload)
        if unity_input:
            x, y, z = unity_vector_to_flu(
                number(payload, "x_unity_m"),
                number(payload, "y_unity_m"),
                number(payload, "z_unity_m"),
            )
            qx, qy, qz, qw = unity_quat_to_flu(
                number(payload, "qx_unity"),
                number(payload, "qy_unity"),
                number(payload, "qz_unity"),
                number(payload, "qw_unity", 1.0),
            )
            input_frame = str(payload.get("frame", "unity"))
        else:
            x = number(payload, "x_m")
            y = number(payload, "y_m")
            z = number(payload, "z_m")
            qx, qy, qz, qw = normalize_quat(
                number(payload, "qx"),
                number(payload, "qy"),
                number(payload, "qz"),
                number(payload, "qw", 1.0),
            )
            input_frame = str(payload.get("frame", "base_flu"))

        command = self.base_command("unity_arm_ee_pose_sanitized", "pose", payload, address, receive_time, valid)
        command.update(
            {
                "frame": "base_flu",
                "x_m": clean_zero(clamp(x, self.args.max_position_m)),
                "y_m": clean_zero(clamp(y, self.args.max_position_m)),
                "z_m": clean_zero(clamp(z, self.args.max_position_m)),
                "qx": clean_zero(qx),
                "qy": clean_zero(qy),
                "qz": clean_zero(qz),
                "qw": clean_zero(qw),
                "input_frame": input_frame,
            }
        )
        return command

    def base_command(self, message_type, mode, payload, address, receive_time, valid):
        return {
            "type": message_type,
            "target": "arm_end_effector",
            "mode": mode,
            "source": f"{address[0]}:{address[1]}",
            "received_time_s": time.time(),
            "age_ms": 0.0,
            "input_seq": payload.get("seq"),
            "input_valid": valid,
            "state": "active",
            "reason": "valid",
            "packets_received": self.packet_count,
            "bad_packets": self.bad_packet_count,
            "_received_monotonic": receive_time,
        }

    def check_timeout(self):
        if self.last_packet_time is None:
            return

        age_ms = (now_s() - self.last_packet_time) * 1000.0
        if age_ms < self.args.timeout_ms:
            return

        should_repeat_hold = self.args.print_json or self.forward_socket is not None
        if self.in_timeout and not should_repeat_hold:
            return

        if self.in_timeout and (now_s() - self.last_output_time) < self.output_interval_s():
            return

        self.in_timeout = True
        self.emit(self.hold_command("timeout", age_ms=age_ms))

    def hold_command(self, reason, age_ms=0.0, address=None, payload=None, error=None):
        command = {
            "type": "unity_arm_ee_hold_sanitized",
            "target": "arm_end_effector",
            "mode": "hold",
            "source": f"{address[0]}:{address[1]}" if address else "",
            "received_time_s": time.time(),
            "age_ms": age_ms,
            "input_seq": payload.get("seq") if payload else None,
            "input_valid": bool(payload.get("valid", False)) if payload else False,
            "state": "hold",
            "reason": reason,
            "frame": "base_flu",
            "vx_mps": 0.0,
            "vy_mps": 0.0,
            "vz_mps": 0.0,
            "wx_rad_s": 0.0,
            "wy_rad_s": 0.0,
            "wz_rad_s": 0.0,
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

        if command["mode"] == "pose":
            self.log(
                f"state={command['state']} reason={command['reason']} "
                f"seq={command.get('input_seq')} valid={command.get('input_valid')} "
                f"x={command['x_m']:+.3f} y={command['y_m']:+.3f} z={command['z_m']:+.3f} "
                f"q=({command['qx']:+.3f},{command['qy']:+.3f},{command['qz']:+.3f},{command['qw']:+.3f}) "
                f"age={command['age_ms']:.0f}ms"
            )
        else:
            self.log(
                f"state={command['state']} reason={command['reason']} "
                f"seq={command.get('input_seq')} valid={command.get('input_valid')} "
                f"vx={command['vx_mps']:+.3f} vy={command['vy_mps']:+.3f} vz={command['vz_mps']:+.3f} "
                f"wx={command['wx_rad_s']:+.3f} wy={command['wy_rad_s']:+.3f} wz={command['wz_rad_s']:+.3f} "
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
        description="Receive Unity UDP commands and emit sanitized FLU robotic-arm end-effector setpoints."
    )
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, default=14561)
    parser.add_argument("--timeout-ms", type=float, default=300.0)
    parser.add_argument("--max-linear-mps", type=float, default=0.5)
    parser.add_argument("--max-position-m", type=float, default=0.5)
    parser.add_argument("--max-angular-rad-s", type=float, default=1.0)
    parser.add_argument("--output-rate-hz", type=float, default=30.0)
    parser.add_argument("--command-mode", choices=("auto", "twist", "pose"), default="auto")
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

    receiver = UnityArmCommandReceiver(args)
    receiver.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
