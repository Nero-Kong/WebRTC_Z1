#!/usr/bin/env python3
import argparse
import glob
import math
import os
import sys
import time


def parse_args():
    parser = argparse.ArgumentParser(
        description="Read MAVLink ATTITUDE and print Z1ATT lines for the THETA Z1 WebRTC IMU side-channel."
    )
    parser.add_argument(
        "--connect",
        default="auto",
        help="MAVLink connection string. Use auto, /dev/ttyACM0, /dev/ttyUSB0, udpin:0.0.0.0:14550, udp:host:14550, or tcp:127.0.0.1:5760.",
    )
    parser.add_argument("--baud", type=int, default=115200, help="Preferred serial baud rate.")
    parser.add_argument(
        "--extra-bauds",
        default="57600,921600,576000,230400",
        help="Additional serial baud rates to try when --connect auto is used.",
    )
    parser.add_argument("--rate-hz", type=float, default=60.0, help="Maximum Z1ATT output rate.")
    parser.add_argument("--auto-udp", default="udpin:0.0.0.0:14550", help="Fallback MAVLink UDP listener used when --connect auto finds no serial device.")
    parser.add_argument(
        "--serial-globs",
        default="/dev/serial/by-id/*,/dev/ttyACM*,/dev/ttyUSB*",
        help="Comma-separated serial device globs used by --connect auto.",
    )
    parser.add_argument("--probe-seconds", type=float, default=2.0, help="Heartbeat probe time for each auto serial candidate.")
    parser.add_argument("--wait-heartbeat", action="store_true", help="Wait for a MAVLink heartbeat before reading ATTITUDE.")
    parser.add_argument("--roll-scale", type=float, default=1.0, help="Multiply roll degrees before output.")
    parser.add_argument("--pitch-scale", type=float, default=1.0, help="Multiply pitch degrees before output.")
    parser.add_argument("--yaw-scale", type=float, default=1.0, help="Multiply yaw degrees before output.")
    parser.add_argument("--roll-offset", type=float, default=0.0, help="Add roll offset degrees after scaling.")
    parser.add_argument("--pitch-offset", type=float, default=0.0, help="Add pitch offset degrees after scaling.")
    parser.add_argument("--yaw-offset", type=float, default=0.0, help="Add yaw offset degrees after scaling.")
    parser.add_argument("--quiet", action="store_true", help="Suppress status logs on stderr.")
    return parser.parse_args()


def log(args, message):
    if not args.quiet:
        print(f"[mavlink-z1att] {message}", file=sys.stderr, flush=True)


def serial_candidates(args):
    candidates = []
    for pattern in [item.strip() for item in args.serial_globs.split(",") if item.strip()]:
        candidates.extend(glob.glob(pattern))
    resolved = []
    seen = set()
    for path in candidates:
        real = os.path.realpath(path)
        key = real if real.startswith("/dev/") else path
        if key in seen:
            continue
        seen.add(key)
        resolved.append(path)
    return resolved


def baud_candidates(args):
    values = [args.baud]
    for item in args.extra_bauds.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            values.append(int(item))
        except ValueError:
            log(args, f"ignoring invalid baud: {item}")

    ordered = []
    seen = set()
    for value in values:
        if value > 0 and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def open_connection(args, mavutil):
    if args.connect != "auto":
        log(args, f"connecting {args.connect} baud={args.baud}")
        return mavutil.mavlink_connection(args.connect, baud=args.baud, autoreconnect=True)

    candidates = serial_candidates(args)
    if candidates:
        log(args, "serial candidates: " + ", ".join(candidates))
    else:
        log(args, "no serial candidates found")

    for candidate in candidates:
        for baud in baud_candidates(args):
            try:
                log(args, f"probing {candidate} baud={baud}")
                master = mavutil.mavlink_connection(candidate, baud=baud, autoreconnect=True)
                probe = master.recv_match(type=["HEARTBEAT", "ATTITUDE"], blocking=True, timeout=max(0.2, args.probe_seconds))
                if probe is not None:
                    log(args, f"selected {candidate} baud={baud} first={probe.get_type()} system={master.target_system} component={master.target_component}")
                    return master
                master.close()
            except Exception as exc:
                log(args, f"skip {candidate} baud={baud}: {exc}")

    log(args, f"no serial MAVLink heartbeat found; listening on {args.auto_udp}")
    return mavutil.mavlink_connection(args.auto_udp, baud=args.baud, autoreconnect=True)


def main():
    args = parse_args()
    try:
        from pymavlink import mavutil
    except ImportError:
        print(
            "[mavlink-z1att] pymavlink is not installed. Run: python3 -m pip install --user pymavlink",
            file=sys.stderr,
            flush=True,
        )
        return 2

    master = open_connection(args, mavutil)
    if args.wait_heartbeat:
        log(args, "waiting heartbeat")
        master.wait_heartbeat()
        log(args, f"heartbeat system={master.target_system} component={master.target_component}")

    min_interval = 1.0 / max(1.0, args.rate_hz)
    last_output = 0.0

    while True:
        msg = master.recv_match(type="ATTITUDE", blocking=True, timeout=1.0)
        if msg is None:
            continue

        now = time.time()
        if now - last_output < min_interval:
            continue
        last_output = now

        roll = math.degrees(float(msg.roll)) * args.roll_scale + args.roll_offset
        pitch = math.degrees(float(msg.pitch)) * args.pitch_scale + args.pitch_offset
        yaw = math.degrees(float(msg.yaw)) * args.yaw_scale + args.yaw_offset
        timestamp_us = time.time_ns() // 1000
        print(f"Z1ATT,{timestamp_us},{roll:.6f},{pitch:.6f},{yaw:.6f}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
