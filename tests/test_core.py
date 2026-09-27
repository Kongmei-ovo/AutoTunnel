import asyncio
import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from backend.core import TunnelError, TunnelManager
from backend import discovery


def test_login_zone_and_creation_does_not_overwrite_dns(tmp_path, monkeypatch):
    manager = TunnelManager(tmp_path)
    cert = tmp_path / "cert.pem"
    payload = base64.b64encode(json.dumps({"apiToken": "token", "zoneID": "zone"}).encode()).decode()
    cert.write_text(f"-----BEGIN ARGO TUNNEL TOKEN-----\n{payload}\n-----END ARGO TUNNEL TOKEN-----")
    manager.login_sessions["session"] = {"cert": cert, "expires": 10**12, "process": None}
    manager.ensure_binary = AsyncMock(return_value="/bin/true")
    calls = []

    async def cf(token, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/zones/zone":
            return {"id": "zone", "name": "example.com"}
        if method == "GET":
            return [{"id": "existing"}]
        return {}

    manager.cf = cf
    async def local_connection(*args, **kwargs):
        class Writer:
            def close(self): pass
            async def wait_closed(self): pass
        return None, Writer()
    monkeypatch.setattr("backend.core.asyncio.open_connection", local_connection)
    with pytest.raises(TunnelError, match="已有 DNS"):
        asyncio.run(manager.create("session", "zone", "app", 3000))
    assert not any(method == "POST" for method, _, _ in calls)
    assert not manager.state_file.exists()


def test_create_validates_prefix_before_network(tmp_path):
    manager = TunnelManager(tmp_path)
    with pytest.raises(TunnelError, match="前缀"):
        asyncio.run(manager.create("missing", "zone", "bad.prefix", 3000))


def test_discovers_docker_and_local_ports(monkeypatch):
    monkeypatch.setattr(discovery.shutil, "which", lambda name: name if name in {"docker", "ss"} else None)
    def run(*args):
        if args[0] == "docker":
            return json.dumps({"Names": "my-app", "Ports": "127.0.0.1:3000->3000/tcp"})
        return 'LISTEN 0 128 127.0.0.1:8080 0.0.0.0:* users:(("python",pid=1,fd=3))'
    monkeypatch.setattr(discovery, "_run", run)
    found = discovery.discover()
    assert [(item["port"], item["name"]) for item in found] == [(3000, "my-app"), (8080, "python")]


def test_create_writes_loopback_ingress_and_removes_account_cert(tmp_path, monkeypatch):
    manager = TunnelManager(tmp_path)
    cert = tmp_path / "cert.pem"
    payload = base64.b64encode(json.dumps({"apiToken": "token", "zoneID": "zone"}).encode()).decode()
    cert.write_text(f"-----BEGIN ARGO TUNNEL TOKEN-----\n{payload}\n-----END ARGO TUNNEL TOKEN-----")
    manager.login_sessions["session"] = {"cert": cert, "expires": 10**12, "process": None}
    async def local_connection(*args, **kwargs):
        class Writer:
            def close(self): pass
            async def wait_closed(self): pass
        return None, Writer()
    monkeypatch.setattr("backend.core.asyncio.open_connection", local_connection)
    async def cf(token, method, path, **kwargs):
        if path == "/zones/zone": return {"id": "zone", "name": "example.com"}
        if method == "GET": return []
        return {"id": "record"}
    async def command(*args, **kwargs):
        Path(args[args.index("--credentials-file") + 1]).write_text(json.dumps({"TunnelID": "11111111-1111-1111-1111-111111111111"}))
        return 'cloudflared log before output\n{"ID": "11111111-1111-1111-1111-111111111111"}'
    manager.cf = cf
    manager.command = command
    manager.start = AsyncMock(return_value={"running": True})
    result = asyncio.run(manager.create("session", "zone", "my-app", 3000))
    assert result["running"]
    config = json.loads((tmp_path / "tunnel.json").read_text())
    assert config["tunnel"] == "11111111-1111-1111-1111-111111111111"
    assert config["ingress"] == [{"hostname": "my-app.example.com", "service": "http://127.0.0.1:3000"}, {"service": "http_status:404"}]
    assert not cert.exists()
    assert manager.saved()["url"] == "https://my-app.example.com"


def test_login_uses_private_browser_suppression(tmp_path, monkeypatch):
    binary = tmp_path / "fake-cloudflared"
    binary.write_text("#!/bin/sh\nopen https://example.com >/dev/null 2>&1\nprintf 'Please open https://dash.cloudflare.com/argotunnel?aud=x&callback=y\\n'\nsleep 10\n")
    binary.chmod(0o700)
    monkeypatch.setenv("CLOUDFLARED_BIN", str(binary))
    manager = TunnelManager(tmp_path / "data")
    async def exercise():
        result = await manager.begin_login()
        assert result["authorization_url"].startswith("https://dash.cloudflare.com/argotunnel?")
        assert (manager.data_dir / "no-browser" / "open").is_file()
        await manager.close()
    asyncio.run(exercise())


def test_authorized_login_survives_backend_restart(tmp_path):
    login_id = "A" * 24
    cert = tmp_path / "login" / login_id / ".cloudflared" / "cert.pem"
    cert.parent.mkdir(parents=True)
    payload = base64.b64encode(json.dumps({"apiToken": "token", "zoneID": "zone"}).encode()).decode()
    cert.write_text(f"-----BEGIN ARGO TUNNEL TOKEN-----\n{payload}\n-----END ARGO TUNNEL TOKEN-----")
    manager = TunnelManager(tmp_path)
    assert manager.login_status(login_id) == {"authorized": True}
