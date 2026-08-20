"""Route storage: a bounded hot table in memory over a full table on disk.

Every verified announce teaches one thing: to reach the node that originated
it, hand the packet to whoever just transmitted it to us. The announce travels
the reverse of the path data will take, so the neighbour it arrived from is the
next hop.

That gives distance-vector routing -- each node knows a direction and a
distance for every destination, never the whole topology. A map would have to
be flooded and reflooded as nodes come and go; a direction is purely local
knowledge and heals on its own.

Two tiers, because at 128 announce hops a node hears from hundreds of
thousands of others and cannot hold them all:

  hot   node_id -> (next_hop, hops, rssi, last_seen), packed to 13 bytes
        LRU, capped by a memory budget. Only what forwarding needs.

  cold  everything, in sqlite: the keys, the name, first_seen, announce
        counts. Indexed, so it stays fast at tens of millions of rows.

A miss in hot costs one indexed disk read, and only ever on first contact with
a node -- after that it is hot again. That read happens before any frame is
transmitted, so it never delays traffic already in flight.

Promotion happens on *every* lookup, including relaying for someone else. A
node on a busy path should keep the destinations it forwards for hot, not just
the ones it originates to.

The public keys deliberately live only in cold storage. They are 55% of a full
record, never needed to forward a packet, and needed exactly when you first
talk to a node -- which is when you are already paying for a disk read.
"""

import json
import os
import sqlite3
import struct
import tempfile
import time

# The announce interval everything else is sized against. Long, deliberately:
# announce traffic is the standing cost of a mesh, paid by every node forever,
# and flooding means each one is repeated by every node in range. Shorten it
# only for testing.
ANNOUNCE_INTERVAL = 6 * 3600.0

# How long a node is remembered at all -- a week, or roughly 28 announce
# intervals. Forgetting a node also loses its keys, which costs a full
# announce interval to relearn.
DEFAULT_TTL = 7 * 24 * 3600.0

# How long a *path* is trusted, which is a much shorter question than how long
# a *node* is remembered. Past this, any working route displaces the one we
# hold: a short path through a neighbour that stopped answering is worse than
# a longer one that works. Three announce intervals rides out two lost
# announces without thrashing between paths.
STALE_AFTER = 3 * ANNOUNCE_INTERVAL

# Memory budget for the hot table. A dict entry holding a 16-byte key and a
# packed 13-byte value measures ~52 bytes -- Python's per-entry overhead
# dwarfs the payload, which is why only routing fields live here.
#
# A plain dict, not an OrderedDict: since 3.7 dicts preserve insertion order,
# so LRU works by deleting and reinserting on hit, at the same 0.23 us but
# half the memory (OrderedDict costs 105 B/entry for its linked list).
MEMORY_BUDGET = 100 * 1024 * 1024
BYTES_PER_ENTRY = 52
MAX_HOT_ROUTES = MEMORY_BUDGET // BYTES_PER_ENTRY

# next_hop, hops, rssi, last_seen. Seconds as an int: routes are compared at
# minute granularity at best, so a float buys nothing.
ROUTE_FMT = "!6sHbI"
ROUTE_LEN = struct.calcsize(ROUTE_FMT)

# Writes are buffered rather than committed per announce. At realistic rates
# the timer always fires first; the size cap is a safety valve against a burst.
FLUSH_SECONDS = 5.0
FLUSH_ENTRIES = 10_000

DEFAULT_DB = "/var/lib/beachedmesh/routes.db"


class Route:
    """A view of one route. Built on demand -- never what is stored."""

    __slots__ = ("node_id", "next_hop", "hops", "rssi", "last_seen",
                 "first_seen", "announces", "sign_pub", "encrypt_pub", "name")

    def __init__(self, node_id, next_hop, hops, rssi, last_seen,
                 first_seen=None, announces=1, sign_pub=None,
                 encrypt_pub=None, name=""):
        self.node_id = node_id
        self.next_hop = next_hop
        self.hops = hops
        self.rssi = rssi
        self.last_seen = last_seen
        self.first_seen = first_seen if first_seen is not None else last_seen
        self.announces = announces
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
        a = self.age()
        if a < 90:
            return f"{a:.0f}s"
        if a < 5400:
            return f"{a / 60:.0f}m"
        if a < 172800:
            return f"{a / 3600:.0f}h"
        return f"{a / 86400:.1f}d"

    def __repr__(self):
        via = ":".join(f"{b:02x}" for b in self.next_hop)
        return (f"<Route {self.node_id.hex()[:8]} via {via} "
                f"hops={self.hops} rssi={self.rssi}>")


