import argparse
import hashlib
import json
import logging
import os
import queue
import socket
import ssl
import struct
import subprocess
import threading
import time
from typing import Dict, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "index.html")
SSL_CERT = os.path.join(HERE, "ssl", "cert.pem")
SSL_KEY = os.path.join(HERE, "ssl", "key.pem")


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
)

try:
    from evdev import UInput, ecodes
except Exception:
    UInput = None
    ecodes = None


class InputBackend:
    def scroll(self, amount: int) -> None:
        raise NotImplementedError

    def click(self) -> None:
        raise NotImplementedError

    def move(self, dx: int, dy: int) -> None:
        raise NotImplementedError


class EvdevBackend(InputBackend):
    def __init__(self) -> None:
        self._available = False
        self._ui = None
        if UInput is None or ecodes is None:
            return
        try:
            capabilities = {
                ecodes.EV_REL: [ecodes.REL_X, ecodes.REL_Y, ecodes.REL_WHEEL],
                ecodes.EV_KEY: [ecodes.BTN_LEFT],
            }
            self._ui = UInput(capabilities, name="gyro-mouse")
            self._available = True
        except Exception as exc:
            logging.warning("evdev backend unavailable: %s", exc)

    def available(self) -> bool:
        return self._available

    def scroll(self, amount: int) -> None:
        if not self._available or not self._ui:
            return
        if amount == 0:
            return
        self._ui.write(ecodes.EV_REL, ecodes.REL_WHEEL, amount)
        self._ui.syn()

    def click(self) -> None:
        if not self._available or not self._ui:
            return
        self._ui.write(ecodes.EV_KEY, ecodes.BTN_LEFT, 1)
        self._ui.syn()
        time.sleep(0.005)
        self._ui.write(ecodes.EV_KEY, ecodes.BTN_LEFT, 0)
        self._ui.syn()

    def move(self, dx: int, dy: int) -> None:
        if not self._available or not self._ui:
            return
        if dx == 0 and dy == 0:
            return
        self._ui.write(ecodes.EV_REL, ecodes.REL_X, dx)
        self._ui.write(ecodes.EV_REL, ecodes.REL_Y, dy)
        self._ui.syn()


