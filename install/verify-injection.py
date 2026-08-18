#!/usr/bin/env python3
"""Verify that a mac80211 adapter can do monitor mode and frame injection.

  sudo ./verify-injection.py -i wlan1            full local check
  sudo ./verify-injection.py -i wlan1 --listen   watch for a peer's test frames

Local check proves: monitor mode receives, and the driver accepts injected
frames. Proving the frames actually hit the air needs a second machine -- run
--listen there while this sends.
"""

import argparse
import os
import re
import socket
import struct
import subprocess
import sys
import time

ETH_P_ALL = 0x0003

# Fixed for now: every node must sit on the same channel to hear each other.
CHANNEL = 1
TEST_BSSID = bytes.fromhex("02DEADBEEF01")
TEST_MAGIC = b"WIFIPI-INJECT-TEST"

GRN, RED, YEL, DIM, RST = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def ok(msg):
    print(f"{GRN}[ok]{RST}   {msg}")


def bad(msg):
    print(f"{RED}[fail]{RST} {msg}")


def warn(msg):
    print(f"{YEL}[warn]{RST} {msg}")


def run(*cmd, check=False):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {r.stderr.strip()}")
    return r


# ---------------------------------------------------------------- radiotap --

def radiotap_tx(rate_mbps: float = 1.0) -> bytes:
    freq = 2407 + CHANNEL * 5
    chan_flags = 0x0080 | (0x0020 if rate_mbps <= 11 else 0x0040)
    present = (1 << 2) | (1 << 3) | (1 << 15)
    return struct.pack(
        "<BBHIBBHHH", 0, 0, 16, present,
        int(rate_mbps * 2), 0, freq, chan_flags, 0x0008 | 0x0010,
    )


def radiotap_len(buf: bytes) -> int:
    if len(buf) < 8:
        return 0
    return struct.unpack_from("<H", buf, 2)[0]


def test_frame(mac: bytes, seq: int) -> bytes:
    hdr = struct.pack(
        "<BBH6s6s6sH", 0x08, 0x00, 0,
        b"\xff" * 6, mac, TEST_BSSID, (seq & 0xFFF) << 4,
    )
    return hdr + TEST_MAGIC + b"%04d" % seq


# ------------------------------------------------------------ monitor mode --

def iface_mac(ifname: str) -> bytes:
    with open(f"/sys/class/net/{ifname}/address") as f:
        return bytes.fromhex(f.read().strip().replace(":", ""))


def iface_exists(ifname: str) -> bool:
    return os.path.exists(f"/sys/class/net/{ifname}")


def iface_is_up(ifname: str) -> bool:
    try:
        with open(f"/sys/class/net/{ifname}/flags") as f:
            return bool(int(f.read().strip(), 16) & 0x1)  # IFF_UP
    except OSError:
        return False


def iface_driver(ifname: str) -> str:
    try:
        return os.path.basename(os.path.realpath(f"/sys/class/net/{ifname}/device/driver"))
    except OSError:
        return "unknown"


def iface_type(ifname: str) -> str:
    r = run("iw", "dev", ifname, "info")
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("type "):
            return line.split()[1]
    return "unknown"


