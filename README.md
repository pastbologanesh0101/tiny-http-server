# tiny-http-server

A small HTTP/1.1 server implemented **entirely from scratch** on top of
Python's `socket` module. No `http.server`, `socketserver`, Flask,
`aiohttp`, or any other HTTP framework is used — the request line,
headers, and body framing are parsed by hand, and every response is
assembled and written to the socket manually.

## Protocol features implemented

- **Manual parsing** of the request line (`METHOD /path?query HTTP/1.1`)
  and headers, with basic validation and `400`/`431`/`505` error
  responses for malformed input.
- **GET and POST** methods (unsupported methods get a `405`).
- **Persistent connections**: HTTP/1.1 keep-alive is the default, so a
  single TCP connection can be reused for many sequential requests. An
  explicit `Connection: close` request header (or an `HTTP/1.0` client
  with no `Connection: keep-alive`) closes the socket after that
  response.
- **Static file serving** from a `www/` directory:
  - `Content-Type` is derived from a small extension map (`.html`,
    `.css`, `.js`, `.json`, `.png`, `.jpg`, `.svg`, `.txt`, ...) and
    falls back to `application/octet-stream`.
  - `Content-Length` is always set correctly.
  - Missing files return `404`.
  - Any request path containing a `..` segment (path traversal) is
    rejected with `403`, and the resolved path is double-checked to stay
    inside `www/` even after normalization.
- **Request bodies for POST**: both `Content-Length`-delimited bodies
  and `Transfer-Encoding: chunked` bodies are supported.
- **Routed handlers**, checked before falling back to static files:
  - `GET /time` — returns the current UTC time as ISO-8601 text.
  - `POST /echo` — reads the request body and sends it right back.
- **Concurrency**: each accepted connection is handled on its own
  thread, so multiple clients can be served at the same time, and each
  connection loops over multiple requests until the client (or the
  keep-alive timeout) closes it.

## Running it

Requires only the Python standard library (Python 3.9+).

```bash
python3 server.py --host 127.0.0.1 --port 8080
```

The server prints the address it bound to and serves files out of the
`www/` directory next to `server.py`.

## Example requests

```bash
# Static file (index.html served at "/")
curl -i http://127.0.0.1:8080/

# Another static file, with its own content type
curl -i http://127.0.0.1:8080/hello.txt

# 404 for a file that doesn't exist
curl -i http://127.0.0.1:8080/nope.txt

# 403 for a path traversal attempt
curl -i http://127.0.0.1:8080/../server.py

# GET /time route
curl -i http://127.0.0.1:8080/time

# POST /echo route
curl -i -X POST --data "hello server" http://127.0.0.1:8080/echo

# Keep-alive: two requests over one connection (curl does this by default
# with HTTP/1.1 when talking to the same host:port)
curl -i http://127.0.0.1:8080/time http://127.0.0.1:8080/hello.txt

# Force the connection to close after one request
curl -i -H "Connection: close" http://127.0.0.1:8080/hello.txt
```

## Tests

The test suite (`tests/test_server.py`) starts a real `HttpServer`
instance on an ephemeral port as a background thread inside the test
process, then drives it with `http.client` and raw `socket` connections
to verify:

- static files are served with the right body/headers,
- missing files return `404`,
- path traversal attempts return `403`,
- keep-alive connections are reused across multiple requests,
- `Connection: close` tears the connection down after one response,
- chunked and `Content-Length` POST bodies are read correctly,
- the `/echo` and `/time` routes behave as documented,
- multiple concurrent clients can be served at once.

Run them with:

```bash
python3 -m unittest discover -s tests -v
```

or, if you prefer pytest:

```bash
pip install pytest
pytest tests/
```

Continuous integration (`.github/workflows/tests.yml`) runs this same
suite on every push and pull request against Python 3.11 and 3.12.

## Project layout

```
tiny-http-server/
├── server.py              # the HTTP/1.1 server implementation
├── www/                    # static files served by the server
│   ├── index.html
│   └── hello.txt
├── tests/
│   └── test_server.py      # unit tests
├── .github/workflows/
│   └── tests.yml           # CI
├── LICENSE
└── README.md
```

## License

MIT — see [LICENSE](LICENSE).
