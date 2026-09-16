"""
tiny-http-server
=================

A from-scratch HTTP/1.1 server built directly on top of Python's `socket`
module. No `http.server`, `socketserver`, Flask, or any other HTTP
framework is used anywhere in this file -- the request line, headers,
and body framing (Content-Length / chunked) are all parsed by hand, and
responses are assembled and written to the socket manually.

Features implemented:
  * Manual parsing of the request line + headers.
  * GET and POST methods.
  * Persistent connections (HTTP/1.1 keep-alive by default), honoring an
    explicit `Connection: close` request header, and closing after a
    single request when the client sends `Connection: close`.
  * Static file serving from a `www/` directory with a basic extension ->
    Content-Type map, correct Content-Length, 404 for missing files, and
    403 for path-traversal attempts (rejects any path containing `..`).
  * Content-Length-based AND chunked request bodies for POST.
  * Two simple routed handlers: `POST /echo` (echoes the request body
    back) and `GET /time` (returns the current server time).
  * A thread-per-connection concurrency model so multiple clients can be
    served at once.
"""

from __future__ import annotations

import datetime
import os
import socket
import threading
from typing import Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
WWW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "www")

# Socket read chunk size and header size safety cap.
RECV_CHUNK = 4096
MAX_HEADER_BYTES = 64 * 1024
MAX_BODY_BYTES = 16 * 1024 * 1024

# Keep a socket alive for this long (seconds) waiting for the next request
# before giving up on a keep-alive connection.
KEEP_ALIVE_TIMEOUT = 15

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
}
DEFAULT_CONTENT_TYPE = "application/octet-stream"

STATUS_TEXT = {
    200: "OK",
    201: "Created",
    204: "No Content",
    301: "Moved Permanently",
    303: "See Other",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    411: "Length Required",
    413: "Payload Too Large",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    505: "HTTP Version Not Supported",
}


class HttpError(Exception):
    """Raised internally to short-circuit request handling with a status."""

    def __init__(self, status: int, message: str = ""):
        super().__init__(message or STATUS_TEXT.get(status, "Error"))
        self.status = status
        self.message = message or STATUS_TEXT.get(status, "Error")


class SocketReader:
    """Buffers raw bytes off a socket so we can read either full lines
    (for the request line / headers) or an exact number of bytes (for a
    Content-Length body) using the same underlying buffer.
    """

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._buf = b""

    def _fill(self) -> bool:
        data = self.sock.recv(RECV_CHUNK)
        if not data:
            return False
        self._buf += data
        return True

    def read_line(self, max_bytes: int = MAX_HEADER_BYTES) -> Optional[bytes]:
        """Read up to and including the next b"\r\n". Returns None on EOF
        with no data at all (clean connection close between requests)."""
        while b"\r\n" not in self._buf:
            if len(self._buf) > max_bytes:
                raise HttpError(431, "Header line too long")
            if not self._fill():
                if not self._buf:
                    return None
                # Partial line then EOF -- treat as bad request.
                raise HttpError(400, "Unexpected EOF")
        idx = self._buf.index(b"\r\n")
        line, self._buf = self._buf[: idx + 2], self._buf[idx + 2 :]
        return line

    def read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            if not self._fill():
                raise HttpError(400, "Unexpected EOF while reading body")
        data, self._buf = self._buf[:n], self._buf[n:]
        return data


def _guess_content_type(path: str) -> str:
    _, ext = os.path.splitext(path)
    return CONTENT_TYPES.get(ext.lower(), DEFAULT_CONTENT_TYPE)


def _build_response(
    status: int,
    body: bytes = b"",
    headers: Optional[dict] = None,
    close: bool = False,
) -> bytes:
    headers = dict(headers or {})
    headers.setdefault("Content-Length", str(len(body)))
    headers.setdefault("Connection", "close" if close else "keep-alive")
    headers.setdefault("Server", "tiny-http-server/1.0")
    reason = STATUS_TEXT.get(status, "Unknown")
    lines = [f"HTTP/1.1 {status} {reason}\r\n"]
    for key, value in headers.items():
        lines.append(f"{key}: {value}\r\n")
    lines.append("\r\n")
    head = "".join(lines).encode("latin-1")
    return head + body


