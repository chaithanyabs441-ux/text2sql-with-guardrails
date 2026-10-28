"""The same-origin frontend proxy must preserve the HTTP-only auth session."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

from frontend import app as frontend


def test_proxy_forwards_cookies_and_session_set_cookie(monkeypatch):
    received = []

    class ApiStub(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.command, self.path, self.headers.get("Cookie")))
            body = b'{"username":"employee","role":"employee"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header(
                "Set-Cookie",
                "safequery_session=opaque; HttpOnly; SameSite=Strict; Path=/",
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            received.append(
                (
                    self.command,
                    self.path,
                    self.headers.get("Cookie"),
                    self.rfile.read(int(self.headers.get("Content-Length", "0"))),
                )
            )
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header(
                "Set-Cookie",
                "safequery_session=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/",
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), ApiStub)
    monkeypatch.setattr(
        frontend,
        "API_BASE_URL",
        f"http://127.0.0.1:{upstream.server_port}",
    )
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), frontend.FrontendHandler)
    for server in (upstream, proxy):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        session_cookie = "safequery_session=opaque"
        request = Request(
            f"http://127.0.0.1:{proxy.server_port}/auth/me",
            headers={"Cookie": session_cookie},
        )
        with urlopen(request) as response:
            assert json.loads(response.read())["role"] == "employee"
            assert "HttpOnly" in response.headers["Set-Cookie"]

        request = Request(
            f"http://127.0.0.1:{proxy.server_port}/auth/logout",
            data=b"{}",
            headers={"Cookie": session_cookie, "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            assert response.status == 200
            assert "Max-Age=0" in response.headers["Set-Cookie"]
    finally:
        proxy.shutdown()
        upstream.shutdown()
        proxy.server_close()
        upstream.server_close()

    assert received[0] == ("GET", "/auth/me", session_cookie)
    assert received[1][:3] == ("POST", "/auth/logout", session_cookie)
    assert received[1][3] == b"{}"
