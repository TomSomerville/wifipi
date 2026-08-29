"""Shared plumbing for the bin/ tools: colours, output, small shell bits.

Two output styles for two audiences. The tools print tagged lines for a human
at a terminal (ok/warn/err); the daemon logs timestamped lines for journald
(log). say() adapts the tagged style to the (level, msg) callable that
recover.reload_driver and the adapter checks expect.
"""

import os
import subprocess
import sys
import time

GRN, RED, YEL, DIM, RST = ("\033[32m", "\033[31m", "\033[33m",
                           "\033[2m", "\033[0m")


def ok(m):
    print(f"{GRN}[ok]{RST}   {m}")


def warn(m):
    print(f"{YEL}[warn]{RST} {m}")


def err(m):
    print(f"{RED}[err]{RST}  {m}")


def skip(m):
    print(f"{DIM}[--]   {m}{RST}")


def note(m):
    print(f"{DIM}       {m}{RST}")


def die(m):
    print(f"{RED}[fail]{RST} {m}", file=sys.stderr)
    sys.exit(1)


def say(level, msg):
    """(level, msg) -> the tagged printers above."""
    {"ok": ok, "warn": warn, "err": err}.get(level, note)(msg)


def log(level, msg):
    # Plain lines with a timestamp: journald adds its own metadata, and a
    # service that formats for a terminal is unreadable in a log file.
    ts = time.strftime("%H:%M:%S")
    colour = {"ok": GRN, "warn": YEL, "err": RED}.get(level, "")
    print(f"[{ts}] {colour}{level:<4}{RST} {msg}", flush=True)


def is_root():
    # geteuid is absent on Windows; these tools are Linux-only, but keeping
    # the check portable lets the logic be exercised anywhere.
    return getattr(os, "geteuid", lambda: 0)() == 0


def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def mac_str(b):
    return ":".join(f"{x:02x}" for x in b)
