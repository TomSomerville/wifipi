"""Control socket: a window into the running daemon.

The route database on disk shows what a node has learned, but not what it
currently holds -- the hot table has an LRU cap the disk does not, and up to
five seconds of writes are still buffered. Flooder counters and pending relays
exist only in memory and never reach disk at all.

So the daemon listens on a unix socket and answers questions about itself.
One line of JSON in, one line of JSON out, connection closed. No streaming, no
sessions: a request that cannot be served in a single non-blocking pass has no
business being on the packet loop's thread.

Serving is polled from that same loop rather than threaded. Every response is
built from data the loop already owns, so a thread would need locks around
structures that are otherwise touched by exactly one thread -- paying real
complexity to answer a question a human asks once a minute.
"""

import json
import os
import socket
import stat
import time

DEFAULT_SOCKET = "/run/beachedmesh.sock"

# A response has to fit one non-blocking write. Route dumps are capped rather
# than paginated; a human reading a terminal does not want 2 million rows.
MAX_ROUTES = 500


class ControlServer:
    """Answers status queries. Polled, never blocking."""

    def __init__(self, node, path=DEFAULT_SOCKET):
        self.node = node
        self.path = path
        self.sock = None
        self.error = None
        try:
            self._listen()
        except OSError as e:
            # A daemon that cannot open its control socket should still route
            # packets. Losing introspection is not worth losing the mesh.
            self.error = str(e)

    def _listen(self):
        if os.path.exists(self.path):
            os.unlink(self.path)
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        self.sock.listen(4)
        self.sock.setblocking(False)
        # World-readable: inspecting a node is not privileged, and requiring
        # root to read a route table makes people run everything as root.
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR |
                 stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH)

    def poll(self):
        """Serve whatever is waiting. Returns how many requests were handled."""
        if self.sock is None:
            return 0
        served = 0
        while True:
            try:
                conn, _ = self.sock.accept()
            except (BlockingIOError, OSError):
                return served
            try:
                conn.settimeout(0.5)
                raw = conn.recv(4096).decode().strip()
                req = json.loads(raw) if raw else {}
                resp = self.handle(req)
                conn.sendall((json.dumps(resp) + "\n").encode())
            except (OSError, ValueError) as e:
                try:
                    conn.sendall((json.dumps({"error": str(e)}) + "\n").encode())
                except OSError:
                    pass
            finally:
                conn.close()
            served += 1

    # ---- queries ----

    def handle(self, req):
        cmd = req.get("cmd", "status")
        fn = getattr(self, f"_cmd_{cmd}", None)
        if fn is None:
            return {"error": f"unknown command: {cmd}"}
        return fn(req)

    def _cmd_status(self, req):
        return self.node.status()

    def _cmd_routes(self, req):
        n = self.node
        limit = min(int(req.get("limit", 40)), MAX_ROUTES)
        want_neighbours = req.get("neighbours", False)
        out = []
        for r in n.routes:
            if want_neighbours and not r.is_neighbour:
                continue
            out.append({
                "node_id": r.node_id.hex(),
                "next_hop": r.next_hop.hex(),
                "hops": r.hops,
                "rssi": r.rssi,
                "age": round(r.age(), 1),
            })
            if len(out) >= limit:
                break
        return {
            "routes": out,
            "hot": len(n.routes),
            "max_hot": n.routes.max_hot,
            "memory_bytes": n.routes.memory_bytes(),
            "truncated": len(n.routes) > len(out),
        }

    def _cmd_flood(self, req):
        f = self.node.flooder
        return {
            **f.stats,
            "pending": len(f.pending),
            "seen_cache": len(f.seen),
            "seen_capacity": f.seen.capacity,
            "threshold": f.threshold,
            # Pending relays are the most perishable state in the daemon:
            # they exist for tens of milliseconds and never touch disk.
            "pending_detail": [
                {"src": p.packet.src.hex(),
                 "packet_id": p.packet.packet_id.hex(),
                 "hops_left": p.packet.hop_limit,
                 "heard": p.heard,
                 "in_ms": round((p.send_at - time.monotonic()) * 1000, 1)}
                for p in list(self.node.flooder.pending.values())[:20]
            ],
        }

    def _cmd_announce(self, req):
        """Send an announce now rather than waiting for the timer."""
        pkt = self.node.send_announce()
        return {"sent": True, "packet_id": pkt.packet_id.hex(),
                "bytes": len(pkt.to_bytes())}

    def close(self):
        if self.sock is not None:
            self.sock.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


def query(cmd="status", path=DEFAULT_SOCKET, timeout=2.0, **kw):
    """Ask a running daemon. Raises ConnectionError if none is listening."""
    if not os.path.exists(path):
        raise ConnectionError(f"no daemon socket at {path}")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
        s.sendall((json.dumps({"cmd": cmd, **kw}) + "\n").encode())
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
            if b.endswith(b"\n"):
                break
        return json.loads(b"".join(chunks).decode())
    except OSError as e:
        raise ConnectionError(f"{path}: {e}") from e
    finally:
        s.close()
