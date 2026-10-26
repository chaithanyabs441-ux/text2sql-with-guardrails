"""Serve the HTML/CSS frontend and proxy its API calls to FastAPI."""

import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

FRONTEND_DIR = Path(__file__).resolve().parent
API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
MAX_REQUEST_BYTES = 1_000_000


class FrontendHandler(SimpleHTTPRequestHandler):
    """Serve local assets and forward same-origin query requests to the API."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FRONTEND_DIR), **kwargs)

    def do_GET(self):
        if urlsplit(self.path).path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        if urlsplit(self.path).path != "/v1/query":
            self.send_error(404, "Not found")
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"detail": "Invalid Content-Length header."})
            return
        if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
            self._send_json(413, {"detail": "Request body must be under 1 MB."})
            return

        body = self.rfile.read(content_length)
        request = Request(
            f"{API_BASE_URL}/v1/query",
            data=body,
            headers={"Content-Type": self.headers.get("Content-Type", "application/json")},
            method="POST",
        )
        try:
            with urlopen(request, timeout=240) as response:
                payload = response.read()
                self._send(response.status, payload, response.headers.get_content_type())
        except HTTPError as exc:
            self._send(exc.code, exc.read(), exc.headers.get_content_type())
        except (URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            self._send_json(502, {"detail": f"Could not reach the query API: {reason}"})

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        self._send(
            status,
            json.dumps(payload).encode("utf-8"),
            "application/json",
        )


if __name__ == "__main__":
    port = int(os.getenv("FRONTEND_PORT", "8501"))
    server = ThreadingHTTPServer(("0.0.0.0", port), FrontendHandler)
    print(f"HTML frontend listening on port {port}; proxying API to {API_BASE_URL}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