class Request:
    __slots__ = ("method", "path", "query", "version", "headers", "body")

    def __init__(self, method, path, query, version, headers, body):
        self.method = method
        self.path = path
        self.query = query
        self.version = version
        self.headers = headers  # dict[str, str], lower-cased keys
        self.body = body

    def get_header(self, name: str, default=None):
        return self.headers.get(name.lower(), default)


def _parse_request_line(line: bytes):
    try:
        text = line.decode("iso-8859-1").rstrip("\r\n")
    except UnicodeDecodeError:
        raise HttpError(400, "Malformed request line")
    parts = text.split(" ")
    if len(parts) != 3:
        raise HttpError(400, "Malformed request line")
    method, target, version = parts
    if version not in ("HTTP/1.0", "HTTP/1.1"):
        raise HttpError(505, "Unsupported HTTP version")
    if "?" in target:
        path, _, query = target.partition("?")
    else:
        path, query = target, ""
    if not path.startswith("/"):
        raise HttpError(400, "Malformed request target")
    return method, path, query, version


def _parse_headers(reader: SocketReader) -> dict:
    headers = {}
    total = 0
    while True:
        line = reader.read_line()
        if line is None:
            raise HttpError(400, "Connection closed while reading headers")
        total += len(line)
        if total > MAX_HEADER_BYTES:
            raise HttpError(431, "Headers too large")
        if line in (b"\r\n", b"\n", b""):
            break
        try:
            text = line.decode("iso-8859-1").rstrip("\r\n")
        except UnicodeDecodeError:
            raise HttpError(400, "Malformed header line")
        if ":" not in text:
            raise HttpError(400, "Malformed header line")
        key, _, value = text.partition(":")
        headers[key.strip().lower()] = value.strip()
    return headers