def enable_monitor(ifname: str) -> str:
    """Put the adapter into monitor mode. Returns the capture interface name.

    On a mac80211 driver monitor mode is just an interface type, and injection
    is a supported path -- the host builds the frames.
    """
    print(f"       driver: {iface_driver(ifname)}")

    if iface_type(ifname) != "monitor":
        run("ip", "link", "set", ifname, "down")
        # 'otherbss' asks for frames from every BSS, not just one.
        r = run("iw", "dev", ifname, "set", "monitor", "otherbss")
        if r.returncode != 0:
            r = run("iw", "dev", ifname, "set", "type", "monitor")
        if r.returncode != 0:
            bad(f"{ifname} will not enter monitor mode: {r.stderr.strip()}")

    # Bringing the interface up is not optional -- recv() on a down interface
    # raises ENETDOWN, which is easy to misread as a driver problem.
    r = run("ip", "link", "set", ifname, "up")
    if r.returncode != 0:
        bad(f"could not bring {ifname} up: {r.stderr.strip()}")
    elif not iface_is_up(ifname):
        bad(f"{ifname} still reports down after 'ip link set up'")
    else:
        ok(f"{ifname} is up")

    if iface_type(ifname) == "monitor":
        ok(f"{ifname} is in monitor mode")
    else:
        bad(f"{ifname} is type '{iface_type(ifname)}', not monitor")

    r = run("iw", "dev", ifname, "set", "channel", str(CHANNEL))
    if r.returncode == 0:
        ok(f"channel {CHANNEL} set")
    else:
        warn(f"could not set channel {CHANNEL}: {r.stderr.strip()}")

    return ifname


def open_sock(ifname: str, timeout: float = 2.0) -> socket.socket:
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    s.bind((ifname, ETH_P_ALL))
    s.settimeout(timeout)
    return s


# ------------------------------------------------------------------ checks --

def check_mac80211(ifname: str) -> bool:
    """For mac80211 adapters, confirm the driver advertises monitor mode."""
    drv = iface_driver(ifname)
    r = run("iw", "phy")
    good = True
    if "monitor" in r.stdout:
        ok(f"driver {drv} advertises monitor mode")
    else:
        bad(f"driver {drv} does not list monitor in its supported modes")
        good = False
    d = run("dmesg")
    errs = [l for l in d.stdout.splitlines()
            if re.search(r"mt79|mt76|firmware", l, re.I) and re.search(r"fail|error|timeout", l, re.I)]
    for line in errs[-5:]:
        warn(line.strip())
    return good


def check_rx(sock: socket.socket, seconds: float = 5.0) -> bool:
    """Monitor mode works if we hear anyone at all. There is always ambient
    2.4 GHz traffic; total silence means the radio is not in monitor mode."""
    print(f"       listening {seconds:.0f}s for any 802.11 traffic...")
    end = time.time() + seconds
    frames = 0
    raw = 0
    while time.time() < end:
        try:
            buf = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError as e:
            bad(f"capture failed: {e}")
            return False
        if not buf:
            continue
        raw += 1
        if radiotap_len(buf) > 0:
            frames += 1
    if frames:
        ok(f"monitor RX working ({frames} frames captured)")
        return True
    if raw:
        # Packets arrived but carried no radiotap header, so the capture path
        # is alive and the framing is what is wrong.
        bad(f"{raw} packets captured but none had a radiotap header")
    else:
        bad("no packets captured at all on this interface")
        print("       monitor mode is set, so this is a receive-path problem:")
        print("       * cross-check with:  sudo tcpdump -i <iface> -c 10")
        print("       * confirm the channel took:  iw dev <iface> info")
    return False


def check_inject(sock: socket.socket, mac: bytes, count: int = 5) -> bool:
    # No self-capture check here. A radio is half-duplex -- its receiver is
    # blanked while transmitting -- so it can never hear its own frame. Only a
    # second node can prove transmission, which is what --listen is for.
    rt = radiotap_tx()
    accepted = 0
    print(f"       injecting {count} frames...")
    for i in range(count):
        try:
            sock.send(rt + test_frame(mac, i))
            accepted += 1
        except OSError as e:
            bad(f"frame {i} rejected: {e}")
            break
        time.sleep(0.05)

    if accepted != count:
        bad(f"driver accepted only {accepted}/{count} frames")
        return False
    ok(f"driver accepted {accepted}/{count} injected frames")
    print("       frames reaching the air can only be confirmed by a second node:")
    print("       run  sudo ./verify-injection.py -i <iface> --listen  there")
    return True


