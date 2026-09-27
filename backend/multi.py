"""Persistent multi-service tunnel management."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
import uuid
import httpx
from pathlib import Path
from typing import Any

from .core import DATA, PREFIX_RE, TunnelError, TunnelManager
from .store import Store


class MultiTunnelManager(TunnelManager):
    def __init__(self, data_dir: Path = DATA):
        super().__init__(data_dir)
        self.store = Store(data_dir)
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.readers: dict[str, asyncio.Task] = {}
        self.errors: dict[str, str] = {}
        self._import_existing()

    @staticmethod
    def decode_cert(raw: str) -> dict[str, str]:
        try:
            encoded = re.sub(r"-----[^-]+-----|\s", "", raw)
            value = json.loads(base64.b64decode(encoded))
            if not value.get("apiToken") or not value.get("zoneID"):
                raise ValueError("missing cert fields")
            return value
        except (ValueError, KeyError) as exc:
            raise TunnelError("Cloudflare 登录凭据无效") from exc

    def _import_existing(self) -> None:
        # Preserve credentials from the original single-tunnel version and from
        # completed logins that preceded SQLite storage.
        for cert in (self.data_dir / "login").glob("*/.cloudflared/cert.pem"):
            try:
                raw = cert.read_text()
                value = self.decode_cert(raw)
                self.store.upsert_account(str(value["zoneID"]), str(value.get("accountID") or ""), raw)
            except (OSError, TunnelError):
                continue
        saved = self.saved()
        if not saved.get("tunnel_id") or not saved.get("hostname") or self.store.by_hostname(saved["hostname"]):
            return
        try:
            credentials = Path(saved["credentials"]).read_text()
            zone_id = str(saved.get("zone_id") or "")
            zone_name = str(saved.get("zone") or "")
            if not zone_id:
                zone_id = next((a["zone_id"] for a in self.store.accounts() if a["zone_name"] == zone_name), "")
            if not zone_id and len(self.store.accounts()) == 1:
                zone_id = self.store.accounts()[0]["zone_id"]
            if not zone_id:
                return
            self.store.add_tunnel({"id": uuid.uuid4().hex, "tunnel_id": saved["tunnel_id"],
                "name": saved["hostname"].split(".")[0], "zone_id": zone_id, "zone_name": zone_name,
                "hostname": saved["hostname"], "port": int(saved["port"]), "credentials_json": credentials,
                "enabled": True})
        except (OSError, ValueError, KeyError):
            return

    def status(self) -> dict[str, Any]:
        return {"account_connected": bool(self.store.accounts()), "accounts": self.store.accounts(),
                "tunnel_count": len(self.store.tunnels()), "cloudflared_available": self._binary_available(),
                "cloudflared_installable": bool(self._asset_available())}

    def _asset_available(self) -> bool:
        from .core import ASSETS, platform
        return (platform.system().lower(), platform.machine().lower()) in ASSETS

    def login_status(self, login_id: str) -> dict[str, Any]:
        result = super().login_status(login_id)
        if result["authorized"]:
            session = self.login_sessions[login_id]
            raw = session["cert"].read_text()
            cert = self.decode_cert(raw)
            self.store.upsert_account(str(cert["zoneID"]), str(cert.get("accountID") or ""), raw)
            result["zone_id"] = str(cert["zoneID"])
        return result

    async def zones(self, login_id: str = "") -> list[dict[str, str]]:
        if login_id:
            self.login_status(login_id)
        accounts = self.store.accounts()
        if not accounts:
            raise TunnelError("请先登录 Cloudflare")
        result = []
        for account in accounts:
            zone_id = account["zone_id"]
            name = account["zone_name"]
            if not name:
                cert = self.decode_cert(self.store.account(zone_id)["cert_pem"])
                zone = await self.cf(cert["apiToken"], "GET", f"/zones/{zone_id}")
                name = str((zone or {}).get("name") or "")
                if not name:
                    raise TunnelError("无法读取授权域名，请重新登录")
                self.store.set_zone_name(zone_id, name)
            result.append({"id": zone_id, "name": name})
        return result

    def _account_token(self, zone_id: str) -> str:
        account = self.store.account(zone_id)
        if not account:
            raise TunnelError("此域名尚未授权，请登录 Cloudflare")
        return self.decode_cert(account["cert_pem"])["apiToken"]

    def _write_cert(self, zone_id: str) -> Path:
        account = self.store.account(zone_id)
        if not account:
            raise TunnelError("此域名尚未授权")
        runtime = self.data_dir / "runtime"
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        runtime.chmod(0o700)
        cert = runtime / f"cert-{uuid.uuid4().hex}.pem"
        cert.write_text(account["cert_pem"])
        cert.chmod(0o600)
        return cert

    @staticmethod
    def origin_host() -> str:
        return os.getenv("AUTOTUNNEL_ORIGIN_HOST", "127.0.0.1").strip() or "127.0.0.1"

    async def _resolve_host_header(self, item: dict[str, Any]) -> str:
        mode = item.get("host_mode") or "auto"
        if mode == "public":
            return ""
        if mode == "local":
            return f"localhost:{item['port']}"
        if mode != "auto":
            raise TunnelError("请求主机名模式无效")
        origin = f"http://{self.origin_host()}:{item['port']}/"
        try:
            async with httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
                public = await client.head(origin, headers={"Host": item["hostname"]})
                if public.status_code != 403:
                    return ""
                local = await client.head(origin, headers={"Host": f"localhost:{item['port']}"})
                if local.status_code >= 400:
                    return ""
                async with client.stream("GET", origin, headers={"Host": item["hostname"]}) as response:
                    body = b""
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) >= 2048:
                            break
                if b"Blocked request" in body and b"allowedHosts" in body:
                    return f"localhost:{item['port']}"
        except httpx.HTTPError:
            pass
        return ""

    def _runtime_files(self, item: dict[str, Any]) -> tuple[Path, Path]:
        runtime = self.data_dir / "runtime"
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        runtime.chmod(0o700)
        credential = runtime / f"{item['id']}.credentials.json"
        config = runtime / f"{item['id']}.config.json"
        credential.write_text(item["credentials_json"])
        credential.chmod(0o600)
        ingress = {"hostname": item["hostname"], "service": f"http://{self.origin_host()}:{item['port']}"}
        if item.get("origin_host"):
            ingress["originRequest"] = {"httpHostHeader": item["origin_host"]}
        config.write_text(json.dumps({"tunnel": item["tunnel_id"], "credentials-file": str(credential),
            "ingress": [ingress, {"service": "http_status:404"}]}))
        config.chmod(0o600)
        return credential, config

    def _public(self, item: dict[str, Any]) -> dict[str, Any]:
        process = self.processes.get(item["id"])
        return {"id": item["id"], "name": item["name"], "zone_id": item["zone_id"],
            "zone_name": item["zone_name"], "hostname": item["hostname"], "url": f"https://{item['hostname']}",
            "port": item["port"], "host_mode": item.get("host_mode", "auto"),
            "origin_host": item.get("origin_host", ""), "enabled": bool(item["enabled"]),
            "running": bool(process and process.returncode is None), "error": self.errors.get(item["id"], ""),
            "created_at": item["created_at"], "updated_at": item["updated_at"]}

    async def tunnels(self) -> list[dict[str, Any]]:
        async def check(item: dict[str, Any]) -> dict[str, Any]:
            public = self._public(item)
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection(self.origin_host(), item["port"]), 0.7)
                writer.close()
                await writer.wait_closed()
                public["local_reachable"] = True
            except (OSError, asyncio.TimeoutError):
                public["local_reachable"] = False
            return public
        return await asyncio.gather(*(check(item) for item in self.store.tunnels()))

    def tunnel(self, id: str) -> dict[str, Any]:
        item = self.store.tunnel(id)
        if not item:
            raise TunnelError("找不到这个服务")
        return self._public(item)

    async def _stop_process(self, id: str) -> None:
        process = self.processes.pop(id, None)
        task = self.readers.pop(id, None)
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 8)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if task:
            task.cancel()

    async def _watch(self, id: str, process: asyncio.subprocess.Process) -> None:
        try:
            assert process.stdout
            tail = ""
            while line := await process.stdout.readline():
                message = line.decode(errors="replace").strip()
                if "error" in message.lower() or '"level":"error"' in message.lower():
                    tail = message[-400:]
            code = await process.wait()
            if self.processes.get(id) is process:
                self.errors[id] = f"cloudflared 已退出（状态码 {code}）" + (f"：{tail}" if tail else "")
                self.processes.pop(id, None)
        except asyncio.CancelledError:
            pass

    async def start(self, id: str) -> dict[str, Any]:
        item = self.store.tunnel(id)
        if not item:
            raise TunnelError("找不到这个服务")
        binary = await self.ensure_binary()
        resolved_host = await self._resolve_host_header(item)
        if resolved_host != item.get("origin_host", ""):
            self.store.update_tunnel(id, origin_host=resolved_host)
            item = self.store.tunnel(id)
        async with self.lock:
            await self._stop_process(id)
            _, config = self._runtime_files(item)
            self.errors.pop(id, None)
            process = await asyncio.create_subprocess_exec(binary, "tunnel", "--no-autoupdate", "--config",
                str(config), "run", item["tunnel_id"], stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
            self.processes[id] = process
            self.readers[id] = asyncio.create_task(self._watch(id, process))
            self.store.update_tunnel(id, enabled=1)
        await asyncio.sleep(0.8)
        if id not in self.processes:
            raise TunnelError(self.errors.get(id, "Tunnel 启动失败"))
        return self.tunnel(id)

    async def stop(self, id: str) -> dict[str, Any]:
        if not self.store.tunnel(id):
            raise TunnelError("找不到这个服务")
        async with self.lock:
            await self._stop_process(id)
            self.store.update_tunnel(id, enabled=0)
        return self.tunnel(id)

    async def reconcile(self) -> None:
        for item in self.store.tunnels():
            if item["enabled"]:
                try:
                    await self.start(item["id"])
                except Exception as exc:
                    self.errors[item["id"]] = str(exc)

    async def _check_port(self, port: int) -> None:
        if not 1 <= port <= 65535 or port == int(os.getenv("AUTOTUNNEL_PORT", "18770")):
            raise TunnelError("请选择有效的应用端口，不能公开 AutoTunnel 管理端口")
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(self.origin_host(), port), 2)
            writer.close()
            await writer.wait_closed()
        except (OSError, asyncio.TimeoutError) as exc:
            raise TunnelError(f"本机端口 {port} 无法连接") from exc

    @staticmethod
    def _hostname(prefix: str, zone_name: str) -> str:
        prefix = prefix.strip().lower()
        if not PREFIX_RE.fullmatch(prefix):
            raise TunnelError("前缀只能包含字母、数字和连字符")
        return f"{prefix}.{zone_name}"

    async def _dns_records(self, token: str, zone_id: str, hostname: str) -> list[dict[str, Any]]:
        return await self.cf(token, "GET", f"/zones/{zone_id}/dns_records", params={"name": hostname}) or []

    async def create(self, zone_id: str, prefix: str, port: int, name: str = "", host_mode: str = "auto") -> dict[str, Any]:
        if host_mode not in {"auto", "public", "local"}:
            raise TunnelError("请求主机名模式无效")
        await self._check_port(port)
        zones = await self.zones()
        zone = next((z for z in zones if z["id"] == zone_id), None)
        if not zone:
            raise TunnelError("请选择已授权的域名")
        hostname = self._hostname(prefix, zone["name"])
        if self.store.by_hostname(hostname):
            raise TunnelError("这个地址已在 AutoTunnel 中使用")
        token = self._account_token(zone_id)
        if await self._dns_records(token, zone_id, hostname):
            raise TunnelError("这个地址已有 DNS 记录，请换一个前缀")
        cert = self._write_cert(zone_id)
        credential = self.data_dir / "runtime" / f"new-{uuid.uuid4().hex}.json"
        tunnel_name = f"autotunnel-{uuid.uuid4().hex[:10]}"
        tunnel_id = ""
        record_id = ""
        try:
            await self.command("tunnel", "--no-autoupdate", "--origincert", str(cert), "create",
                               "--output", "json", "--credentials-file", str(credential), tunnel_name)
            tunnel_id = str(uuid.UUID(json.loads(credential.read_text())["TunnelID"]))
            record = await self.cf(token, "POST", f"/zones/{zone_id}/dns_records", json={"type": "CNAME",
                "name": hostname, "content": f"{tunnel_id}.cfargotunnel.com", "proxied": True})
            record_id = str((record or {}).get("id") or "")
            id = uuid.uuid4().hex
            self.store.add_tunnel({"id": id, "tunnel_id": tunnel_id, "name": name.strip() or prefix.strip(),
                "zone_id": zone_id, "zone_name": zone["name"], "hostname": hostname, "port": port,
                "credentials_json": credential.read_text(), "host_mode": host_mode, "enabled": True})
        except Exception:
            if record_id:
                try:
                    await self.cf(token, "DELETE", f"/zones/{zone_id}/dns_records/{record_id}")
                except Exception:
                    pass
            if tunnel_id:
                try:
                    await self.command("tunnel", "--no-autoupdate", "--origincert", str(cert),
                                       "delete", "--force", tunnel_id)
                except Exception:
                    pass
            raise
        finally:
            cert.unlink(missing_ok=True)
            credential.unlink(missing_ok=True)
        try:
            return await self.start(id)
        except TunnelError as exc:
            # The resource exists in Cloudflare and SQLite even when its local
            # connector fails. Return it for recovery in Service Management.
            self.errors[id] = str(exc)
            return self.tunnel(id)

    async def update(self, id: str, *, name: str, prefix: str, port: int, host_mode: str = "auto") -> dict[str, Any]:
        if host_mode not in {"auto", "public", "local"}:
            raise TunnelError("请求主机名模式无效")
        item = self.store.tunnel(id)
        if not item:
            raise TunnelError("找不到这个服务")
        if port != item["port"]:
            await self._check_port(port)
        hostname = self._hostname(prefix, item["zone_name"])
        token = self._account_token(item["zone_id"])
        changed_host = hostname != item["hostname"]
        record_id = ""
        if changed_host:
            if self.store.by_hostname(hostname) or await self._dns_records(token, item["zone_id"], hostname):
                raise TunnelError("新地址已有 DNS 记录，请换一个前缀")
            record = await self.cf(token, "POST", f"/zones/{item['zone_id']}/dns_records", json={
                "type": "CNAME", "name": hostname, "content": f"{item['tunnel_id']}.cfargotunnel.com", "proxied": True})
            record_id = str((record or {}).get("id") or "")
        try:
            self.store.update_tunnel(id, name=name.strip() or prefix.strip(), hostname=hostname, port=port, host_mode=host_mode)
            if item["enabled"]:
                await self.start(id)
        except Exception:
            self.store.update_tunnel(id, name=item["name"], hostname=item["hostname"], port=item["port"], host_mode=item.get("host_mode", "auto"), origin_host=item.get("origin_host", ""))
            if item["enabled"]:
                try:
                    await self.start(id)
                except Exception:
                    pass
            if record_id:
                await self.cf(token, "DELETE", f"/zones/{item['zone_id']}/dns_records/{record_id}")
            raise
        if changed_host:
            old_records = await self._dns_records(token, item["zone_id"], item["hostname"])
            for record in old_records:
                if str(record.get("content") or "").rstrip(".") == f"{item['tunnel_id']}.cfargotunnel.com":
                    await self.cf(token, "DELETE", f"/zones/{item['zone_id']}/dns_records/{record['id']}")
        return self.tunnel(id)

    async def delete(self, id: str) -> None:
        item = self.store.tunnel(id)
        if not item:
            raise TunnelError("找不到这个服务")
        token = self._account_token(item["zone_id"])
        cert = self._write_cert(item["zone_id"])
        try:
            await self._stop_process(id)
            self.store.update_tunnel(id, enabled=0)
            records = await self._dns_records(token, item["zone_id"], item["hostname"])
            for record in records:
                if str(record.get("content") or "").rstrip(".") == f"{item['tunnel_id']}.cfargotunnel.com":
                    await self.cf(token, "DELETE", f"/zones/{item['zone_id']}/dns_records/{record['id']}")
            await self.command("tunnel", "--no-autoupdate", "--origincert", str(cert), "delete", "--force", item["tunnel_id"])
            self.store.delete_tunnel(id)
            for path in (self.data_dir / "runtime").glob(f"{id}.*"):
                path.unlink(missing_ok=True)
        finally:
            cert.unlink(missing_ok=True)

    async def close(self) -> None:
        for id in list(self.processes):
            await self._stop_process(id)
        for session in self.login_sessions.values():
            process = session.get("process")
            if process and process.returncode is None:
                process.terminate()
                await process.wait()
