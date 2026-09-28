"""Start host discovery and the web server in one container."""
from __future__ import annotations

import subprocess
import sys
import os


def main() -> None:
    command = sys.argv[1:] or ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "18770"]
    subprocess.Popen([sys.executable, "-m", "backend.host_snapshot"])
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
