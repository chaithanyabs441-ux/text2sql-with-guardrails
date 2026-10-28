"""Serve the HTML/CSS frontend and proxy its API calls to FastAPI."""

import json
import os
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

FRONTEND_DIR = Path(__file__).resolve().parent
API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
MAX_REQUEST_BYTES = 201 * 1024 * 1024


class FrontendHandler(SimpleHTTPRequestHandler):
    """Serve local assets and forward same-origin query requests to the API."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FRONTEND_DIR), **kwargs)

    def end_headers(self) -> None:
        # The frontend contains the auth gate. Do not let browsers keep an older
        # pre-auth page after the server has been upgraded.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            self.path = "/index.html"
        elif self._is_api_path(path, "GET"):
            return self._proxy("GET", self.path)
        return super().do_GET()

    def do_POST(self):
        if not self._is_api_path(urlsplit(self.path).path, "POST"):
            self.send_error(404, "Not found")
            return
        return self._proxy("POST", self.path)

    @staticmethod
    def _is_api_path(path: str, method: str) -> bool:
        if method == "GET":
            return path in {"/auth/me", "/v1/schema", "/v1/history", "/datasets"} or bool(
                re.fullmatch(
                    r"/admin/(?:users|datasets|audit-logs|query-activity)(?:/[a-zA-Z0-9_-]+)*", path
                )
            )
        return path in {"/auth/login", "/auth/logout", "/v1/query", "/datasets/import"} or bool(
            re.fullmatch(r"/admin/(?:users|datasets)(?:/[a-zA-Z0-9_-]+)*", path)
        )

    def _proxy(self, method: str, target: str) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"detail": "Invalid Content-Length header."})
            return
        if content_length > MAX_REQUEST_BYTES or (
            method == "POST" and content_length <= 0 and urlsplit(target).path != "/auth/logout"
        ):
            self._send_json(413, {"detail": "Request body must be under 201 MB."})
            return

        body = self.rfile.read(content_length) if content_length else None
        headers = {"Content-Type": self.headers.get("Content-Type", "application/json")}
        if self.headers.get("Cookie"):
            headers["Cookie"] = self.headers["Cookie"]
        request = Request(
            f"{API_BASE_URL}{target}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=240) as response:
                payload = response.read()
                self._send(
                    response.status,
                    payload,
                    response.headers.get_content_type(),
                    response.headers.get_all("Set-Cookie", []),
                )
        except HTTPError as exc:
            self._send(
                exc.code,
                exc.read(),
                exc.headers.get_content_type(),
                exc.headers.get_all("Set-Cookie", []),
            )
        except (URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            self._send_json(502, {"detail": f"Could not reach the query API: {reason}"})

    def _send(
        self, status: int, body: bytes, content_type: str, cookies: list[str] | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
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
