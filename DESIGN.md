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
