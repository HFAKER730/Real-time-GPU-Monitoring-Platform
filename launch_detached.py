"""Launch the dashboard without tying it to the current terminal."""
from __future__ import annotations

import getpass
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
password = getpass.getpass("SSH password for sshuser@192.168.1.100: ")
env = os.environ.copy()
env["GPU_MONITOR_PASSWORD"] = password
flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
proc = subprocess.Popen(
    [sys.executable, "app.py"], cwd=ROOT, env=env,
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=flags, start_new_session=True,
)
print(f"GPU dashboard detached, PID={proc.pid}")
