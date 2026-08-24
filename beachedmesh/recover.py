"""Detecting and recovering a radio that transmits but cannot hear.

The fault: the adapter accepts injection and peers receive its announces, but
it receives nothing at all -- not even beacons from access points in the same
room. Seen on a Pi and a laptop, after moving the adapter between USB ports and
after every reboot on one machine, because a warm reboot leaves the USB port
powered and the device comes back in the state it went down in.

What makes it expensive is that nothing above the driver can see it. Monitor
mode is set, the channel is right, injection succeeds, the service is healthy,
the route table is intact. Every check a node can make on itself passes. The
only evidence is negative: frames that should be arriving are not.

So the test here is deliberately the crudest possible one -- open a raw socket
and count frames of any kind, ours or anyone's. 2.4 GHz is never empty; an
access point beacons about ten times a second, and a laptop that hears none in
three seconds is not in a quiet place, it is deaf. Filtering for beachedmesh
traffic would confuse a dead radio with an empty mesh, which is the whole
mistake this module exists to avoid.

The remedy is to unload the mt76 stack and load it back. That is the only
thing short of physically unplugging the adapter that resets the device.
"""

import os
import select
import shutil
import socket
import subprocess
import time

ETH_P_ALL = 0x0003

# Unload order matters: dependents before dependencies, or modprobe refuses.
MODULES = ["mt7925u", "mt7925_common", "mt792x_lib", "mt76_usb", "mt76"]
LOAD = "mt7925u"

# Long enough that a quiet band still produces beacons, short enough to sit in
# a service's startup path without making a healthy node slow to come up.
LISTEN_SECONDS = 3.0

# USB re-enumeration is not instant, and the driver has firmware to load after
# the device reappears.
SETTLE_SECONDS = 3.0
REAPPEAR_TIMEOUT = 20.0


def _run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def frames_heard(iface, seconds=LISTEN_SECONDS):
    """Count frames of any kind arriving on iface. None if it cannot listen.

    Counts rather than returning on the first frame: the number is worth
    logging, because "heard 412 frames" and "heard 1 frame" are different
    kinds of healthy and the second is worth someone's attention.
    """
    try:
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                          socket.htons(ETH_P_ALL))
        s.bind((iface, ETH_P_ALL))
    except (OSError, AttributeError):
        # AttributeError: no AF_PACKET off Linux, where nothing here runs
        # anyway. Both mean the same thing to the caller -- cannot tell.
        return None

    frames = 0
    deadline = time.monotonic() + seconds
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return frames
            if not select.select([s], [], [], left)[0]:
                continue
            try:
                if s.recv(4096):
                    frames += 1
            except OSError:
                return frames
    finally:
        s.close()


def reload_driver(log=None):
    """Unload the mt76 stack and load it back. True if the load succeeded.

    Removing only the top module leaves mt76 holding the device state that is
    broken, so the dependencies come out too; loading the top one pulls them
    all back in.
    """
    def say(level, msg):
        if log:
            log(level, msg)

    # A service must not die because the box has no modprobe. Nothing else
    # recovers this fault, so say so and let the caller carry on deaf.
    if not shutil.which("modprobe"):
        say("err", "modprobe is missing -- cannot reload the driver")
        return False

    r = _run("modprobe", "-r", *MODULES)
    if r.returncode != 0:
        output = r.stderr + r.stdout
        # -r fails as a batch if any one module is busy. "in use" is a
        # different problem with a different fix, so say which it was.
        if "in use" in output:
            say("err", "cannot unload the driver: something still holds the "
                       "radio (NetworkManager or wpa_supplicant)")
            return False
        lines = output.strip().splitlines()
        say("warn", "module unload reported: " +
                    (lines[-1] if lines else "failed"))
    else:
        say("ok", "mt76 stack unloaded")

    time.sleep(SETTLE_SECONDS)

    r = _run("modprobe", LOAD)
    if r.returncode != 0:
        say("err", f"loading {LOAD} failed: {(r.stderr or r.stdout).strip()}")
        return False
    say("ok", f"{LOAD} reloaded")
    return True


def wait_for_iface(iface, timeout=REAPPEAR_TIMEOUT):
    """Wait for an interface to reappear after a driver reload."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(f"/sys/class/net/{iface}"):
            # Enumerating is not the same as ready -- firmware still loads.
            time.sleep(2)
            return True
        time.sleep(0.5)
    return False
