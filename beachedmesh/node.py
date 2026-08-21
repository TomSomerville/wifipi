"""The node: one process, one radio, everything composed.

A single process owns the radio because the pieces underneath only work if
they see *all* traffic. The dedup cache must observe every relay to suppress
duplicates; the route table must observe every announce to learn paths; the
flooder must observe its own transmissions coming back. Split across two
processes -- as the separate monitor and announce scripts did -- and each has
half a picture.

The loop is deliberately single-threaded. Everything it does is either
microseconds (parse, route lookup) or bounded milliseconds (verify a
signature, flush the write buffer), and the socket has a 2 MB receive buffer
to absorb the gaps. Threads would buy nothing and cost the guarantee that
nothing observes the tables mid-update.
"""

import time

from .flood import Flooder
from .frame import (BadFrame, DEFAULT_HOP_LIMIT, Packet, TYPE_ANNOUNCE,
                    _TYPE_NAMES)
from .identity import BadAnnounce, parse_announce
from .link import MonitorLink
from .routes import ANNOUNCE_INTERVAL, RouteTable

# How often to drop nodes past their TTL. Cheap, and doing it on a timer
# rather than on receive means a silent mesh still ages out.
EXPIRE_EVERY = 60.0

# Longest the loop will block waiting for a packet. Caps how late a pending
# relay or a due announce can fire when the air is quiet.
MAX_POLL = 0.25


class Node:
    """Composes link, flooder and routes into one poll loop.

    Owns no output of its own -- callers attach handlers -- so the daemon and
    tests drive the same object.
    """

    def __init__(self, iface, identity, announce_interval=ANNOUNCE_INTERVAL,
                 name="", db=None, hop_limit=DEFAULT_HOP_LIMIT):
        self.identity = identity
        self.node_id = identity.node_id
        self.name = name
        self.hop_limit = hop_limit
        self.announce_interval = announce_interval

        self.link = MonitorLink(iface)
        self.flooder = Flooder(own_id=self.node_id)
        self.routes = (RouteTable(own_id=self.node_id, db=db) if db is not None
                       else RouteTable(own_id=self.node_id))

        self._next_announce = 0.0        # announce immediately on start
        self._last_expire = time.monotonic()
        self.started = time.time()
        self.stats = {"received": 0, "announces": 0, "rejected": 0,
                      "relayed": 0, "sent": 0}

        # Callbacks: on_packet(pkt, meta, verdict), on_announce(info, pkt,
        # meta, verdict), on_event(kind, detail)
        self.on_packet = None
        self.on_announce = None
        self.on_event = None

    # ---- transmit ----

    def send_announce(self):
        pkt = Packet(src=self.node_id, ptype=TYPE_ANNOUNCE,
                     payload=self.identity.build_announce(self.name.encode()),
                     hop_limit=self.hop_limit)
        # Record it as seen so our own announce coming back from a relay is
        # recognised rather than treated as new traffic.
        self.flooder.seen.add(pkt.key)
        self.link.send(pkt.to_bytes())
        self.stats["sent"] += 1
        self.stats["announces"] += 1
        self._next_announce = time.monotonic() + self.announce_interval
        self._event("announce-sent", pkt)
        return pkt

    # ---- receive ----

    def _handle(self, data, meta):
        try:
            pkt = Packet.from_bytes(data)      # checks the BCHD magic
        except BadFrame:
            return None
        self.stats["received"] += 1

        rssi = meta.get("rssi")
        verdict = self.flooder.on_receive(pkt, rssi)

        # Duplicates are dropped for *relaying* but still carry information:
        # a second copy via a different neighbour is a second path. Learn from
        # it, but never re-verify -- the signature cannot have changed, and
        # verification is ~100x the cost of parsing.
        first_time = verdict in ("scheduled", "no-hops")

        if pkt.type == TYPE_ANNOUNCE:
            self._handle_announce(pkt, meta, rssi, verdict, first_time)
        elif self.on_packet:
            self.on_packet(pkt, meta, verdict)

        return pkt

    def _handle_announce(self, pkt, meta, rssi, verdict, first_time):
        hops = max(0, self.hop_limit - pkt.hop_limit)
        info = None

        if first_time:
            try:
                info = parse_announce(pkt.src, pkt.payload)
            except BadAnnounce as e:
                self.stats["rejected"] += 1
                self._event("announce-rejected", (pkt, str(e)))
                return
        elif verdict == "own":
            return

        # addr2 is the radio that handed us this frame, so it is the next hop
        # toward whoever originated the announce.
        route = self.routes.learn(
            pkt.src, meta["src_mac"], hops, rssi,
            sign_pub=info["sign_pub"] if info else None,
            encrypt_pub=info["encrypt_pub"] if info else None,
            name=info["app_data"].decode(errors="replace") if info else "")

        if self.on_announce:
            self.on_announce(info, pkt, meta, route, verdict)

    # ---- loop ----

    def poll(self, timeout=MAX_POLL):
        """One turn: read what is waiting, then do what is due.

        Sized so the loop wakes before the next pending relay -- a relay that
        fires late has already lost its race with the neighbours it was
        competing against.
        """
        deadline = self.flooder.next_deadline()
        if deadline is not None:
            timeout = max(0.0, min(timeout, deadline - time.monotonic()))

        got = self.link.recv()
        if got is not None:
            self._handle(*got)

        now = time.monotonic()

        # Relays whose contention timer expired. These are transmitted whether
        # or not anything arrived, which is why due() cannot live in the
        # receive branch.
        for relay in self.flooder.due():
            self.link.send(relay.to_bytes())
            self.stats["relayed"] += 1
            self.stats["sent"] += 1
            self._event("relayed", relay)

        if now >= self._next_announce:
            self.send_announce()

        self.routes.tick()          # flush buffered writes when due

        if now - self._last_expire >= EXPIRE_EVERY:
            dead = self.routes.expire()
            self._last_expire = now
            for nid in dead:
                self._event("expired", nid)

    def run(self):
        while True:
            self.poll()

    def close(self):
        self.routes.close()
        self.link.close()

    # ---- reporting ----

    def _event(self, kind, detail=None):
        if self.on_event:
            self.on_event(kind, detail)

    def status(self):
        up = time.time() - self.started
        f = self.flooder.stats
        return {
            "node_id": self.node_id.hex(),
            "name": self.name,
            "iface": self.link.ifname,
            "uptime": up,
            "hot_routes": len(self.routes),
            "neighbours": len(self.routes.neighbours()),
            "received": self.stats["received"],
            "sent": self.stats["sent"],
            "announces_sent": self.stats["announces"],
            "rejected": self.stats["rejected"],
            "relayed": f["relayed"],
            "cancelled": f["cancelled"],
            "duplicates": f["duplicates"],
            "seen_cache": len(self.flooder.seen),
            "next_announce": max(0.0, self._next_announce - time.monotonic()),
        }
