"""VM paging through the real HTTP connection against a spec-shaped local engine."""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from olvm_aiops.connection import OlvmConnection
from olvm_aiops.ops.vms import list_vms

pytestmark = pytest.mark.unit


def test_vm_pages_over_http_use_constant_max_and_encoded_search():
    inventory = [{"id": str(i), "name": f"vm-{i}"} for i in range(1, 6)]
    seen: list[tuple[int, int, str]] = []

    class Engine(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, code: int, body: dict):
            encoded = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self):
            if self.path == "/ovirt-engine/sso/oauth/token":
                self.respond(200, {"access_token": "stub-token"})
            elif self.path == "/ovirt-engine/services/sso-logout":
                self.respond(200, {})
            else:
                self.respond(404, {"fault": {"reason": "unknown endpoint"}})

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/ovirt-engine/api/vms":
                self.respond(404, {"fault": {"reason": "unknown endpoint"}})
                return
            if self.headers.get("Authorization") != "Bearer stub-token":
                self.respond(401, {"fault": {"reason": "missing token"}})
                return
            params = parse_qs(parsed.query)
            size = int(params["max"][0])
            query = params["search"][0]
            match = re.fullmatch(r"sortby name page (\d+)", query)
            if not match:
                self.respond(400, {"fault": {"reason": "bad search"}})
                return
            page = int(match.group(1))
            seen.append((size, page, query))
            self.respond(200, {"vm": inventory[(page - 1) * size:page * size]})

    server = HTTPServer(("127.0.0.1", 0), Engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    target = SimpleNamespace(origin=origin, username="stub@internal", password="stub",
                             timeout=5, verify=False)
    client = httpx.Client(base_url=origin, trust_env=False,
                          headers={"Accept": "application/json", "Version": "4"})
    conn = None
    try:
        conn = OlvmConnection(target, client=client)
        pages = []
        number = 1
        while number is not None:
            result = list_vms(conn, limit=2, page=number)
            pages.append(result)
            number = result["nextPage"]
        assert [[vm["id"] for vm in p["vms"]] for p in pages] == [
            ["1", "2"], ["3", "4"], ["5"]]
        assert seen == [(2, 1, "sortby name page 1"), (2, 2, "sortby name page 2"),
                        (2, 2, "sortby name page 2"), (2, 3, "sortby name page 3"),
                        (2, 3, "sortby name page 3")]
    finally:
        if conn is not None:
            conn.close()
        else:
            client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