def hexdump(data: bytes, indent: str = "         ") -> str:
    """Classic offset / hex / printable-ASCII dump."""
    out = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hexes = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{indent}{off:04x}  {hexes:<47}  |{text}|")
    return "\n".join(out)


def describe_frame(buf: bytes) -> str:
    """Break a captured frame into radiotap / 802.11 header / body."""
    rl = radiotap_len(buf)
    rt, frame = buf[:rl], buf[rl:]
    hdr, body = frame[:24], frame[24:]

    def mac(b):
        return ":".join(f"{x:02x}" for x in b)

    lines = [f"       radiotap ({len(rt)} bytes)", hexdump(rt)]

    if len(hdr) == 24:
        fc_type, fc_flags, dur, a1, a2, a3, seq = struct.unpack("<BBH6s6s6sH", hdr)
        lines += [
            f"       802.11 header ({len(hdr)} bytes)",
            f"         frame control  type/subtype 0x{fc_type:02x}  flags 0x{fc_flags:02x}",
            f"         duration       {dur}",
            f"         addr1 receiver {mac(a1)}",
            f"         addr2 sender   {mac(a2)}",
            f"         addr3 bssid    {mac(a3)}",
            f"         sequence       {seq >> 4}  fragment {seq & 0x0f}",
            hexdump(hdr),
        ]
    else:
        lines += [f"       802.11 header truncated ({len(hdr)} bytes)", hexdump(hdr)]

    lines += [f"       body ({len(body)} bytes)", hexdump(body)]
    return "\n".join(lines)


def do_listen(sock: socket.socket, seconds: float) -> bool:
    print(f"       waiting {seconds:.0f}s for test frames from a peer...")
    end = time.time() + seconds
    hits = 0
    while time.time() < end:
        try:
            buf = sock.recv(4096)
        except socket.timeout:
            continue
        rl = radiotap_len(buf)
        body = buf[rl:]
        if len(body) > 24 and body[16:22] == TEST_BSSID and TEST_MAGIC in body:
            src = ":".join(f"{b:02x}" for b in body[10:16])
            hits += 1
            print()
            print(f"       ---- frame {hits} from {src} "
                  f"({len(buf)} bytes captured) ----")
            print(describe_frame(buf))
    if hits:
        ok(f"received {hits} injected frames -- injection confirmed over the air")
        return True
    bad("no test frames received")
    return False


def main() -> int:
    p = argparse.ArgumentParser()
    # Required, not defaulted: wlan0 is usually the Pi's onboard radio, which
    # cannot inject. Guessing it wastes a run and looks like a driver failure.
    p.add_argument("-i", "--iface", required=True,
                   help="monitor-mode interface, e.g. wlan1")
    p.add_argument("-n", "--count", type=int, default=5,
                   help="frames to inject (default 5)")
    p.add_argument("--listen", action="store_true", help="peer mode: watch for test frames")
    p.add_argument("--seconds", type=float, default=30.0, help="listen duration")
    args = p.parse_args()

    if os.geteuid() != 0:
        bad("must run as root")
        return 1
    if not iface_exists(args.iface):
        bad(f"no such interface: {args.iface}")
        return 1

    print(f"\n{DIM}=== monitor mode / injection verification ==={RST}\n")
    checks = []

    checks.append(("driver", check_mac80211(args.iface)))
    cap = enable_monitor(args.iface)
    print(f"       capture interface: {cap}")

    try:
        sock = open_sock(cap)
    except OSError as e:
        bad(f"cannot open raw socket on {cap}: {e}")
        return 1

    if args.listen:
        rc = 0 if do_listen(sock, args.seconds) else 1
        sock.close()
        return rc

    checks.append(("monitor rx", check_rx(sock)))
    mac = iface_mac(args.iface)
    checks.append(("injection", check_inject(sock, mac, args.count)))
    sock.close()

    print()
    failed = [n for n, r in checks if not r]
    if failed:
        bad(f"failed: {', '.join(failed)}")
        return 1
    ok("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
