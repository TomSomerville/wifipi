"""Route table, built from announces.

Every verified announce teaches one thing: to reach the node that originated
it, hand the packet to whoever just transmitted it to us. The announce travels
the reverse of the path data will take, so the neighbour it arrived from is the
next hop.

That gives distance-vector routing -- each node knows a direction and a
distance for every destination, never the whole topology. A map would have to
be flooded and reflooded as nodes come and go; a direction is purely local
knowledge and heals on its own.

The table is persisted so a restart does not start blind, and every entry
carries the absolute time it was last heard from. On load, anything older than
a week is dropped.

A loaded next_hop is a hint, not a fact -- it says a neighbour could reach that
node at some point in the past, which a reboot or a move may have invalidated.
That resolves itself without special handling: a loaded entry is far older than
STALE_AFTER, so the first fresh announce displaces it unopposed.
"""

import json
import os
import tempfile
import time

# How long a node is remembered at all. A week, matching what survives a
# restart, so the table's memory is the same whether or not it was reloaded.
# Forgetting a node means losing its keys too, which costs an announce to
# relearn -- worth avoiding for anything that might come back.
DEFAULT_TTL = 7 * 24 * 3600.0

# How long a *path* is trusted, which is a much shorter question than how long
# a *node* is remembered. Past this, any working route displaces the one we
# hold: a short path through a neighbour that stopped answering is worse than
# a longer one that works. Wants to be a few announce intervals -- long enough
# to ride out a couple of lost announces, short enough to reroute quickly.
STALE_AFTER = 300.0

# Entries older than this are dropped when the table is loaded from disk.
MAX_AGE_ON_LOAD = DEFAULT_TTL

DEFAULT_PATH = "/var/lib/beachedmesh/routes.json"


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

    def age_str(self):
        """Compact age. Loaded entries can be days old, so seconds alone is
        unreadable at exactly the moment the number matters."""
        a = self.age()
        if a < 90:
            return f"{a:.0f}s"
        if a < 5400:
            return f"{a / 60:.0f}m"
        if a < 172800:
            return f"{a / 3600:.0f}h"
        return f"{a / 86400:.1f}d"

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
                        f"{rssi:>6} {r.age_str():>6} {r.announces:>5}  "
                        f"{r.name}")
        return "\n".join(rows)

    # ---- persistence ----

    def to_json(self):
        out = []
        for r in self._routes.values():
            out.append({
                "node_id": r.node_id.hex(),
                "next_hop": r.next_hop.hex(),
                "hops": r.hops,
                "rssi": r.rssi,
                "first_seen": r.first_seen,
                "last_seen": r.last_seen,
                "announces": r.announces,
                "sign_pub": r.sign_pub.hex() if r.sign_pub else None,
                "encrypt_pub": r.encrypt_pub.hex() if r.encrypt_pub else None,
                "name": r.name,
            })
        return {"version": 1, "saved": time.time(), "routes": out}

    def save(self, path=DEFAULT_PATH):
        """Write atomically: a half-written table read at boot is worse than
        none, and a node that loses power mid-write is not a rare event."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.to_json(), f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
        return len(self._routes)

    def load(self, path=DEFAULT_PATH, max_age=MAX_AGE_ON_LOAD):
        """Restore the table, dropping anything older than max_age.

        Returns (loaded, dropped). A corrupt or missing file is not an error:
        the table rebuilds from announces, so starting empty always works.
        """
        try:
            with open(path) as f:
                blob = json.load(f)
        except (OSError, ValueError):
            return (0, 0)

        cutoff = time.time() - max_age
        loaded = dropped = 0
        for e in blob.get("routes", []):
            try:
                if e["last_seen"] < cutoff:
                    dropped += 1
                    continue
                r = Route(
                    bytes.fromhex(e["node_id"]),
                    bytes.fromhex(e["next_hop"]),
                    e["hops"],
                    e.get("rssi"),
                    bytes.fromhex(e["sign_pub"]) if e.get("sign_pub") else None,
                    bytes.fromhex(e["encrypt_pub"]) if e.get("encrypt_pub") else None,
                    e.get("name", ""),
                )
                # Preserve the real timestamps; the constructor stamps "now",
                # which would make every loaded route look freshly heard.
                r.first_seen = e.get("first_seen", e["last_seen"])
                r.last_seen = e["last_seen"]
                r.announces = e.get("announces", 1)
                self._routes[r.node_id] = r
                loaded += 1
            except (KeyError, ValueError):
                dropped += 1
        return (loaded, dropped)
