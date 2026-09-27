"""Local Cloudflare Tunnel orchestration, adapted from JavHub's Emby tunnel flow."""
from __future__ import annotations

import asyncio
import base64
import json
import hashlib
import platform
import tarfile
import io
import os
import re
import secrets
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

ZONE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$", re.I)
PREFIX_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.I)
LOGIN_URL_RE = re.compile(r"https://dash\.cloudflare\.com/[^\s]+", re.I)
VERSION = "2026.8.3"
ASSETS = {
    ("darwin", "x86_64"): ("cloudflared-darwin-amd64.tgz", "61e1316266a00fd70ce40da011d612badc805367fb65293dd1925f938f704c99", "tgz"),
    ("darwin", "arm64"): ("cloudflared-darwin-arm64.tgz", "40c9144d86df8937c5b43293a1f7d2d2107029aa74725023dd46b1b27154352f", "tgz"),
    ("linux", "x86_64"): ("cloudflared-linux-amd64", "f29324fe934d1e100617484c78deef803c4dc2cd351d645bbde42e96b4fccc5e", "binary"),
    ("linux", "aarch64"): ("cloudflared-linux-arm64", "4bcfd35521a7cbc545ebfd5d57334a71ee180e2a64874981f374c81472118391", "binary"),
}
DATA = Path(os.getenv("AUTOTUNNEL_DATA_DIR", Path.home() / ".autotunnel"))


class TunnelError(Exception):
    pass