class YdotoolBackend(InputBackend):
    def __init__(self) -> None:
        self._ydotool = shutil_which("ydotool")

    def available(self) -> bool:
        return self._ydotool is not None

    def scroll(self, amount: int) -> None:
        if not self._ydotool:
            return
        steps = min(abs(amount), 10)
        if steps == 0:
            return
        button = "4" if amount > 0 else "5"
        subprocess.run(
            [self._ydotool, "click", "-r", str(steps), button],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def click(self) -> None:
        if not self._ydotool:
            return
        subprocess.run(
            [self._ydotool, "click", "1"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def move(self, dx: int, dy: int) -> None:
        if not self._ydotool:
            return
        subprocess.run(
            [self._ydotool, "mousemove", "--relative", "--", str(dx), str(dy)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


class XdotoolBackend(InputBackend):
    def __init__(self) -> None:
        self._xdotool = shutil_which("xdotool")

    def available(self) -> bool:
        return self._xdotool is not None

    def scroll(self, amount: int) -> None:
        if not self._xdotool:
            return
        steps = min(abs(amount), 10)
        if steps == 0:
            return
        button = "4" if amount > 0 else "5"
        subprocess.run(
            [self._xdotool, "click", "--repeat", str(steps), "--delay", "0", button],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def click(self) -> None:
        if not self._xdotool:
            return
        subprocess.run(
            [self._xdotool, "click", "1"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def move(self, dx: int, dy: int) -> None:
        if not self._xdotool:
            return
        subprocess.run(
            [self._xdotool, "mousemove_relative", "--", str(dx), str(dy)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


class LogBackend(InputBackend):
    def scroll(self, amount: int) -> None:
        logging.info("scroll %s", amount)

    def click(self) -> None:
        logging.info("click")

    def move(self, dx: int, dy: int) -> None:
        logging.info("move %s %s", dx, dy)


class CoalescingBackend(InputBackend):
    def __init__(self, backend: InputBackend, flush_ms: int = 4) -> None:
        self._backend = backend
        self._flush_interval = max(flush_ms, 1) / 1000.0
        self._queue: "queue.Queue" = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        pending_dx = 0
        pending_dy = 0
        pending_scroll = 0
        last_flush = time.monotonic()
        while not self._stop.is_set():
            try:
                event, a, b = self._queue.get(timeout=self._flush_interval)
            except queue.Empty:
                event = None
                a = 0
                b = 0

            if event == "move":
                pending_dx += a
                pending_dy += b
            elif event == "scroll":
                pending_scroll += a
            elif event == "click":
                self._backend.click()

            now = time.monotonic()
            if now - last_flush >= self._flush_interval:
                if pending_dx or pending_dy:
                    self._backend.move(pending_dx, pending_dy)
                    pending_dx = 0
                    pending_dy = 0
                if pending_scroll:
                    remaining = pending_scroll
                    step = 10 if remaining > 0 else -10
                    while remaining:
                        if abs(remaining) <= 10:
                            self._backend.scroll(remaining)
                            remaining = 0
                        else:
                            self._backend.scroll(step)
                            remaining -= step
                    pending_scroll = 0
                last_flush = now

    def stop(self) -> None:
        self._stop.set()

    def scroll(self, amount: int) -> None:
        self._queue.put(("scroll", amount, 0))

    def click(self) -> None:
        self._queue.put(("click", 0, 0))

    def move(self, dx: int, dy: int) -> None:
        self._queue.put(("move", dx, dy))


def shutil_which(name: str) -> Optional[str]:
    for path in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(path, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def pick_backend() -> InputBackend:
    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    is_wayland = session_type == "wayland" or "WAYLAND_DISPLAY" in os.environ
    is_x11 = session_type == "x11" or "DISPLAY" in os.environ

    if is_x11 and not is_wayland:
        xdotool = XdotoolBackend()
        if xdotool.available():
            logging.info("input backend: xdotool (x11)")
            return xdotool

    evdev = EvdevBackend()
    if evdev.available():
        logging.info("input backend: evdev")
        return evdev
    ydotool = YdotoolBackend()
    if ydotool.available():
        logging.info("input backend: ydotool")
        return ydotool

    if not is_x11:
        xdotool = XdotoolBackend()
        if xdotool.available():
            logging.info("input backend: xdotool (fallback)")
            return xdotool

    logging.warning("no input backend found; falling back to log only")
    return LogBackend()


def make_accept(key: str) -> str:
    guid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    raw = (key + guid).encode("ascii")
    digest = hashlib.sha1(raw).digest()
    return base64_encode(digest)


def base64_encode(data: bytes) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    result = []
    for i in range(0, len(data), 3):
        chunk = data[i : i + 3]
        padding = 3 - len(chunk)
        value = int.from_bytes(chunk, "big") << (padding * 8)
        for shift in range(18, -1, -6):
            result.append(alphabet[(value >> shift) & 0x3F])
        if padding:
            result[-padding:] = "=" * padding
    return "".join(result)


def read_http_request(sock: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(1024)
        if not chunk:
            break
        data += chunk
    return data


def parse_headers(data: bytes) -> Dict[str, str]:
    lines = data.split(b"\r\n")
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            break
        if b":" in line:
            name, value = line.split(b":", 1)
            headers[name.decode("ascii").strip().lower()] = value.decode(
                "ascii"
            ).strip()
    return headers


def send_http(
    sock: socket.socket, status: str, headers: Dict[str, str], body: bytes = b""
) -> None:
    header_lines = [f"HTTP/1.1 {status}"]
    for key, value in headers.items():
        header_lines.append(f"{key}: {value}")
    header_lines.append("\r\n")
    sock.sendall("\r\n".join(header_lines).encode("ascii") + body)


def send_file(sock: socket.socket, path: str, content_type: str) -> None:
    if not os.path.isfile(path):
        send_http(sock, "404 Not Found", {"Content-Length": "0"})
        return
    with open(path, "rb") as handle:
        body = handle.read()
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
        "Cache-Control": "no-store",
    }
    send_http(sock, "200 OK", headers, body)


def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("socket closed")
        data += chunk
    return data


def read_ws_frame(sock: socket.socket) -> Optional[str]:
    header = sock.recv(2)
    if not header:
        return None
    first, second = header
    opcode = first & 0x0F
    masked = second & 0x80
    length = second & 0x7F
    if opcode == 0x8:
        return None
    if length == 126:
        length = struct.unpack(">H", recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", recv_exact(sock, 8))[0]
    mask = recv_exact(sock, 4) if masked else b"\x00\x00\x00\x00"
    payload = recv_exact(sock, length)
    decoded = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    if opcode == 0x1:
        return decoded.decode("utf-8", errors="ignore")
    return None


def handle_ws(sock: socket.socket, backend: InputBackend, peer: str) -> None:
    logging.info("client connected: %s", peer)
    try:
        while True:
            message = read_ws_frame(sock)
            if message is None:
                break
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                continue
            event = data.get("event")
            if event == "scroll":
                amount = int(float(data.get("dy", 0)))
                if amount > 10:
                    amount = 10
                elif amount < -10:
                    amount = -10
                if amount != 0:
                    backend.scroll(amount)
            elif event == "click":
                backend.click()
            elif event == "move":
                dx_raw = data.get("dx", 0)
                dy_raw = data.get("dy", 0)
                try:
                    dx = int(float(dx_raw)) if dx_raw is not None else 0
                    dy = int(float(dy_raw)) if dy_raw is not None else 0
                except (TypeError, ValueError):
                    dx = 0
                    dy = 0
                if dx != 0 or dy != 0:
                    backend.move(dx, dy)
    finally:
        logging.info("client disconnected: %s", peer)
        try:
            sock.close()
        except OSError:
            pass


def serve_client(sock: socket.socket, address: tuple, backend: InputBackend) -> None:
    peer = f"{address[0]}:{address[1]}"
    try:
        data = read_http_request(sock)
        if not data:
            return
        headers = parse_headers(data)
        request_line = data.split(b"\r\n", 1)[0].decode("ascii", errors="ignore")
        parts = request_line.split(" ")
        path = parts[1] if len(parts) > 1 else "/"

        if headers.get("upgrade", "").lower() == "websocket" and path == "/ws":
            key = headers.get("sec-websocket-key")
            if not key:
                send_http(sock, "400 Bad Request", {"Content-Length": "0"})
                return
            accept = make_accept(key)
            response_headers = {
                "Upgrade": "websocket",
                "Connection": "Upgrade",
                "Sec-WebSocket-Accept": accept,
            }
            send_http(sock, "101 Switching Protocols", response_headers)
            handle_ws(sock, backend, peer)
            return

        if path == "/":
            send_file(sock, INDEX_HTML, "text/html; charset=utf-8")
        else:
            send_http(sock, "404 Not Found", {"Content-Length": "0"})
    finally:
        try:
            sock.close()
        except OSError:
            pass


def serve(host: str, port: int) -> None:
    backend = CoalescingBackend(pick_backend())
    logging.info("starting gyro mouse server")
    logging.info("serving https on %s:%s", host, port)

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(5)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(SSL_CERT, SSL_KEY)

    with context.wrap_socket(server, server_side=True) as tls_server:
        while True:
            try:
                client_sock, address = tls_server.accept()
            except ssl.SSLError as exc:
                logging.warning("ssl handshake failed: %s", exc)
                continue
            thread = threading.Thread(
                target=serve_client,
                args=(client_sock, address, backend),
                daemon=True,
            )
            thread.start()


def main() -> None:
    parser = argparse.ArgumentParser(description="Gyro mouse server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8443)
    args = parser.parse_args()
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