def _read_chunked_body(reader: SocketReader) -> bytes:
    body = b""
    total = 0
    while True:
        size_line = reader.read_line()
        if size_line is None:
            raise HttpError(400, "EOF while reading chunk size")
        size_text = size_line.decode("iso-8859-1").strip()
        # Chunk extensions (after ';') are ignored.
        size_text = size_text.split(";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError:
            raise HttpError(400, "Malformed chunk size")
        if size == 0:
            # Consume trailing headers (trailers) until blank line.
            while True:
                trailer_line = reader.read_line()
                if trailer_line is None or trailer_line in (b"\r\n", b"\n", b""):
                    break
            break
        total += size
        if total > MAX_BODY_BYTES:
            raise HttpError(413, "Body too large")
        chunk = reader.read_exact(size)
        body += chunk
        crlf = reader.read_exact(2)  # trailing CRLF after each chunk
        if crlf != b"\r\n":
            raise HttpError(400, "Malformed chunk terminator")
    return body


def _read_body(reader: SocketReader, headers: dict) -> bytes:
    transfer_encoding = headers.get("transfer-encoding", "").lower()
    if "chunked" in transfer_encoding:
        return _read_chunked_body(reader)

    content_length = headers.get("content-length")
    if content_length is None:
        return b""
    try:
        length = int(content_length)
    except ValueError:
        raise HttpError(400, "Malformed Content-Length")
    if length < 0:
        raise HttpError(400, "Negative Content-Length")
    if length > MAX_BODY_BYTES:
        raise HttpError(413, "Body too large")
    return reader.read_exact(length)


def _is_safe_path(rel_path: str) -> bool:
    """Reject any request path containing a '..' path segment (path
    traversal). This is intentionally conservative and checks the raw,
    undecoded segments as well as the normalized ones."""
    if ".." in rel_path.split("/"):
        return False
    normalized = os.path.normpath(rel_path)
    if normalized.startswith("..") or normalized.startswith(os.sep + ".."):
        return False
    return True


def _serve_static(path: str) -> tuple[int, bytes, dict]:
    if path == "/":
        rel = "index.html"
    else:
        rel = path.lstrip("/")

    if not _is_safe_path(path):
        raise HttpError(403, "Path traversal is not allowed")

    full_path = os.path.normpath(os.path.join(WWW_DIR, rel))
    www_root = os.path.normpath(WWW_DIR)
    if not (full_path == www_root or full_path.startswith(www_root + os.sep)):
        raise HttpError(403, "Forbidden")

    if not os.path.isfile(full_path):
        raise HttpError(404, "Not Found")

    with open(full_path, "rb") as f:
        data = f.read()
    headers = {"Content-Type": _guess_content_type(full_path)}
    return 200, data, headers


def _handle_echo(request: Request) -> tuple[int, bytes, dict]:
    body = request.body
    headers = {"Content-Type": request.get_header("content-type", "application/octet-stream")}
    return 200, body, headers


def _handle_time(request: Request) -> tuple[int, bytes, dict]:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    body = (now + "\n").encode("utf-8")
    return 200, body, {"Content-Type": "text/plain; charset=utf-8"}


ROUTES = {
    ("GET", "/time"): _handle_time,
    ("POST", "/echo"): _handle_echo,
}


def _dispatch(request: Request) -> tuple[int, bytes, dict]:
    handler = ROUTES.get((request.method, request.path))
    if handler is not None:
        return handler(request)

    if request.method in ("GET", "HEAD"):
        return _serve_static(request.path)

    raise HttpError(405, "Method Not Allowed")


def _should_close(request: Optional[Request], version: str) -> bool:
    if request is not None:
        conn = request.get_header("connection", "")
    else:
        conn = ""
    conn = conn.lower()
    if "close" in conn:
        return True
    if "keep-alive" in conn:
        return False
    # HTTP/1.1 defaults to keep-alive, HTTP/1.0 defaults to close.
    return version == "HTTP/1.0"


def handle_connection(conn: socket.socket, addr) -> None:
    reader = SocketReader(conn)
    try:
        conn.settimeout(KEEP_ALIVE_TIMEOUT)
        while True:
            try:
                request_line = reader.read_line()
            except socket.timeout:
                break
            except HttpError as exc:
                conn.sendall(_build_response(exc.status, exc.message.encode(), close=True))
                break

            if request_line is None:
                break  # client closed the connection cleanly

            request: Optional[Request] = None
            try:
                method, path, query, version = _parse_request_line(request_line)
                headers = _parse_headers(reader)
                body = _read_body(reader, headers)
                request = Request(method, path, query, version, headers, body)

                close = _should_close(request, version)
                status, resp_body, resp_headers = _dispatch(request)
            except HttpError as exc:
                close = True
                status, resp_body, resp_headers = exc.status, exc.message.encode(), {}
            except Exception as exc:  # pragma: no cover - defensive fallback
                close = True
                status, resp_body, resp_headers = 500, str(exc).encode(), {}

            response = _build_response(status, resp_body, resp_headers, close=close)
            try:
                conn.sendall(response)
            except OSError:
                break

            if close:
                break
    finally:
        try:
            conn.close()
        except OSError:
            pass


class HttpServer:
    """A minimal, threaded HTTP/1.1 server."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host = host
        self.port = port
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    @property
    def address(self):
        return self._sock.getsockname() if self._sock else (self.host, self.port)

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(128)
        self.port = self._sock.getsockname()[1]
        self._running = True
        self._thread = threading.Thread(target=self._serve_forever, daemon=True)
        self._thread.start()

    def _serve_forever(self) -> None:
        self._sock.settimeout(0.5)
        while self._running:
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            client_thread = threading.Thread(
                target=handle_connection, args=(conn, addr), daemon=True
            )
            client_thread.start()

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="tiny-http-server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    server = HttpServer(args.host, args.port)
    server.start()
    print(f"tiny-http-server listening on http://{args.host}:{server.port}")
    try:
        while True:
            threading.Event().wait(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.stop()


if __name__ == "__main__":
    main()
