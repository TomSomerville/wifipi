# wifipi

An association-free mesh network over 802.11. Nodes never join an access
point, never associate, and never touch the kernel networking stack — each
sits in monitor mode on a fixed channel and injects raw 802.11 frames that
every other node in range receives directly.

See [DESIGN.md](DESIGN.md) for the protocol and the reasoning behind it.

## Status

Announces work. A node generates an identity, broadcasts a signed announce,
and another node receives it, checks the address really belongs to the key,
and verifies the signature. Routing is not written yet.

## What is here

```
bin/wifipi-setup      one-time: generate this node's identity
bin/wifipi-monitor    verify the adapter, then monitor our traffic
bin/wifipi-announce   broadcast an announce

wifipi/frame.py       the packet header: build and parse
wifipi/identity.py    keys, node_id, announce signing and verification
wifipi/link.py        radiotap, 802.11 framing, raw socket
```

## Hardware

**Netgear A9000** USB adapters (MediaTek MT7925U, driver `mt7925u`) on
Raspberry Pi or any Linux box, with **kernel 6.18+** — the driver landed in
6.7 but this adapter's USB id only in 6.18.

The onboard Broadcom radio on a Pi cannot do this. Its 802.11 MAC runs in
closed firmware on the chip, so the host cannot hand it arbitrary frames.
A USB adapter whose driver registers with `mac80211` puts frame construction
back in the kernel, where injection is a supported path.

## Use

Install dependencies, once per node:

```bash
pip3 install -r requirements.txt
```

Generate this node's identity, once ever:

```bash
sudo ./bin/wifipi-setup
```

It prints the `node_id` and does nothing if one already exists — a reboot or a
re-run must never produce a second identity.

On the listening node, verify the adapter and stay in monitor mode:

```bash
sudo ./bin/wifipi-monitor -i wlan1
```

It checks the interface is a USB adapter rather than the onboard radio, that
the driver advertises monitor mode, that monitor mode engages on channel 1,
and that the driver accepts injected frames. Then it prints only our traffic —
frames whose BSSID is `42:45:41:43:48:44` ("BEACHD") and whose packet magic is
`BCHD`. Ambient wifi is discarded.

On another node, send an announce:

```bash
sudo ./bin/wifipi-announce -i wlan1 --name pi4 --once
```

The monitor should print the announce with a matching node_id, `verified`, the
name, `hops=0` and a signal reading. Drop `--once` to repeat every 60 seconds.

Add `--hexdump` to the monitor to see raw packet bytes.

## Things to watch

- **The rate field.** If received announces report 1 Mbps, the adapter
  honoured our radiotap rate and the range assumption holds. Anything else
  means it ignored us.
- **Power.** A tri-band WiFi 7 adapter draws far more than an onboard radio.
  Use a 3 A supply or a powered hub — brownout looks like random packet loss.
- **Stay on 2.4 GHz.** 6 GHz brings regulatory complexity and worse
  propagation. A mesh wants range, not throughput.
- **`mt76` monitor mode is young.** Injection on the A9000 is reported working
  on 6.18, but this is new ground.
- **Regulatory domain** caps TX power and forbids channels. Set it
  (`sudo iw reg set GB`) and leave the power alone.