class TunnelManager:
    def __init__(self, data_dir: Path = DATA):
        self.data_dir = data_dir
        self.state_file = data_dir / "state.json"
        self.process: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task | None = None
        self.login_sessions: dict[str, dict[str, Any]] = {}
        self.lock = asyncio.Lock()
        self.install_lock = asyncio.Lock()
        self.error = ""

    def binary(self) -> str:
        configured = os.getenv("CLOUDFLARED_BIN", "").strip()
        if configured:
            binary = shutil.which(configured)
            if not binary:
                raise TunnelError("CLOUDFLARED_BIN 指定的程序不可用")
            return binary
        managed = self.data_dir / "bin" / f"cloudflared-{VERSION}"
        if managed.is_file() and os.access(managed, os.X_OK):
            return str(managed)
        binary = shutil.which("cloudflared")
        if not binary:
            raise TunnelError("cloudflared 尚未安装")
        return binary

    async def ensure_binary(self) -> str:
        try:
            return self.binary()
        except TunnelError:
            if os.getenv("CLOUDFLARED_BIN", "").strip():
                raise
        asset = ASSETS.get((platform.system().lower(), platform.machine().lower()))
        if not asset:
            raise TunnelError("当前平台不支持自动安装 cloudflared，请手动安装")
        async with self.install_lock:
            try:
                return self.binary()
            except TunnelError:
                pass
            name, digest, kind = asset
            url = f"https://github.com/cloudflare/cloudflared/releases/download/{VERSION}/{name}"
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
                    response = await client.get(url)
                    response.raise_for_status()
                    payload = response.content
            except httpx.HTTPError as exc:
                raise TunnelError("cloudflared 自动下载失败，请检查网络") from exc
            if len(payload) > 100 * 1024 * 1024 or not secrets.compare_digest(hashlib.sha256(payload).hexdigest(), digest):
                raise TunnelError("cloudflared 下载校验失败")
            if kind == "tgz":
                try:
                    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
                        members = [m for m in archive.getmembers() if m.isfile() and Path(m.name).name == "cloudflared"]
                        if len(members) != 1 or members[0].size > 100 * 1024 * 1024:
                            raise TunnelError("cloudflared 安装包内容无效")
                        payload = archive.extractfile(members[0]).read()
                except (tarfile.TarError, OSError) as exc:
                    raise TunnelError("cloudflared 安装包无法解压") from exc
            target = self.data_dir / "bin" / f"cloudflared-{VERSION}"
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(payload)
            temporary.chmod(0o755)
            temporary.replace(target)
            return str(target)

    def _binary_available(self) -> bool:
        try:
            self.binary()
            return True
        except TunnelError:
            return False

    def saved(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}

    def status(self) -> dict[str, Any]:
        saved = self.saved()
        running = self.process is not None and self.process.returncode is None
        return {"running": running, "url": saved.get("url", ""), "hostname": saved.get("hostname", ""),
                "port": saved.get("port"), "zone": saved.get("zone", ""), "error": self.error,
                "cloudflared_available": self._binary_available(),
                "cloudflared_installable": (platform.system().lower(), platform.machine().lower()) in ASSETS}

    async def command(self, *args: str, timeout: int = 45) -> str:
        binary = await self.ensure_binary()
        process = await asyncio.create_subprocess_exec(binary, *args, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.STDOUT)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise TunnelError("Cloudflare 操作超时") from exc
        result = output.decode(errors="replace").strip()
        if process.returncode:
            raise TunnelError(result.splitlines()[-1] if result else "Cloudflare 操作失败")
        return result

    async def begin_login(self) -> dict[str, str]:
        binary = await self.ensure_binary()
        for old in self.login_sessions.values():
            process = old.get("process")
            if process and process.returncode is None:
                process.terminate()
                await process.wait()
        self.login_sessions.clear()
        login_id = secrets.token_urlsafe(24)
        home = self.data_dir / "login" / login_id
        (home / ".cloudflared").mkdir(parents=True, exist_ok=True)
        # cloudflared opens a browser itself. Route that helper through a private
        # no-op so only the frontend's named popup opens the authorization URL.
        helper_dir = self.data_dir / "no-browser"
        helper_dir.mkdir(parents=True, exist_ok=True)
        for command_name in ("open", "xdg-open"):
            helper = helper_dir / command_name
            helper.write_text("#!/bin/sh\nexit 1\n")
            helper.chmod(0o700)
        env = {**os.environ, "HOME": str(home), "PATH": str(helper_dir) + os.pathsep + os.environ.get("PATH", "")}
        process = await asyncio.create_subprocess_exec(binary, "tunnel", "--no-autoupdate", "login",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
        url = ""
        assert process.stdout
        deadline = asyncio.get_running_loop().time() + 15
        while asyncio.get_running_loop().time() < deadline:
            try:
                line = await asyncio.wait_for(process.stdout.readline(), 2)
            except asyncio.TimeoutError:
                continue
            if not line:
                break
            match = LOGIN_URL_RE.search(line.decode(errors="replace"))
            if match:
                url = match.group(0).rstrip(".,;)")
                break
        if not url:
            process.terminate()
            await process.wait()
            raise TunnelError("没有取得 Cloudflare 登录地址")
        self.login_sessions[login_id] = {"process": process, "cert": home / ".cloudflared" / "cert.pem",
                                          "expires": time.time() + 600}
        asyncio.create_task(process.communicate())
        return {"login_id": login_id, "authorization_url": url}

    def certificate(self, login_id: str) -> dict[str, str]:
        session = self.login_sessions.get(login_id)
        if not session and re.fullmatch(r"[A-Za-z0-9_-]{20,64}", login_id):
            cert = self.data_dir / "login" / login_id / ".cloudflared" / "cert.pem"
            if cert.is_file() and time.time() - cert.stat().st_mtime < 600:
                session = {"process": None, "cert": cert, "expires": cert.stat().st_mtime + 600}
                self.login_sessions[login_id] = session
        if not session or session["expires"] < time.time():
            raise TunnelError("登录已过期，请重新登录")
        try:
            raw = session["cert"].read_text()
            encoded = re.sub(r"-----[^-]+-----|\s", "", raw)
            value = json.loads(base64.b64decode(encoded))
            if not value.get("apiToken") or not value.get("zoneID"):
                raise ValueError("missing token")
            return value
        except (OSError, ValueError) as exc:
            raise TunnelError("等待 Cloudflare 登录完成") from exc

    def login_status(self, login_id: str) -> dict[str, bool]:
        try:
            self.certificate(login_id)
            return {"authorized": True}
        except TunnelError as exc:
            if "过期" in str(exc):
                raise
            return {"authorized": False}

    async def cf(self, token: str, method: str, path: str, **kwargs: Any) -> Any:
        async with httpx.AsyncClient(base_url="https://api.cloudflare.com/client/v4", timeout=30,
                                     headers={"Authorization": f"Bearer {token}"}) as client:
            response = await client.request(method, path, **kwargs)
        try:
            payload = response.json()
        except ValueError as exc:
            raise TunnelError("Cloudflare 返回了无效响应") from exc
        if response.is_error or payload.get("success") is False:
            errors = payload.get("errors") or []
            detail = errors[0].get("message", "Cloudflare API 失败") if errors else "Cloudflare API 失败"
            raise TunnelError(str(detail))
        return payload.get("result")

    async def zones(self, login_id: str) -> list[dict[str, str]]:
        cert = self.certificate(login_id)
        token = cert["apiToken"]
        # cloudflared's cert may be scoped to the chosen zone. Resolve that zone first.
        zone = await self.cf(token, "GET", f"/zones/{cert['zoneID']}")
        if not zone or not zone.get("name"):
            raise TunnelError("无法读取授权域名")
        return [{"id": str(zone["id"]), "name": str(zone["name"])}]

    async def stop(self) -> None:
        async with self.lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        process = self.process
        self.process = None
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 8)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if self.reader:
            self.reader.cancel()
            self.reader = None

    async def _watch(self, process: asyncio.subprocess.Process) -> None:
        try:
            assert process.stdout
            while await process.stdout.readline():
                pass
            code = await process.wait()
            if self.process is process:
                self.error = f"连接已退出（状态码 {code}）"
                self.process = None
        except asyncio.CancelledError:
            pass

    async def start(self) -> dict[str, Any]:
        saved = self.saved()
        config = self.data_dir / "tunnel.json"
        if not saved or not config.is_file() or not Path(saved.get("credentials", "")).is_file():
            raise TunnelError("尚未创建可恢复的 Tunnel")
        binary = await self.ensure_binary()
        async with self.lock:
            await self._stop_locked()
            self.error = ""
            self.process = await asyncio.create_subprocess_exec(binary, "tunnel", "--no-autoupdate",
                "--config", str(config), "run", saved["tunnel_id"], stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
            self.reader = asyncio.create_task(self._watch(self.process))
        await asyncio.sleep(0.8)
        if not self.process:
            raise TunnelError(self.error or "Tunnel 启动失败")
        return self.status()

    async def create(self, login_id: str, zone_id: str, prefix: str, port: int) -> dict[str, Any]:
        if not 1 <= port <= 65535:
            raise TunnelError("端口必须在 1–65535 之间")
        if port == int(os.getenv("AUTOTUNNEL_PORT", "18770")):
            raise TunnelError("不能公开 AutoTunnel 管理页面")
        prefix = prefix.strip().lower()
        if not PREFIX_RE.fullmatch(prefix):
            raise TunnelError("前缀只能包含字母、数字和连字符")
        zones = await self.zones(login_id)
        zone = next((z for z in zones if z["id"] == zone_id), None)
        if not zone or not ZONE_RE.fullmatch(zone["name"]):
            raise TunnelError("请选择已授权的域名")
        hostname = f"{prefix}.{zone['name']}"
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=2)
            writer.close()
            await writer.wait_closed()
        except (OSError, asyncio.TimeoutError) as exc:
            raise TunnelError(f"本机端口 {port} 无法连接，请确认应用正在运行并已映射到宿主机") from exc
        cert = self.login_sessions[login_id]["cert"]
        credentials = self.data_dir / f"{uuid.uuid4().hex}.json"
        tunnel_name = f"autotunnel-{uuid.uuid4().hex[:10]}"
        # Never replace an existing hostname, including a non-CNAME record.
        records = await self.cf(self.certificate(login_id)["apiToken"], "GET",
                                f"/zones/{zone_id}/dns_records", params={"name": hostname})
        if records:
            raise TunnelError("这个地址已有 DNS 记录，请换一个前缀")
        await self.command("tunnel", "--no-autoupdate", "--origincert", str(cert),
                           "create", "--output", "json", "--credentials-file", str(credentials), tunnel_name)
        # cloudflared's combined output can contain logs or a different JSON
        # shape. The credential it just wrote is the authoritative tunnel ID.
        try:
            tunnel_id = str(uuid.UUID(json.loads(credentials.read_text())["TunnelID"]))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise TunnelError("Tunnel 已创建但凭据文件无效，请检查 Cloudflare Tunnel 列表") from exc
        token = self.certificate(login_id)["apiToken"]
        try:
            await self.cf(token, "POST", f"/zones/{zone_id}/dns_records", json={
                "type": "CNAME", "name": hostname, "content": f"{tunnel_id}.cfargotunnel.com", "proxied": True})
        except Exception:
            try:
                await self.command("tunnel", "--no-autoupdate", "--origincert", str(cert), "delete", "--force", tunnel_id)
            finally:
                credentials.unlink(missing_ok=True)
            raise
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(credentials, 0o600)
        config = self.data_dir / "tunnel.json"
        config.write_text(json.dumps({"tunnel": tunnel_id, "credentials-file": str(credentials), "ingress": [
            {"hostname": hostname, "service": f"http://127.0.0.1:{port}"}, {"service": "http_status:404"}]}))
        os.chmod(config, 0o600)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"tunnel_id": tunnel_id, "hostname": hostname, "url": f"https://{hostname}",
                                   "zone": zone["name"], "port": port, "credentials": str(credentials)}))
        os.chmod(tmp, 0o600)
        tmp.replace(self.state_file)
        cert.unlink(missing_ok=True)
        self.login_sessions.pop(login_id, None)
        return await self.start()

    async def close(self) -> None:
        await self.stop()
        for session in self.login_sessions.values():
            process = session.get("process")
            if process and process.returncode is None:
                process.terminate()
                await process.wait()
