"""
Dashboard subcommand for hermes CLI.

Handles: hermes dashboard [--port PORT] [--open] [--dev] [status]
"""

import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"


def dashboard_command(args):
    """Handle dashboard subcommands."""
    subcmd = getattr(args, "dashboard_command", None)

    if subcmd == "status":
        _dashboard_status()
        return

    port = getattr(args, "port", 18799)
    open_browser = getattr(args, "open", False)
    dev_mode = getattr(args, "dev", False)

    sys.path.insert(0, str(PROJECT_ROOT))

    if open_browser:
        import webbrowser
        import threading

        def _open():
            time.sleep(1.5)
            webbrowser.open(f"http://localhost:{port}")

        threading.Thread(target=_open, daemon=True).start()

    if dev_mode:
        _run_dev(port)
    else:
        from dashboard.server import run_server
        run_server(port=port)


def _run_dev(port: int):
    """Run the dashboard with auto-restart on Python file changes."""
    watch_paths = [
        DASHBOARD_DIR / "server.py",
        DASHBOARD_DIR / "data.py",
    ]

    print(f"[dev] Watching for changes in dashboard/*.py")
    print(f"[dev] HTML/JS changes only need a browser refresh")
    print(f"[dev] Press Ctrl+C to stop\n")

    while True:
        # Snapshot mtimes
        mtimes = {p: p.stat().st_mtime for p in watch_paths if p.exists()}

        # Start server as subprocess so we can kill and restart it
        proc = subprocess.Popen(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {str(PROJECT_ROOT)!r}); "
             f"from dashboard.server import run_server; run_server(port={port})"],
        )

        try:
            # Poll for file changes
            while proc.poll() is None:
                time.sleep(1)
                for p in watch_paths:
                    if not p.exists():
                        continue
                    current = p.stat().st_mtime
                    if mtimes.get(p) != current:
                        print(f"\n[dev] {p.name} changed — restarting...")
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()
                        time.sleep(0.5)
                        raise _Restart()

            # Server exited on its own (error or Ctrl+C reached it)
            if proc.returncode != 0:
                print(f"[dev] Server exited with code {proc.returncode}")
            break

        except _Restart:
            continue
        except KeyboardInterrupt:
            print("\n[dev] Shutting down...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            break


class _Restart(Exception):
    """Signal to restart the server."""


def _dashboard_status():
    """Check if the dashboard is reachable."""
    import urllib.request

    port = 18799
    try:
        resp = urllib.request.urlopen(f"http://localhost:{port}/api/status", timeout=3)
        if resp.status == 200:
            print(f"Dashboard is running on http://localhost:{port}")
        else:
            print(f"Dashboard responded with status {resp.status}")
    except Exception:
        print("Dashboard is not running.")
        print(f"  Start with: hermes dashboard")
