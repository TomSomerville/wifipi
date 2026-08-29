"""Adapter checks and monitor-mode entry, shared by the daemon and the tools.

One definition of "usable adapter" for everything: the daemon retries on
failure, the monitor exits, setup only warns -- but they all ask the same
questions, so the answers come from here. Progress and failures go to the
caller's (level, msg) callable rather than being printed directly.
"""

import os

from .cli import mac_str, run
from .link import BSSID, CHANNEL

DRIVER_HINT = "mt7925u"


def iface_info(iface):
    """(type, channel) as iw reports them; ("unknown", None) if it won't say."""
    itype, chan = "unknown", None
    for line in run("iw", "dev", iface, "info").stdout.splitlines():
        line = line.strip()
        if line.startswith("type "):
            itype = line.split()[1]
        elif line.startswith("channel "):
            chan = int(line.split()[1])
    return itype, chan


def verify(iface, log):
    """The interface is a USB wifi adapter whose driver can do monitor mode."""
    if not os.path.exists(f"/sys/class/net/{iface}"):
        log("err", f"no such interface: {iface}")
        log("info", "attached?  lsusb    driver?  dmesg | tail -40")
        return False
    if not os.path.exists(f"/sys/class/net/{iface}/phy80211"):
        log("err", f"{iface} is not a wireless interface")
        return False

    dev = os.path.realpath(f"/sys/class/net/{iface}/device")
    if "usb" not in dev:
        log("err", f"{iface} is not USB-attached -- that looks like the "
                   f"onboard radio, whose MAC runs in firmware and cannot "
                   f"inject")
        return False

    drv = os.path.basename(
        os.path.realpath(f"/sys/class/net/{iface}/device/driver"))
    log("ok", f"{iface}: {drv} over USB")
    if drv != DRIVER_HINT:
        log("warn", f"expected {DRIVER_HINT}; {drv} may work if it is mac80211")

    if "monitor" not in run("iw", "phy").stdout:
        log("err", f"driver {drv} does not list monitor in its supported modes")
        return False
    return True


def enter_monitor(iface, log):
    """Engage monitor mode on our channel and leave it there."""
    # NetworkManager and wpa_supplicant drag the interface back to managed.
    if run("sh", "-c", "command -v nmcli").returncode == 0:
        run("nmcli", "device", "set", iface, "managed", "no")
    run("pkill", "-f", f"wpa_supplicant.*{iface}")

    if iface_info(iface)[0] != "monitor":
        run("ip", "link", "set", iface, "down")
        # 'otherbss' asks for frames from every BSS, not just one.
        r = run("iw", "dev", iface, "set", "monitor", "otherbss")
        if r.returncode != 0:
            r = run("iw", "dev", iface, "set", "type", "monitor")
        if r.returncode != 0:
            log("err", f"{iface} will not enter monitor mode: "
                       f"{r.stderr.strip()}")
            return False

    run("ip", "link", "set", iface, "up")
    itype = iface_info(iface)[0]
    if itype != "monitor":
        log("err", f"{iface} is type '{itype}', not monitor")
        return False

    r = run("iw", "dev", iface, "set", "channel", str(CHANNEL))
    if r.returncode != 0:
        log("warn", f"could not set channel {CHANNEL}: {r.stderr.strip()} "
                    f"(check your regulatory domain)")

    log("ok", f"{iface} in monitor mode on channel {CHANNEL}, "
              f"bssid {mac_str(BSSID)}")
    return True


def verify_and_configure(iface, log):
    """Both halves. False means present but unusable -- callers decide whether
    to retry, warn, or exit."""
    return verify(iface, log) and enter_monitor(iface, log)
