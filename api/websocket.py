"""Hand-rolled RFC 6455 WebSocket over the raw HTTP socket (no websockets lib).
Server-push only: the live feed sends story clusters, poll landings, volatility
ticks, results updates, race calls. Clients that stop responding are dropped."""
from __future__ import annotations

import base64
import hashlib
import json
import struct
import threading

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_clients: list = []
_lock = threading.Lock()


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()


def handshake(handler) -> bool:
    key = handler.headers.get("Sec-WebSocket-Key")
    if not key:
        return False
    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept_key(key))
    handler.end_headers()
    return True


def frame_text(payload: str) -> bytes:
    data = payload.encode()
    n = len(data)
    if n < 126:
        header = struct.pack("!BB", 0x81, n)
    elif n < 65536:
        header = struct.pack("!BBH", 0x81, 126, n)
    else:
        header = struct.pack("!BBQ", 0x81, 127, n)
    return header + data


def serve_client(handler) -> None:
    """Register the socket and block reading (to detect close/ping) until EOF."""
    sock = handler.connection
    with _lock:
        _clients.append(sock)
    try:
        sock.settimeout(30)
        while True:
            try:
                first = handler.rfile.read(2)
            except OSError:
                break
            if not first or len(first) < 2:
                break
            opcode = first[0] & 0x0F
            length = first[1] & 0x7F
            masked = first[1] & 0x80
            if length == 126:
                length = struct.unpack("!H", handler.rfile.read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", handler.rfile.read(8))[0]
            mask = handler.rfile.read(4) if masked else b"\0\0\0\0"
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(handler.rfile.read(length)))
            if opcode == 0x8:  # close
                break
            if opcode == 0x9:  # ping → pong
                try:
                    sock.sendall(struct.pack("!BB", 0x8A, len(payload)) + payload)
                except OSError:
                    break
    finally:
        with _lock:
            if sock in _clients:
                _clients.remove(sock)


def broadcast(message: dict) -> int:
    """Push one JSON frame to every connected client; drop dead sockets."""
    frame = frame_text(json.dumps(message, default=str))
    sent = 0
    with _lock:
        for sock in list(_clients):
            try:
                sock.sendall(frame)
                sent += 1
            except OSError:
                _clients.remove(sock)
    return sent


def client_count() -> int:
    with _lock:
        return len(_clients)
