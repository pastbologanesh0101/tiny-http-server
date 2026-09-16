"""Unit tests for tiny-http-server.

Each test class starts the real HttpServer on an ephemeral port
(port=0) as a background thread within the test process, then talks
to it over real sockets (either raw `socket` or `http.client`) to
verify protocol behavior end-to-end. Every server is stopped in
tearDown so no listener sockets or threads leak between tests.
"""

import http.client
import os
import socket
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import HttpServer  # noqa: E402


class ServerTestCase(unittest.TestCase):
    """Base class that boots a fresh server for every test."""

    def setUp(self):
        self.server = HttpServer(host="127.0.0.1", port=0)
        self.server.start()
        self.host, self.port = self.server.address

    def tearDown(self):
        self.server.stop()

    def connection(self, timeout=5):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        return conn

    def raw_socket(self, timeout=5):
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        return sock


class TestStaticFiles(ServerTestCase):
    def test_static_file_served_correctly(self):
        conn = self.connection()
        conn.request("GET", "/hello.txt")
        resp = conn.getresponse()
        body = resp.read()
        self.assertEqual(resp.status, 200)
        self.assertIn(b"Hello from tiny-http-server", body)
        self.assertTrue(resp.getheader("Content-Type", "").startswith("text/plain"))
        self.assertEqual(resp.getheader("Content-Length"), str(len(body)))
        conn.close()

    def test_index_served_at_root(self):
        conn = self.connection()
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read()
        self.assertEqual(resp.status, 200)
        self.assertIn(b"It works!", body)
        conn.close()

    def test_404_for_missing_file(self):
        conn = self.connection()
        conn.request("GET", "/does-not-exist.txt")
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 404)
        conn.close()

    def test_path_traversal_blocked(self):
        conn = self.connection()
        # Ask for a file outside of www/ via a traversal sequence.
        conn.request("GET", "/../server.py")
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 403)
        conn.close()

    def test_path_traversal_blocked_encoded_segment(self):
        conn = self.connection()
        conn.request("GET", "/foo/../../etc/passwd")
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 403)
        conn.close()


class TestRoutes(ServerTestCase):
    def test_post_echo_route(self):
        conn = self.connection()
        payload = b"hello, echo!"
        conn.request("POST", "/echo", body=payload,
                      headers={"Content-Type": "text/plain"})
        resp = conn.getresponse()
        body = resp.read()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, payload)
        conn.close()

    def test_time_route(self):
        conn = self.connection()
        conn.request("GET", "/time")
        resp = conn.getresponse()
        body = resp.read().decode()
        self.assertEqual(resp.status, 200)
        # Should look like an ISO-8601 timestamp, e.g. 2026-01-01T00:00:00+00:00
        self.assertRegex(body.strip(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        conn.close()

    def test_chunked_post_body(self):
        sock = self.raw_socket()
        request = (
            b"POST /echo HTTP/1.1\r\n"
            b"Host: test\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Connection: close\r\n"
            b"\r\n"
            b"5\r\nhello\r\n"
            b"1\r\n \r\n"
            b"5\r\nworld\r\n"
            b"0\r\n\r\n"
        )
        sock.sendall(request)
        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
        sock.close()
        self.assertIn(b"200", response.split(b"\r\n", 1)[0])
        self.assertTrue(response.endswith(b"hello world"))


class TestConnectionHandling(ServerTestCase):
    def test_keep_alive_reuses_connection_for_multiple_requests(self):
        conn = self.connection()
        conn.request("GET", "/hello.txt")
        resp1 = conn.getresponse()
        resp1.read()
        self.assertEqual(resp1.status, 200)
        self.assertNotEqual((resp1.getheader("Connection") or "").lower(), "close")

        # Reuse the *same* underlying TCP connection for a second request.
        conn.request("GET", "/time")
        resp2 = conn.getresponse()
        resp2.read()
        self.assertEqual(resp2.status, 200)

        # And a third, just to be sure the loop keeps working.
        conn.request("GET", "/hello.txt")
        resp3 = conn.getresponse()
        resp3.read()
        self.assertEqual(resp3.status, 200)
        conn.close()

    def test_connection_close_terminates_after_one_request(self):
        sock = self.raw_socket()
        request = (
            b"GET /hello.txt HTTP/1.1\r\n"
            b"Host: test\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
        sock.sendall(request)
        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
        # The server must have closed its end after the single response.
        self.assertIn(b"HTTP/1.1 200", response)
        self.assertIn(b"Connection: close", response)

        # A further recv on the now-closed socket returns no data.
        sock.settimeout(1)
        more = sock.recv(4096)
        self.assertEqual(more, b"")
        sock.close()

    def test_multiple_concurrent_clients(self):
        import threading

        results = []

        def worker():
            conn = self.connection()
            conn.request("GET", "/hello.txt")
            resp = conn.getresponse()
            results.append(resp.status)
            resp.read()
            conn.close()

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(len(results), 10)
        self.assertTrue(all(status == 200 for status in results))


class TestMethodHandling(ServerTestCase):
    def test_unsupported_method_returns_405(self):
        conn = self.connection()
        conn.request("DELETE", "/hello.txt")
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 405)
        conn.close()

    def test_post_with_content_length_body(self):
        conn = self.connection()
        payload = b"a" * 1000
        conn.request("POST", "/echo", body=payload)
        resp = conn.getresponse()
        body = resp.read()
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(body), 1000)
        conn.close()


if __name__ == "__main__":
    unittest.main()
