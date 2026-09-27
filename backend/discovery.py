"""Discover locally reachable application ports without requiring privileged APIs."""
from __future__ import annotations

import json
import http.client
import os
import re
import shutil
import subprocess
from typing import Any


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
    return sorted(found.values(), key=lambda item: (item["source"] != "Docker", item["port"]))
