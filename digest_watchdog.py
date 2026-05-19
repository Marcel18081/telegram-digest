#!/usr/bin/env python3
"""
Runs every 60 seconds. On wake from sleep, detects that digest wasn't sent today and sends it.
"""
import os
import sys
import subprocess
from datetime import datetime

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

MARKER = os.path.join(os.path.dirname(__file__), "digest_last_sent")
AGENT  = os.path.join(os.path.dirname(__file__), "email_agent.py")
TODAY  = datetime.now().strftime("%Y-%m-%d")

if os.path.exists(MARKER):
    with open(MARKER) as f:
        if f.read().strip() == TODAY:
            sys.exit(0)  # already sent today

ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
print(f"[{ts}] Watchdog: digest not sent yet today, launching agent...", flush=True)

result = subprocess.run([sys.executable, AGENT])
if result.returncode != 0:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] Watchdog: agent exited with code {result.returncode}, will retry next cycle", flush=True)
