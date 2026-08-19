"""Route table, built from announces.

Every verified announce teaches one thing: to reach the node that originated
it, hand the packet to whoever just transmitted it to us. The announce travels
the reverse of the path data will take, so the neighbour it arrived from is the
next hop.

That gives distance-vector routing -- each node knows a direction and a
distance for every destination, never the whole topology. A map would have to
be flooded and reflooded as nodes come and go; a direction is purely local
knowledge and heals on its own.

Nothing here is persisted. The table rebuilds itself from the next round of
announces, which is what makes a node that reboots, or one that wanders into
range, cost nothing to handle.
"""

import time

# A node that has not been heard from in this long is assumed gone. Wants to be
# a few announce intervals: long enough to survive a couple of lost announces,
# short enough that a departed node does not linger as a black hole.
DEFAULT_TTL = 300.0

# Below this age an entry is trusted enough that a longer path will not
# displace it. Past it, any working route is better than a stale short one.
STALE_AFTER = DEFAULT_TTL / 2


class Route:
    """What we know about reaching one node."""

    __slots__ = ("node_id", "next_hop", "hops", "rssi", "first_seen",
                 "last_seen", "announces", "sign_pub", "encrypt_pub", "name")

    def __init__(self, node_id, next_hop, hops, rssi, sign_pub=None,
                 encrypt_pub=None, name=""):
        now = time.time()
        self.node_id = node_id
        self.next_hop = next_hop      # MAC of the neighbour to hand packets to
        self.hops = hops
        self.rssi = rssi
        self.first_seen = now
        self.last_seen = now
        self.announces = 1
        self.sign_pub = sign_pub
        self.encrypt_pub = encrypt_pub
        self.name = name

    @property
    def is_neighbour(self):
        """Zero hops means it reached us directly, not via a relay."""
        return self.hops == 0

    def age(self):
        return time.time() - self.last_seen

    def __repr__(self):
        return (f"<Route {self.node_id.hex()[:8]} via "
                f"{':'.join(f'{b:02x}' for b in self.next_hop)} "
                f"hops={self.hops} rssi={self.rssi}>")


class RouteTable:
    def __init__(self, own_id=None, ttl=DEFAULT_TTL):
        self.own_id = own_id
        self.ttl = ttl
        self._routes = {}

    # ---- learning ----

    def learn(self, node_id, next_hop, hops, rssi=None, sign_pub=None,
              encrypt_pub=None, name=""):
        """Record what an announce taught us.

        Returns "self", "new", "better", "refreshed" or "kept" so a caller can
        report what changed without diffing the table.
        """
        if self.own_id is not None and node_id == self.own_id:
            return "self"        # our own announce, relayed back to us

        existing = self._routes.get(node_id)
        if existing is None:
            self._routes[node_id] = Route(node_id, next_hop, hops, rssi,
                                          sign_pub, encrypt_pub, name)
            return "new"

        existing.announces += 1
        if sign_pub:
            existing.sign_pub = sign_pub
            existing.encrypt_pub = encrypt_pub
        if name:
            existing.name = name

        # Same neighbour: this is just a fresher copy of what we already know.
        if next_hop == existing.next_hop:
            existing.hops = hops
            existing.rssi = rssi
            existing.last_seen = time.time()
            return "refreshed"

        # A different neighbour is offering a path. Take it if it is shorter,
        # or equally short but stronger, or if what we hold has gone stale --
        # a short route through a neighbour that has stopped answering is
        # worse than a longer one that works.
        better = (hops < existing.hops
                  or (hops == existing.hops and rssi is not None
                      and existing.rssi is not None and rssi > existing.rssi)
                  or existing.age() > STALE_AFTER)
        if better:
            existing.next_hop = next_hop
            existing.hops = hops
            existing.rssi = rssi
            existing.last_seen = time.time()
            return "better"

        return "kept"

    # ---- lookup ----

    def next_hop(self, node_id):
        """MAC to send to, or None if we have no route."""
        r = self._routes.get(node_id)
        return r.next_hop if r else None

    def get(self, node_id):
        return self._routes.get(node_id)

    def neighbours(self):
        return [r for r in self._routes.values() if r.is_neighbour]

    def expire(self):
        """Drop routes past their TTL. Returns what was removed."""
        cutoff = time.time() - self.ttl
        dead = [nid for nid, r in self._routes.items() if r.last_seen < cutoff]
        for nid in dead:
            del self._routes[nid]
        return dead

    def __len__(self):
        return len(self._routes)

    def __iter__(self):
        # Nearest first, then strongest -- the order you want to read.
        return iter(sorted(self._routes.values(),
                           key=lambda r: (r.hops, -(r.rssi or -127))))

    # ---- display ----

    def render(self):
        if not self._routes:
            return "  (no routes learned yet)"
        rows = [f"  {'node':<34} {'via':<19} {'hops':>4} {'rssi':>6} "
                f"{'age':>6} {'seen':>5}  name",
                f"  {'-' * 34} {'-' * 19} {'-' * 4} {'-' * 6} "
                f"{'-' * 6} {'-' * 5}  {'-' * 8}"]
        for r in self:
            via = ":".join(f"{b:02x}" for b in r.next_hop)
            rssi = f"{r.rssi}" if r.rssi is not None else "?"
            direct = "direct" if r.is_neighbour else f"{r.hops}"
            rows.append(f"  {r.node_id.hex():<34} {via:<19} {direct:>4} "
                        f"{rssi:>6} {r.age():>5.0f}s {r.announces:>5}  "
                        f"{r.name}")
        return "\n".join(rows)
