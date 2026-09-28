"""Discover locally reachable application ports without requiring privileged APIs."""
from __future__ import annotations

import json
import http.client
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


# These describe conventional TCP port usage, not the application currently
# listening on the port. Keep them separate from discovered process names.
PORT_HINTS = {
    21: "FTP", 22: "SSH", 25: "SMTP", 53: "DNS", 80: "HTTP",
    110: "POP3", 139: "SMB", 143: "IMAP", 443: "HTTPS", 445: "SMB",
    631: "IPP 打印", 993: "IMAPS", 995: "POP3S",
    1883: "MQTT", 3306: "MySQL", 5432: "PostgreSQL", 5672: "AMQP",
    6379: "Redis", 8080: "HTTP", 8443: "HTTPS",
}

HOST_PROCESSES_FILE = Path(os.getenv("AUTOTUNNEL_HOST_PROCESSES_FILE", "/run/autotunnel-host-processes/listeners.json"))


def _host_process_names() -> dict[int, str]:
    try:
        if time.time() - HOST_PROCESSES_FILE.stat().st_mtime > 30:
            return {}
        names = json.loads(HOST_PROCESSES_FILE.read_text())
        return {int(port): name for port, name in names.items()
                if str(port).isdigit() and 1 <= int(port) <= 65535 and isinstance(name, str) and name[:1]}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _run(*args: str) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _docker_socket_containers() -> list[dict[str, Any]]:
    socket = "/var/run/docker.sock"
    if not os.path.exists(socket) or not os.access(socket, os.R_OK | os.W_OK):
        return []
    class UnixConnection(http.client.HTTPConnection):
        def connect(self):
            import socket as socket_module
            self.sock = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
            self.sock.settimeout(2)
            self.sock.connect(socket)
    connection = UnixConnection("localhost", timeout=2)
    try:
        connection.request("GET", "/containers/json")
        response = connection.getresponse()
        if response.status != 200:
            return []
        return json.loads(response.read(2_000_000))
    except (OSError, ValueError, http.client.HTTPException):
        return []
    finally:
        connection.close()


def discover() -> list[dict[str, Any]]:
    found: dict[int, dict[str, Any]] = {}
    for item in _docker_socket_containers():
        for mapping in item.get("Ports", []):
            if mapping.get("Type") != "tcp" or not mapping.get("PublicPort"):
                continue
            port = int(mapping["PublicPort"])
            name = (item.get("Names") or [item.get("Image") or "Docker 容器"])[0].lstrip("/")
            found[port] = {"name": name, "port": port, "source": "Docker",
                           "detail": f"容器端口 {mapping.get('PrivatePort', '?')}"}
    if shutil.which("docker"):
        output = _run("docker", "ps", "--format", "{{json .}}")
        for line in output.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            for match in re.finditer(r"(?:127\.0\.0\.1|0\.0\.0\.0|\[::\]|:::)[^\s,]*?:(\d+)->(\d+)/tcp", item.get("Ports", "")):
                port = int(match.group(1))
                found[port] = {"name": item.get("Names") or item.get("Image") or "Docker 容器",
                               "port": port, "source": "Docker", "detail": f"容器端口 {match.group(2)}"}
    if shutil.which("lsof"):
        output = _run("lsof", "-nP", "-iTCP", "-sTCP:LISTEN")
        for line in output.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 9:
                continue
            match = re.search(r":(\d+)(?:\s|$)", line)
            if match:
                port = int(match.group(1))
                found.setdefault(port, {"name": parts[0], "port": port, "source": "本机进程", "detail": "正在监听"})
    elif shutil.which("ss"):
        output = _run("ss", "-ltnpH")
        for line in output.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            match = re.search(r":(\d+)$", parts[3])
            if match:
                port = int(match.group(1))
                process = re.search(r'users:\(\("([^"]+)', line)
                found.setdefault(port, {"name": process.group(1) if process else "Linux 服务", "port": port,
                                        "source": "本机进程", "detail": "正在监听"})
    for port, name in _host_process_names().items():
        if port in found and found[port]["source"] != "Docker":
            found[port]["name"] = name
    for item in found.values():
        if item["source"] != "Docker" and item["port"] in PORT_HINTS:
            item["port_hint"] = PORT_HINTS[item["port"]]
    return sorted(found.values(), key=lambda item: (item["source"] != "Docker", item["port"]))