class ColdStore:
    """Every route ever heard, on disk, indexed.

    sqlite rather than a flat file because the table is meant to reach tens of
    millions of rows: an appended file gives O(n) lookups and turns the 7-day
    purge into a full rewrite, where an index gives ~10 us lookups and makes
    the purge a single DELETE.
    """

    def __init__(self, path=DEFAULT_DB):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        # WAL keeps readers going during a flush, which matters because the
        # flush happens on the same thread that is servicing packets.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS routes(
                node_id     BLOB PRIMARY KEY,
                next_hop    BLOB NOT NULL,
                hops        INTEGER NOT NULL,
                rssi        INTEGER,
                first_seen  REAL NOT NULL,
                last_seen   REAL NOT NULL,
                announces   INTEGER NOT NULL DEFAULT 1,
                sign_pub    BLOB,
                encrypt_pub BLOB,
                name        TEXT
            )""")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_last_seen "
                        "ON routes(last_seen)")
        self.db.commit()

        self._buf = {}
        self._last_flush = time.monotonic()

    def buffer(self, node_id, next_hop, hops, rssi, last_seen,
               sign_pub=None, encrypt_pub=None, name=""):
        # Merge rather than replace. The buffer is keyed by node_id, so
        # several announces for one node collapse into a single row before
        # the flush -- and only the first carries keys, since a repeat
        # announce is usually processed without re-verifying. Overwriting
        # would silently discard them.
        prev = self._buf.get(node_id)
        if prev is not None:
            _, _, _, _, p_sign, p_enc, p_name = prev
            sign_pub = sign_pub or p_sign
            encrypt_pub = encrypt_pub or p_enc
            name = name or p_name
        self._buf[node_id] = (next_hop, hops, rssi, last_seen,
                              sign_pub, encrypt_pub, name)
        if len(self._buf) >= FLUSH_ENTRIES:
            self.flush()

    def maybe_flush(self):
        if self._buf and time.monotonic() - self._last_flush >= FLUSH_SECONDS:
            return self.flush()
        return 0

    def flush(self):
        if not self._buf:
            self._last_flush = time.monotonic()
            return 0
        rows = [(nid, nh, hops, rssi, seen, seen, sp, ep, nm)
                for nid, (nh, hops, rssi, seen, sp, ep, nm) in self._buf.items()]
        # Preserve first_seen and accumulate the announce count on conflict:
        # the row already on disk knows when we first heard this node.
        self.db.executemany("""
            INSERT INTO routes(node_id, next_hop, hops, rssi, first_seen,
                               last_seen, sign_pub, encrypt_pub, name)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(node_id) DO UPDATE SET
                next_hop=excluded.next_hop, hops=excluded.hops,
                rssi=excluded.rssi, last_seen=excluded.last_seen,
                announces=routes.announces + 1,
                sign_pub=COALESCE(excluded.sign_pub, routes.sign_pub),
                encrypt_pub=COALESCE(excluded.encrypt_pub, routes.encrypt_pub),
                name=CASE WHEN excluded.name != '' THEN excluded.name
                          ELSE routes.name END
            """, rows)
        self.db.commit()
        n = len(self._buf)
        self._buf.clear()
        self._last_flush = time.monotonic()
        return n

    def get(self, node_id):
        """Full record, or None. Checks the unflushed buffer first."""
        b = self._buf.get(node_id)
        if b is not None:
            nh, hops, rssi, seen, sp, ep, nm = b
            return Route(node_id, nh, hops, rssi, seen, sign_pub=sp,
                         encrypt_pub=ep, name=nm or "")
        row = self.db.execute(
            "SELECT next_hop,hops,rssi,first_seen,last_seen,announces,"
            "sign_pub,encrypt_pub,name FROM routes WHERE node_id=?",
            (node_id,)).fetchone()
        if row is None:
            return None
        nh, hops, rssi, first, last, ann, sp, ep, nm = row
        return Route(node_id, nh, hops, rssi, last, first, ann, sp, ep, nm or "")

    def recent(self, limit):
        """Most recently heard routes, for warming the hot table at start."""
        return self.db.execute(
            "SELECT node_id,next_hop,hops,rssi,last_seen FROM routes "
            "ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()

    def expire(self, ttl=DEFAULT_TTL):
        cur = self.db.execute("DELETE FROM routes WHERE last_seen < ?",
                              (time.time() - ttl,))
        self.db.commit()
        return cur.rowcount

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM routes").fetchone()[0]

    def size_bytes(self):
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def close(self):
        self.flush()
        self.db.close()


class RouteTable:
    def __init__(self, own_id=None, ttl=DEFAULT_TTL, db=DEFAULT_DB,
                 max_hot=MAX_HOT_ROUTES):
        self.own_id = own_id
        self.ttl = ttl
        self.max_hot = max_hot
        self.cold = ColdStore(db) if db else None
        # node_id -> packed(next_hop, hops, rssi, last_seen). Ordered so the
        # least recently used entry is the one evicted.
        self._hot = {}
        self.stats = {"hot_hits": 0, "cold_hits": 0, "misses": 0,
                      "evictions": 0}

    # ---- learning ----

    def learn(self, node_id, next_hop, hops, rssi=None, sign_pub=None,
              encrypt_pub=None, name=""):
        """Record what an announce taught us.

        Returns "self", "new", "better", "refreshed" or "kept".
        """
        if self.own_id is not None and node_id == self.own_id:
            return "self"        # our own announce, relayed back to us

        now = time.time()
        existing = self._hot.get(node_id)
        verdict = "new"

        if existing is not None:
            old_hop, old_hops, old_rssi, old_seen = struct.unpack(
                ROUTE_FMT, existing)
            if next_hop == old_hop:
                verdict = "refreshed"
            else:
                # A different neighbour is offering a path. Take it if it is
                # shorter, or equally short but stronger, or if what we hold
                # has gone stale -- a short route through a neighbour that
                # stopped answering is worse than a longer one that works.
                better = (hops < old_hops
                          or (hops == old_hops and rssi is not None
                              and rssi > old_rssi)
                          or (now - old_seen) > STALE_AFTER)
                if not better:
                    # Still worth recording that we heard it.
                    if self.cold:
                        self.cold.buffer(node_id, old_hop, old_hops, old_rssi,
                                         now, sign_pub, encrypt_pub, name)
                    return "kept"
                verdict = "better"

        self._put_hot(node_id, next_hop, hops, rssi, now)
        if self.cold:
            self.cold.buffer(node_id, next_hop, hops, rssi, now,
                             sign_pub, encrypt_pub, name)
        return verdict

    def _put_hot(self, node_id, next_hop, hops, rssi, last_seen):
        # Delete first so reinserting moves it to the end: dicts keep
        # insertion order, so the oldest key is simply the first one.
        self._hot.pop(node_id, None)
        self._hot[node_id] = struct.pack(
            ROUTE_FMT, next_hop, hops, -128 if rssi is None else int(rssi),
            int(last_seen))
        while len(self._hot) > self.max_hot:
            del self._hot[next(iter(self._hot))]    # least recently used
            self.stats["evictions"] += 1

    # ---- lookup ----

    def next_hop(self, node_id):
        """MAC to send to, or None. Promotes on hit, including when relaying
        for someone else -- a node on a busy path should keep the destinations
        it forwards for hot, not only the ones it talks to itself."""
        v = self._hot.pop(node_id, None)
        if v is not None:
            self._hot[node_id] = v          # reinsert == most recently used
            self.stats["hot_hits"] += 1
            return struct.unpack(ROUTE_FMT, v)[0]

        if self.cold:
            r = self.cold.get(node_id)
            if r is not None:
                self.stats["cold_hits"] += 1
                self._put_hot(r.node_id, r.next_hop, r.hops, r.rssi,
                              r.last_seen)
                return r.next_hop

        self.stats["misses"] += 1
        return None

    def get(self, node_id, full=False):
        """A Route view. full=True fetches keys and counts from disk."""
        if full and self.cold:
            r = self.cold.get(node_id)
            if r is not None:
                return r
        v = self._hot.get(node_id)
        if v is None:
            return self.cold.get(node_id) if self.cold else None
        nh, hops, rssi, seen = struct.unpack(ROUTE_FMT, v)
        return Route(node_id, nh, hops, None if rssi == -128 else rssi, seen)

    def keys_for(self, node_id):
        """(sign_pub, encrypt_pub) from cold storage, or (None, None).

        Needed only when actually talking to a node, which is also when a cold
        read is already being paid for.
        """
        if not self.cold:
            return (None, None)
        r = self.cold.get(node_id)
        return (r.sign_pub, r.encrypt_pub) if r else (None, None)

    def neighbours(self):
        return [r for r in self if r.is_neighbour]

    def warm(self, limit=None):
        """Fill the hot table from the most recently heard cold rows."""
        if not self.cold:
            return 0
        limit = limit or self.max_hot
        n = 0
        for node_id, nh, hops, rssi, last in self.cold.recent(limit):
            self._put_hot(node_id, nh, hops, rssi, last)
            n += 1
        return n

    # ---- maintenance ----

    def tick(self):
        """Call each loop turn. Flushes buffered writes when due."""
        return self.cold.maybe_flush() if self.cold else 0

    def expire(self):
        """Drop routes past the TTL, in memory and on disk."""
        cutoff = time.time() - self.ttl
        dead = [nid for nid, v in self._hot.items()
                if struct.unpack(ROUTE_FMT, v)[3] < cutoff]
        for nid in dead:
            del self._hot[nid]
        if self.cold:
            self.cold.expire(self.ttl)
        return dead

    def close(self):
        if self.cold:
            self.cold.close()

    # ---- inspection ----

    def __len__(self):
        return len(self._hot)

    def __iter__(self):
        out = []
        for node_id, v in self._hot.items():
            nh, hops, rssi, seen = struct.unpack(ROUTE_FMT, v)
            out.append(Route(node_id, nh, hops,
                             None if rssi == -128 else rssi, seen))
        # Nearest first, then strongest -- the order you want to read.
        return iter(sorted(out, key=lambda r: (r.hops, -(r.rssi or -127))))

    def memory_bytes(self):
        return len(self._hot) * BYTES_PER_ENTRY

    def render(self, limit=40):
        if not self._hot:
            return "  (no routes learned yet)"
        rows = [f"  {'node':<34} {'via':<19} {'hops':>4} {'rssi':>6} {'age':>6}",
                f"  {'-'*34} {'-'*19} {'-'*4} {'-'*6} {'-'*6}"]
        for i, r in enumerate(self):
            if i >= limit:
                rows.append(f"  ... and {len(self._hot) - limit:,} more")
                break
            via = ":".join(f"{b:02x}" for b in r.next_hop)
            rssi = f"{r.rssi}" if r.rssi is not None else "?"
            hops = "direct" if r.is_neighbour else f"{r.hops}"
            rows.append(f"  {r.node_id.hex():<34} {via:<19} {hops:>4} "
                        f"{rssi:>6} {r.age_str():>6}")
        return "\n".join(rows)

    def summary(self):
        s = self.stats
        cold_n = self.cold.count() if self.cold else 0
        cold_mb = self.cold.size_bytes() / 1e6 if self.cold else 0
        return (f"hot {len(self._hot):,}/{self.max_hot:,} "
                f"({self.memory_bytes()/1e6:.1f} MB)   "
                f"cold {cold_n:,} ({cold_mb:.1f} MB)   "
                f"hits {s['hot_hits']:,}h/{s['cold_hits']:,}c "
                f"miss {s['misses']:,} evict {s['evictions']:,}")
