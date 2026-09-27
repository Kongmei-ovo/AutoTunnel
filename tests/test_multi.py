import asyncio
import base64
import json
import os
import stat
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import AsyncMock

import pytest

from backend.core import TunnelError
from backend.multi import MultiTunnelManager

ZONE = "zone-id"
TID = "11111111-1111-1111-1111-111111111111"


def seed_account(manager):
    payload = base64.b64encode(json.dumps({"apiToken": "secret-token", "zoneID": ZONE, "accountID": "account"}).encode()).decode()
    cert = f"-----BEGIN ARGO TUNNEL TOKEN-----\n{payload}\n-----END ARGO TUNNEL TOKEN-----"
    manager.store.upsert_account(ZONE, "account", cert, "example.com")


def seed_tunnel(manager):
    manager.store.add_tunnel({"id": "local-id", "tunnel_id": TID, "name": "App", "zone_id": ZONE,
        "zone_name": "example.com", "hostname": "app.example.com", "port": 3000,
        "credentials_json": json.dumps({"TunnelID": TID, "TunnelSecret": "private"}), "enabled": False})


def test_sqlite_persists_accounts_and_tunnels_without_leaking_secrets(tmp_path):
    first = MultiTunnelManager(tmp_path)
    seed_account(first); seed_tunnel(first)
    second = MultiTunnelManager(tmp_path)
    assert second.status()["account_connected"]
    assert second.status()["tunnel_count"] == 1
    row = second.tunnel("local-id")
    assert row["hostname"] == "app.example.com"
    assert "secret-token" not in json.dumps(second.status())
    assert "TunnelSecret" not in json.dumps(row)
    assert stat.S_IMODE(os.stat(second.store.path).st_mode) == 0o600


def test_vite_host_rejection_gets_localhost_origin_override(tmp_path):
    class Origin(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(200 if self.headers.get("Host", "").startswith("localhost:") else 403)
            self.end_headers()
        def do_GET(self):
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Blocked request. Add the host to preview.allowedHosts")
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        manager = MultiTunnelManager(tmp_path)
        item = {"id": "example", "hostname": "app.example.com", "port": server.server_port,
                "host_mode": "auto", "tunnel_id": TID, "credentials_json": "{}"}
        host = asyncio.run(manager._resolve_host_header(item))
        assert host == f"localhost:{server.server_port}"
        item["origin_host"] = host
        _, config = manager._runtime_files(item)
        ingress = json.loads(config.read_text())["ingress"][0]
        assert ingress["originRequest"]["httpHostHeader"] == host
    finally:
        server.shutdown()
        thread.join()


def test_create_second_tunnel_uses_existing_login_and_keeps_first(tmp_path, monkeypatch):
    manager = MultiTunnelManager(tmp_path)
    seed_account(manager); seed_tunnel(manager)
    manager._check_port = AsyncMock()
    manager.start = AsyncMock(side_effect=lambda id: manager.tunnel(id))
    calls = []
    async def cf(token, method, path, **kwargs):
        assert token == "secret-token"
        calls.append((method, path))
        if method == "GET": return []
        if method == "POST": return {"id": "dns-id"}
        return {}
    async def command(*args, **kwargs):
        Path(args[args.index("--credentials-file") + 1]).write_text(json.dumps({"TunnelID": "22222222-2222-2222-2222-222222222222", "TunnelSecret": "private"}))
        return 'log before JSON'
    manager.cf = cf; manager.command = command
    result = asyncio.run(manager.create(ZONE, "photos", 4000, "我的相册"))
    assert result["hostname"] == "photos.example.com"
    assert len(manager.store.tunnels()) == 2
    assert manager.store.tunnel("local-id") is not None
    assert ("POST", f"/zones/{ZONE}/dns_records") in calls


def test_edit_prefix_and_port_replaces_only_managed_dns(tmp_path):
    manager = MultiTunnelManager(tmp_path)
    seed_account(manager); seed_tunnel(manager)
    manager._check_port = AsyncMock()
    calls = []
    async def cf(token, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET" and kwargs["params"]["name"] == "renamed.example.com": return []
        if method == "GET": return [{"id": "old-dns", "content": TID + ".cfargotunnel.com"},
                                    {"id": "unrelated", "content": "elsewhere.example.com"}]
        if method == "POST": return {"id": "new-dns"}
        return {}
    manager.cf = cf
    row = asyncio.run(manager.update("local-id", name="Renamed", prefix="renamed", port=4000))
    assert row["hostname"] == "renamed.example.com"
    assert row["port"] == 4000
    deleted = [path for method, path, _ in calls if method == "DELETE"]
    assert deleted == [f"/zones/{ZONE}/dns_records/old-dns"]


def test_delete_removes_dns_and_cloudflare_tunnel(tmp_path):
    manager = MultiTunnelManager(tmp_path)
    seed_account(manager); seed_tunnel(manager)
    calls = []
    async def cf(token, method, path, **kwargs):
        calls.append((method, path))
        if method == "GET": return [{"id": "dns", "content": TID + ".cfargotunnel.com"}]
        return {}
    async def command(*args, **kwargs):
        calls.append(("CLI", args[-1]))
        return ""
    manager.cf = cf; manager.command = command
    asyncio.run(manager.delete("local-id"))
    assert not manager.store.tunnel("local-id")
    assert ("DELETE", f"/zones/{ZONE}/dns_records/dns") in calls
    assert ("CLI", TID) in calls
