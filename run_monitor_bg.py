"""Background launcher for the GPU dashboard (job-object safe).

Usage:
    python run_monitor_bg.py <password-file> [port] [host] [gpu] [interval]

Reads the SSH password from <password-file>, deletes that file immediately,
then runs app.py **in this same process** (no child process) with stdout and
stderr appended to ./monitor.log.

Start it through WMI (or a scheduled task) so the dashboard is not owned by
the launching shell's job object; a plain subprocess.Popen stays inside that
job and is killed when the launching session closes, even with
DETACHED_PROCESS set.
"""
from __future__ import annotations

import datetime
import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    pw_file = Path(sys.argv[1])
    port = sys.argv[2] if len(sys.argv) > 2 else "18877"
    host = sys.argv[3] if len(sys.argv) > 3 else "192.168.1.100"
    gpu = sys.argv[4] if len(sys.argv) > 4 else "0"
    interval = sys.argv[5] if len(sys.argv) > 5 else "2"

    password = pw_file.read_text(encoding="utf-8").strip()
    try:
        pw_file.unlink()
    except OSError:
        pass

    os.environ["GPU_MONITOR_PASSWORD"] = password

    log = (ROOT / "monitor.log").open("a", encoding="utf-8", buffering=1)
    sys.stdout = log
    sys.stderr = log
    print(
        f"--- start {datetime.datetime.now().isoformat(timespec='seconds')} "
        f"pid={os.getpid()} target={host} port={port} gpu={gpu} interval={interval}s ---",
        flush=True,
    )

    sys.argv = ["app.py", "--port", port, "--host", host,
                "--gpu", gpu, "--interval", interval]
    try:
        runpy.run_path(str(ROOT / "app.py"), run_name="__main__")
    finally:
        print(f"--- exit {datetime.datetime.now().isoformat(timespec='seconds')} ---", flush=True)


if __name__ == "__main__":
    main()
