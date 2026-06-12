#!/usr/bin/env python3
import argparse
import asyncio
import datetime
import json

import websockets


sender = None
viewers = set()


def log(message):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"{timestamp} {message}", flush=True)


async def send_json(socket, message):
    await socket.send(json.dumps(message))


async def handler(socket, _path=None):
    global sender

    role = None
    log(f"connect {getattr(socket, 'remote_address', '')}")
    try:
        async for raw in socket:
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue

            message_type = message.get("type")
            if message_type == "register":
                role = message.get("role")
                if role == "sender":
                    sender = socket
                    log("registered sender")
                    await send_json(socket, {"type": "registered", "role": "sender"})
                    if viewers:
                        await send_json(socket, {"type": "viewer-ready"})
                elif role == "viewer":
                    viewers.add(socket)
                    log("registered viewer")
                    await send_json(socket, {"type": "registered", "role": "viewer"})
                    if sender:
                        await send_json(sender, {"type": "viewer-ready"})
                continue

            if role == "sender":
                for viewer in list(viewers):
                    try:
                        await viewer.send(raw)
                    except Exception:
                        viewers.discard(viewer)
                if message_type:
                    log(f"sender -> viewer {message_type}")
            elif sender:
                await sender.send(raw)
                if message_type:
                    log(f"viewer -> sender {message_type}")
            elif message_type:
                log(f"no sender for {message_type}")
    finally:
        if socket is sender:
            sender = None
            log("sender disconnected")
        if socket in viewers:
            viewers.discard(socket)
            log("viewer disconnected")


async def main():
    parser = argparse.ArgumentParser(description="Minimal WebSocket signaling relay for THETA Z1 WebRTC streaming.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    log(f"signaling ws://{args.host}:{args.port}")
    async with websockets.serve(handler, args.host, args.port, max_size=10_000_000):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
