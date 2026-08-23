"""The radio: raw 802.11 frames in and out of a monitor-mode interface.

Sends and receives beachedmesh packet bytes. Everything 802.11-specific lives here,
so nothing above this layer needs to know about radiotap, MAC headers or FCS.

A frame on the wire looks like:

    [ radiotap | 802.11 header | packet | FCS ]
       local        24 bytes                4

Radiotap is metadata between us and the driver -- we write it on transmit to
ask for a rate and channel, the driver writes it on receive to report signal
and rate. It never goes on the air.
"""

import socket
import struct

ETH_P_ALL = 0x0003

CHANNEL = 1
# 11 Mbps DSSS -- the fastest DSSS rate, and the design requirement is 10 Mbps.
#
# Worth knowing why this beats 12 Mbps OFDM, which is nominally faster: DSSS
# spreads each symbol across an 11-chip sequence, and that processing gain is
# worth ~10 dB at the receiver. 11 Mbps decodes at -89 dBm where 12 Mbps OFDM
# needs -79 -- three times the range for 1 Mbps less rate.
#
# The cost against 1 Mbps DSSS is 8 dB, roughly 40% of the range. That is the
# price of the throughput requirement, not an accident.
RATE_MBPS = 11.0
BROADCAST_MAC = b"\xff" * 6
BSSID = bytes.fromhex("424541434844")   # "BEACHD"

# radiotap TX flags
TX_NOACK = 0x0008          # broadcast is never acknowledged, so do not retry
# TX_NOSEQ deliberately unset: mac80211 owns the sequence number.

# radiotap RX flags
F_FCS_AT_END = 0x10

FC_DATA = 0x08             # version 0, type data, subtype 0
DOT11_HDR_LEN = 24


def _radiotap_tx(channel=CHANNEL, rate_mbps=RATE_MBPS):
    freq = 2484 if channel == 14 else 2407 + channel * 5
    chan_flags = 0x0080 | (0x0020 if rate_mbps <= 11 else 0x0040)
    present = (1 << 2) | (1 << 3) | (1 << 15)   # rate, channel, tx flags
    return struct.pack(
        "<BBHIBBHHH", 0, 0, 16, present,
        int(rate_mbps * 2), 0, freq, chan_flags, TX_NOACK,
    )


# present-bit -> (alignment, size), enough to reach signal strength
_RT_FIELDS = {
    0: (8, 8),   # TSFT
    1: (1, 1),   # FLAGS
    2: (1, 1),   # RATE
    3: (2, 4),   # CHANNEL
    4: (2, 2),   # FHSS
    5: (1, 1),   # DBM_ANTSIGNAL
    6: (1, 1),   # DBM_ANTNOISE
}


def parse_radiotap(buf):
    """Return (meta, rest_of_frame). Raises ValueError on malformed input."""
    if len(buf) < 8:
        raise ValueError("short radiotap")
    _ver, _pad, it_len, present = struct.unpack_from("<BBHI", buf, 0)
    if it_len < 8 or it_len > len(buf):
        raise ValueError("bad radiotap length")

    off = 8
    # Chained present words: bit 31 says another follows.
    first = present
    while present & (1 << 31):
        if off + 4 > it_len:
            raise ValueError("truncated radiotap")
        (present,) = struct.unpack_from("<I", buf, off)
        off += 4

    meta = {}
    for bit, (align, size) in _RT_FIELDS.items():
        if not (first & (1 << bit)):
            continue
        off = (off + align - 1) & ~(align - 1)
        if off + size > it_len:
            break
        if bit == 1:
            meta["flags"] = buf[off]
        elif bit == 2:
            meta["rate_mbps"] = buf[off] / 2.0
        elif bit == 3:
            meta["freq"] = struct.unpack_from("<H", buf, off)[0]
        elif bit == 5:
            meta["rssi"] = struct.unpack_from("<b", buf, off)[0]
        elif bit == 6:
            meta["noise"] = struct.unpack_from("<b", buf, off)[0]
        off += size

    return meta, buf[it_len:]


class MonitorLink:
    """Send and receive packet bytes on a monitor-mode interface."""

    def __init__(self, ifname, timeout=1.0):
        self.ifname = ifname
        self.mac = self._hw_addr(ifname)
        self.sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                                  socket.htons(ETH_P_ALL))
        self.sock.bind((ifname, ETH_P_ALL))
        self.sock.settimeout(timeout)
        # Ambient traffic is heavy in monitor mode; a large buffer stops our
        # own packets being dropped while we are busy handling one.
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 21)
        self._rt = _radiotap_tx()

    @staticmethod
    def _hw_addr(ifname):
        with open(f"/sys/class/net/{ifname}/address") as f:
            return bytes.fromhex(f.read().strip().replace(":", ""))

    def _dot11(self):
        # ToDS=0, FromDS=0 -- the IBSS form, no access point involved.
        # Sequence control left 0: mac80211 fills it in.
        return struct.pack("<BBH6s6s6sH", FC_DATA, 0x00, 0,
                           BROADCAST_MAC, self.mac, BSSID, 0)

    def send(self, packet_bytes):
        self.sock.send(self._rt + self._dot11() + packet_bytes)

    def recv(self):
        """One of our packets, or None. Returns (packet_bytes, meta)."""
        try:
            buf = self.sock.recv(4096)
        except socket.timeout:
            return None
        if not buf:
            return None

        try:
            meta, frame = parse_radiotap(buf)
        except ValueError:
            return None

        # Keep every layer. Nothing above needs radiotap to route a packet,
        # but a troubleshooting tool has to be able to show the whole frame.
        meta["raw"] = buf
        meta["radiotap"] = buf[:len(buf) - len(frame)]

        # The driver may or may not hand up the trailing checksum; radiotap
        # FLAGS bit 0x10 says which. Keep it rather than discard it, so
        # callers can show the whole frame.
        fcs = b""
        if meta.get("flags", 0) & F_FCS_AT_END and len(frame) >= 4:
            frame, fcs = frame[:-4], frame[-4:]
        if len(frame) <= DOT11_HDR_LEN:
            return None
        if frame[0] != FC_DATA or frame[16:22] != BSSID:
            return None      # not ours -- the overwhelming majority

        meta["src_mac"] = frame[10:16]
        meta["dot11"] = frame[:DOT11_HDR_LEN]
        meta["fcs"] = fcs
        return frame[DOT11_HDR_LEN:], meta

    def fileno(self):
        return self.sock.fileno()

    def close(self):
        self.sock.close()
