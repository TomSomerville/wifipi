"""The beachedmesh packet header.

A packet is the end-to-end unit: it is copied unchanged from hop to hop, except
hop_limit which each relay decrements. The 802.11 frame around it is rebuilt by
every transmitter and carries nothing we rely on.

DATA payloads are always encrypted end to end. The header stays in the clear --
relays need src, dst, hop_limit and packet_id to route and dedup -- but they
never see the contents. ANNOUNCE is the one plaintext type, because it carries
the public keys everything else is bootstrapped from.

    offset  size  field
      0      4    magic       "BCHD"
      4      1    version
      5      1    type        DATA | ANNOUNCE | ACK
      6      1    flags       WANT_ACK
      7      2    hop_limit   decremented per relay; 0 = do not relay
      9      4    packet_id   random at origin, copied by relays
     13     16    src         originating node_id
     29     16    dst         node_id, or BROADCAST
    ────────────
     45 bytes + payload

hop_limit is two bytes because one caps the mesh at 255 relays -- around
1,000 km at realistic hop distances, which a network meant to span continents
without touching the internet would hit. 65,535 hops circles the Earth even at
1 km per hop, and latency makes anything past a few thousand unusable, so a
third byte would buy nothing.
"""

import os
import struct

MAGIC = b"BCHD"
VERSION = 1

HEADER_FMT = "!4sBBBHI16s16s"
HEADER_LEN = struct.calcsize(HEADER_FMT)   # 45

ID_LEN = 16
BROADCAST = b"\xff" * ID_LEN

# type: mutually exclusive, so an enum rather than bits
TYPE_DATA = 0x01
TYPE_ANNOUNCE = 0x02
TYPE_ACK = 0x03

_TYPE_NAMES = {TYPE_DATA: "DATA", TYPE_ANNOUNCE: "ANNOUNCE", TYPE_ACK: "ACK"}

# flags: properties that combine, so bits rather than an enum.
# There is deliberately no ENCRYPTED flag. DATA payloads are always encrypted,
# so a flag would only ever say "yes" -- and a flag that can say "no" is a
# downgrade waiting to happen. Unencrypted DATA is not representable.
FLAG_NO_ACK = 0x00      # 0000 0000 -- no bits set; fire and forget
FLAG_WANT_ACK = 0x01    # 0000 0001

DEFAULT_HOP_LIMIT = 3
MAX_HOP_LIMIT = 0xFFFF


class BadFrame(Exception):
    """Not one of ours, or malformed. Expected constantly in monitor mode."""


class Packet:
    __slots__ = ("type", "flags", "hop_limit", "packet_id", "src", "dst",
                 "payload", "version")

    def __init__(self, src, dst=BROADCAST, ptype=TYPE_DATA, payload=b"",
                 flags=FLAG_NO_ACK, hop_limit=DEFAULT_HOP_LIMIT, packet_id=None,
                 version=VERSION):
        if len(src) != ID_LEN or len(dst) != ID_LEN:
            raise ValueError(f"node ids must be {ID_LEN} bytes")
        self.src = src
        self.dst = dst
        self.type = ptype
        self.flags = flags
        self.hop_limit = hop_limit
        self.payload = payload
        self.version = version
        # Random, not a counter: a counter leaks how much a node has sent and
        # resets on reboot, colliding with entries still in others' dedup
        # caches and getting the packets silently dropped as duplicates.
        self.packet_id = os.urandom(4) if packet_id is None else packet_id
        if len(self.packet_id) != 4:
            raise ValueError("packet_id must be 4 bytes")

    # ---- wire format ----

    def to_bytes(self):
        return struct.pack(
            HEADER_FMT, MAGIC, self.version, self.type, self.flags,
            self.hop_limit, int.from_bytes(self.packet_id, "big"),
            self.src, self.dst,
        ) + self.payload

    @classmethod
    def from_bytes(cls, buf):
        if len(buf) < HEADER_LEN:
            raise BadFrame(f"short packet: {len(buf)} < {HEADER_LEN}")
        magic, ver, ptype, flags, hops, pid, src, dst = struct.unpack_from(
            HEADER_FMT, buf, 0)
        if magic != MAGIC:
            raise BadFrame("bad magic")
        if ver != VERSION:
            raise BadFrame(f"unsupported version {ver}")
        return cls(src=src, dst=dst, ptype=ptype, payload=buf[HEADER_LEN:],
                   flags=flags, hop_limit=hops,
                   packet_id=pid.to_bytes(4, "big"), version=ver)

    # ---- helpers ----

    @property
    def key(self):
        """Dedup identity. Must be stable across every hop, so it is the
        originator and the packet id -- never the previous hop."""
        return (self.src, self.packet_id)

    @property
    def is_broadcast(self):
        return self.dst == BROADCAST

    def relayed(self):
        """A copy with one hop spent, or None if it must not travel further."""
        if self.hop_limit <= 0:
            return None
        clone = Packet(src=self.src, dst=self.dst, ptype=self.type,
                       payload=self.payload, flags=self.flags,
                       hop_limit=self.hop_limit - 1,
                       packet_id=self.packet_id, version=self.version)
        return clone

    def __repr__(self):
        short = lambda b: b[:4].hex()
        name = _TYPE_NAMES.get(self.type, f"0x{self.type:02x}")
        dst = "broadcast" if self.is_broadcast else short(self.dst)
        return (f"<{name} {short(self.src)}->{dst} "
                f"id={self.packet_id.hex()} hops={self.hop_limit} "
                f"len={len(self.payload)}>")
