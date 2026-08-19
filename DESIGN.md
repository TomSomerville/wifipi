# Design decisions

Running record of choices made and why, so they can be revisited deliberately
rather than rediscovered.

This is a self-contained protocol. Reticulum, Meshtastic, LoRa and Tor are
reference points for ideas that are already known to work -- they are not
dependencies and interoperating with them is a non-goal.

## Link layer

**Raw 802.11 data frames, no association.** ToDS=0, FromDS=0, with a magic
BSSID in address 3 so our frames are cheap to filter out of ambient traffic.
Nodes sit in monitor mode and inject.

**Channel 1, fixed.** Every node must be on the same channel to hear the
others. Channel hopping and rendezvous are a later problem.

**mac80211 owns the 802.11 sequence number.** TX_FLAG_NOSEQ is not set,
so the stack fills the field. The whole 802.11 header is rebuilt at every
hop and never authenticated end to end, and packet_id does dedup across
the mesh, so the field carries no meaning for us. TX_FLAG_NOACK stays --
broadcast frames should not be ACKed or retried.

**1 Mbps DSSS.** Roughly 15 dB better receiver sensitivity than 6 Mbps OFDM,
which is worth several times the range. Throughput is not the goal.
*Unverified:* whether an 802.11be part honours a legacy rate set via radiotap.

## Status

Bidirectional over-the-air comms confirmed between a Pi 4 and a laptop, both
using Netgear A9000 (MT7925U) adapters: each injects frames the other
receives, with no association and no access point. The link layer works.

**MTU 1500.** Chosen for Ethernet interoperability, so payloads can carry
ordinary IP traffic without fragmentation surprises.

Note the distinction: 1500 is the *ceiling*, not the target. Injected
broadcast frames get no ACK and no retransmission, and one bit error fails the
whole frame. At a 1e-5 bit error rate a 1500-byte frame lands 88.7% of the
time versus 96.1% for 500 bytes; at 1 Mbps it also occupies 12.2 ms of airtime
versus 4.2 ms. In a flood where several neighbours repeat everything, that
difference compounds. Keep normal traffic small; use the headroom when a
payload genuinely needs it.

## Frame format

Identifiers, both chosen to be readable in a hex dump:

- **BSSID** (802.11 addr3): `42 45 41 43 48 44` = `42:45:41:43:48:44`, "BEACHD".
  A MAC is 6 bytes, so "BEACHED" does not fit. `0x42` conveniently has the
  locally-administered bit set and the multicast bit clear, which is what an
  invented MAC should look like.
- **Body magic**: `42 43 48 44` = "BCHD".

The two filter at different levels. addr3 sits at a fixed offset early in the
frame, so the kernel can reject foreign traffic with a BPF filter before it is
ever copied to userspace -- worth doing, since monitor mode delivers every
frame in the air. The body magic is the userspace confirmation.

Header, big-endian, `!4sBBBBI16s16s`:

```
offset  size  field
  0      4    magic       "BCHD"
  4      1    version     start at 1
  5      1    type        0x01 DATA, 0x02 ANNOUNCE, 0x03 ACK
  6      1    flags       bit0 ENCRYPTED, bit1 WANT_ACK
  7      1    hop_limit   decremented per relay; 0 = do not relay
  8      4    packet_id   random at origin, copied by relays
 12     16    src         originating node_id
 28     16    dst         node_id, or ff*16 for broadcast
────────────
 44 bytes + payload
```

`type` is an enum because the values are mutually exclusive; `flags` is a
bitfield because its properties combine.

**hop_limit is mutated in flight**, so it cannot be covered by a signature or
AEAD tag -- the first relay would invalidate it. Authentication must span the
immutable fields only.

**packet_id is random, not a counter.** A counter leaks how many messages a
node has sent, and resets to zero on reboot, colliding with entries still in
other nodes' dedup caches and getting the packets silently dropped.

Deliberately absent: no length field (the 802.11 frame length already bounds
the payload), no checksum (802.11 FCS covers it in hardware), no previous-hop
address (802.11 addr2 gives it free), no sequence number (packet_id covers
dedup).

Cost on air: 24 (802.11) + 44 (ours) + 4 (FCS) = 72 bytes before payload,
about 576 us at 1 Mbps. Most of it is the 32 bytes of addressing.

## Deferred work

Decided, waiting on something else.

- **Set 802.11 addr2 from the node_id** once identity exists. Today it is the
  adapter's real MAC, which broadcasts the hardware identity to every listener
  and means the link-layer sender and the protocol sender are unrelated
  values. Using the first 6 bytes of the node_id makes them agree and gives
  the relay logic a free previous-hop identifier (addr2 is already in every
  frame, so nothing has to be added to our header to carry it).
  Blocked on: node_id generation.

- **Attach a BPF filter on addr3** so the kernel discards foreign frames
  before they reach userspace. Monitor mode delivers everything in the air,
  and filtering in Python means a process wakeup per frame.

