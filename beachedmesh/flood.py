"""Managed flood: relay what you hear, once, without melting the channel.

Two mechanisms, solving two different problems that are easy to confuse.

**Duplicate suppression** stops packets circulating forever. packet_id is
chosen by the originator and copied unchanged by every relay, so every copy
anywhere in the mesh carries the same (src, packet_id). A node that has
already handled that pair drops it. Without this, four neighbours hearing one
announce would bounce it between themselves until the hop limit ran out.

**Counter cancellation** stops redundant coverage, which duplicate suppression
does nothing about. Dedup means each of B, C, D and E relays an announce
exactly once -- but that is still four transmissions covering nearly the same
ground. So a relay is scheduled after a random delay, and cancelled if enough
other nodes are heard relaying it first. Transmissions per neighbourhood cap
at the threshold rather than growing with density: 40 neighbours produce 3
relays instead of 40.

The cancellation must count *relays*, not the original. A node cancels because
a neighbour already forwarded the packet, never because it heard the sender --
otherwise nobody would ever relay anything.
"""

import random
import time
from collections import OrderedDict

# How many other relays we must hear before dropping our own. Higher is more
# redundant and more robust to loss; lower is quieter. Three is the usual
# recommendation from the broadcast-storm literature.
RELAY_THRESHOLD = 3

# Window a relay waits in before transmitting. Long enough that other nodes'
# relays arrive and can cancel ours -- at 1 Mbps an announce is ~2 ms on air,
# so tens of milliseconds is ample.
CW_MIN = 0.020
CW_MAX = 0.200

# Signal range mapped onto that window. A node that heard the sender weakly is
# probably far away, so it waits less and relays first, covering the most
# ground the original did not reach. Nodes that heard it strongly wait longer
# and usually end up cancelling.
RSSI_WEAK = -95.0
RSSI_STRONG = -50.0


def relay_delay(rssi=None):
    """Weak signal -> short delay -> distant nodes relay first."""
    if rssi is None:
        return random.uniform(CW_MIN, CW_MAX)
    span = RSSI_STRONG - RSSI_WEAK
    frac = (max(RSSI_WEAK, min(RSSI_STRONG, float(rssi))) - RSSI_WEAK) / span
    base = CW_MIN + frac * (CW_MAX - CW_MIN)
    # Jitter so two nodes at the same distance do not collide every time.
    jitter = (CW_MAX - CW_MIN) * 0.1
    return max(0.0, base + random.uniform(-jitter, jitter))


class SeenCache:
    """Bounded LRU of packet identities, with a time floor for late copies."""

    def __init__(self, capacity=4096, ttl=600.0):
        self.capacity = capacity
        self.ttl = ttl
        self._d = OrderedDict()

    def seen(self, key):
        ts = self._d.get(key)
        if ts is None:
            return False
        if time.monotonic() - ts > self.ttl:
            del self._d[key]
            return False
        self._d.move_to_end(key)
        return True

    def add(self, key):
        self._d[key] = time.monotonic()
        self._d.move_to_end(key)
        # Bounded so a node announcing fabricated ids cannot grow it forever.
        while len(self._d) > self.capacity:
            self._d.popitem(last=False)

    def __len__(self):
        return len(self._d)


class Pending:
    """A relay waiting on its timer."""

    __slots__ = ("packet", "send_at", "heard")

    def __init__(self, packet, send_at):
        self.packet = packet
        self.send_at = send_at
        self.heard = 0          # other relays of this packet heard so far


class Flooder:
    """Decides what to relay, when, and what to drop.

    Owns no radio and no clock beyond time.monotonic, so it can be driven
    directly in tests.
    """

    def __init__(self, own_id=None, threshold=RELAY_THRESHOLD, seen=None):
        self.own_id = own_id
        self.threshold = threshold
        self.seen = seen or SeenCache()
        self.pending = {}
        self.stats = {"relayed": 0, "cancelled": 0, "duplicates": 0,
                      "expired_hops": 0}

    def on_receive(self, pkt, rssi=None):
        """Classify an inbound packet.

        Returns one of:
          "own"        our own packet coming back
          "duplicate"  already handled; if a relay was pending it is cancelled
          "cancelled"  a pending relay of ours just hit the threshold
          "scheduled"  queued for relay after a delay
          "no-hops"    ours to deliver, but not to forward
        """
        key = pkt.key

        if self.own_id is not None and pkt.src == self.own_id:
            self.pending.pop(key, None)
            return "own"

        # A copy of something we already have. If we are still waiting to
        # relay it, this is another node doing the job for us -- count it, and
        # drop ours once enough have.
        if key in self.pending:
            p = self.pending[key]
            p.heard += 1
            if p.heard >= self.threshold:
                del self.pending[key]
                self.stats["cancelled"] += 1
                return "cancelled"
            return "duplicate"

        if self.seen.seen(key):
            self.stats["duplicates"] += 1
            return "duplicate"

        self.seen.add(key)

        relayed = pkt.relayed()
        if relayed is None:
            self.stats["expired_hops"] += 1
            return "no-hops"

        self.pending[key] = Pending(relayed, time.monotonic() + relay_delay(rssi))
        return "scheduled"

    def due(self):
        """Relays whose timer has expired. Caller transmits them."""
        now = time.monotonic()
        ready = [k for k, p in self.pending.items() if p.send_at <= now]
        out = []
        for k in ready:
            out.append(self.pending.pop(k).packet)
            self.stats["relayed"] += 1
        return out

    def next_deadline(self):
        """When due() next has work, or None. For sizing a select timeout."""
        if not self.pending:
            return None
        return min(p.send_at for p in self.pending.values())
