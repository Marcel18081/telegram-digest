#!/usr/bin/env python3
"""
Runs every 60 seconds. On wake from sleep, detects that digest wasn't sent today and sends it.
"""
import os
import sys
import subprocess
from datetime import datetime

MARKER = os.path.join(os.path.dirname(__file__), "digest_last_sent")
AGENT  = os.path.join(os.path.dirname(__file__), "email_agent.py")
TODAY  = datetime.now().strftime("%Y-%m-%d")

if os.path.exists(MARKER):
    with open(MARKER) as f:
        if f.read().strip() == TODAY:
            sys.exit(0)  # already sent today

subprocess.run([sys.executable, AGENT], check=True)