## Addressing

Self-authenticating, derived from keys rather than assigned. Tor onion
addresses and Reticulum destination hashes both work this way; the idea is
worth borrowing even though we share nothing else with them.

```
identity = Ed25519 keypair (signing) + X25519 keypair (ECDH)
node_id  = SHA-256(ed25519_pubkey)[:16]
```

- **No allocation.** Generate a key, you have an address. No DHCP analogue,
  no collision detection, no authority.
- **No spoofing.** Claiming another node_id needs a key that hashes to it and
  the matching private key.
- **128 bits** gives a 2^64 birthday bound. Tor v2's 80-bit truncated SHA-1
  was broken by GPUs; v3 embeds the full key instead. 16 bytes is the
  Reticulum choice and is comfortable.

Full public keys in headers (Tor v3 style) were considered: 64 bytes of
src+dst per frame, 4.3% of a 1500 MTU. Affordable, but every relay pays it on
every frame to save one key lookup. Hashes win.

**Announce** frames carry the full public keys plus a signature; receivers
verify the hash matches before accepting.

## Announces and route tables

Modelled on Reticulum. Announces flood outward with dedup and a hop limit;
every node records which neighbour each announce arrived from and how many hops
it travelled. That gives "to reach X, hand the packet to whoever told me about
X" -- the announce travels the reverse of the path data will take.

Announces repeat periodically and entries expire, so the table self-heals: a
node that goes away stops announcing and ages out, and one that appears is
known within an announce interval.

ANNOUNCE payload (plaintext -- it carries the keys everything else bootstraps
from, and contains nothing sensitive):

```
32  ed25519 public key   verify this node's signatures
32  x25519 public key    encrypt to this node
10  random               makes every announce unique
 n  app data             optional: name, capabilities
64  ed25519 signature    over all of the above
```

**No counter, no persistent state.** Every announce carries fresh random bytes
inside the signature, so no two are identical -- the same approach Reticulum
takes.

Randomness alone does not stop replay: a bit-identical copy carries a valid
signature. What catches it is the duplicate-suppression cache flooding already
needs. A replayed announce has the same (src, packet_id) as the original and
is dropped as a duplicate.

Past the cache TTL a replay would be accepted and could briefly install a
bogus route, pointing traffic at a node that will drop it. It self-corrects at
the next genuine announce, since the real node keeps announcing and entries
expire. That is the price of holding no state, and it is worth paying: a
monotonic counter would have to be persisted as carefully as the private key,
and a reset would get every announce rejected as stale until it climbed past
the old value.

**Next hop is free.** 802.11 addr2 is the radio that transmitted the frame we
just received, so a route entry is `destination node_id -> next-hop MAC`.
Nothing extra has to be carried to support routing.

**Hops travelled is free too.** Announces start at a fixed hop_limit, so
`hops = INITIAL_ANNOUNCE_HOPS - received hop_limit`. No separate counter, and
it stays outside the signature since relays mutate hop_limit.

Route entry: `dest -> (next_hop_mac, hops, last_seen, rssi)`. Prefer fewest
hops, break ties on signal.

Data still floods until this is built; the route table is the optimisation
that stops every node repeating every packet.

## Encryption is mandatory

All DATA payloads are encrypted end to end. There is no ENCRYPTED flag: a flag
that can say "no" is a downgrade waiting to happen, so unencrypted DATA is not
representable in the wire format.

The header stays in the clear. Relays need src, dst, hop_limit and packet_id
to route and dedup, and they are not trusted with contents -- only the payload
is sealed.

Three cases, because "encrypt to everyone" is not a thing:

- **Unicast DATA** -- X25519 to the destination's public key, learned from its
  announce. Only that node can open it.
- **Broadcast DATA** -- AEAD under a shared channel key. This is group privacy
  against outsiders, not per-sender authenticity, so pair it with an Ed25519
  signature or bind the sender into the AEAD's associated data.
- **ANNOUNCE** -- plaintext by necessity: it carries the public keys that
  everything else bootstraps from. Signed, so it can be verified but not
  forged.

Open: whether ACK is encrypted (it names a packet_id, which a relay already
saw in the clear) and how the channel key is distributed.

## Crypto

**Signatures on announces, AEAD on data.** Ed25519 signatures are 64 bytes
plus a keypair operation per packet -- too expensive to put on every frame
when a Pi is relaying a flood. Derive a shared secret with X25519, then use
an AEAD whose 16-byte tag proves per-packet authenticity cheaply.

## Open questions

- Rate control: whether the 802.11be part honours the 1 Mbps legacy rate set
  via radiotap, or silently transmits at something else. Untested, and the
  range budget depends on it.
- Routing: managed flood with duplicate suppression is the obvious starting
  point, borrowing Meshtastic's approach. Contention timing, hop limits and
  whether to weight rebroadcast delay by signal strength are all unsettled.
- Framing: header layout and field widths, once addressing is fixed.
