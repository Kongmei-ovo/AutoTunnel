"""Publish host listener names from an isolated, opt-in helper container."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

OUTPUT = Path(os.getenv("AUTOTUNNEL_HOST_PROCESSES_FILE", "/run/autotunnel-host-processes/listeners.json"))


def snapshot(output: str) -> dict[str, str]:
    names: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        port = re.search(r":(\d+)$", parts[3])
        process = re.search(r'users:\(\("([^"]+)', line)
        if port and process:
            names[port.group(1)] = process.group(1)
    return names


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    while True:
        result = subprocess.run(["ss", "-ltnpH"], capture_output=True, text=True, timeout=10, check=False)
        if result.returncode == 0:
            temporary = OUTPUT.with_suffix(".tmp")
            temporary.write_text(json.dumps(snapshot(result.stdout), ensure_ascii=False))
            temporary.chmod(0o644)
            temporary.replace(OUTPUT)
        time.sleep(5)


if __name__ == "__main__":
    main()
