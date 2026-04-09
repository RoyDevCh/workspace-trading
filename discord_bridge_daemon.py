#!/usr/bin/env python3
"""Discord bridge daemon with auto-restart (v2 - robust).

Key fixes:
1. Close log file handle AFTER child exits (not before next open)
2. Wait for child process to fully terminate before reopening log
3. Exponential backoff between restarts
4. Rate limiting: max 10 restarts per hour
"""

import subprocess
import sys
import time
import os

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BRIDGE_SCRIPT = os.path.join(os.path.dirname(__file__), "discord_agent_bridge.py")
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
LOG_FILE = os.path.join(LOG_DIR, "discord_bridge.log")
PYTHON = sys.executable
MAX_RESTARTS = 100
RESTART_DELAY = 15
MAX_RESTARTS_PER_HOUR = 10

os.makedirs(LOG_DIR, exist_ok=True)

os.environ.setdefault("HTTP_PROXY", "http://127.0.0.1:7897")
os.environ.setdefault("HTTPS_PROXY", "http://127.0.0.1:7897")
os.environ.setdefault("PYTHONUTF8", "1")


def daemon_log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [daemon] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(LOG_DIR, "discord_daemon.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


restart_count = 0
restart_times = []

while restart_count < MAX_RESTARTS:
    daemon_log(f"Starting discord bridge (attempt {restart_count + 1})...")
    log_fh = None
    try:
        log_fh = open(LOG_FILE, "a", encoding="utf-8")
        proc = subprocess.Popen(
            [PYTHON, BRIDGE_SCRIPT],
            stdout=log_fh,
            stderr=log_fh,
            env=os.environ,
        )
        proc.wait()
        rc = proc.returncode
        # IMPORTANT: Close the log file handle BEFORE logging about exit
        log_fh.close()
        log_fh = None
        daemon_log(f"Bridge exited with code {rc}")
    except Exception as e:
        daemon_log(f"Bridge crashed: {e}")
        if log_fh is not None:
            try:
                log_fh.close()
            except Exception:
                pass
            log_fh = None

    restart_count += 1
    restart_times.append(time.time())

    # Rate limit
    one_hour_ago = time.time() - 3600
    recent_restarts = [t for t in restart_times if t > one_hour_ago]
    if len(recent_restarts) >= MAX_RESTARTS_PER_HOUR:
        daemon_log(f"{len(recent_restarts)} restarts in the last hour, sleeping 5 min")
        time.sleep(300)
        restart_times = recent_restarts
        continue

    # Exponential backoff
    delay = min(RESTART_DELAY * (2 ** min(restart_count - 1, 5)), 300)
    daemon_log(f"Restarting in {delay}s... (attempt {restart_count}/{MAX_RESTARTS})")
    time.sleep(delay)

daemon_log("Max restarts reached, exiting.")
