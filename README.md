# beachedmesh

An association-free mesh network over 802.11. Nodes never join an access
point, never associate, and never touch the kernel networking stack — each
sits in monitor mode on a fixed channel and injects raw 802.11 frames that
every other node in range receives directly.

See [DESIGN.md](DESIGN.md) for the protocol and the reasoning behind it.

## Status

Runs as a service. A node generates an identity, announces on a timer, relays
what it hears with duplicate suppression, and learns routes from every
verified announce. Encryption, data packets and path requests are not written.

## What is here

```
bin/beachedmesh            the service: announce, relay, learn routes
bin/beachedmesh-setup      install: identity + service, safe to re-run
bin/beachedmesh-routes     inspect what routes are known
bin/beachedmesh-uninstall  remove the service, optionally the identity
bin/beachedmesh-monitor    diagnostics: watch traffic, dump frames
bin/beachedmesh-laptopreset  fix an adapter that transmits but hears nothing
bin/beachedmesh-announce   send an announce on demand

beachedmesh/node.py        the loop -- composes everything below
beachedmesh/frame.py       the packet header: build and parse
beachedmesh/identity.py    keys, node_id, announce signing and verification
beachedmesh/link.py        radiotap, 802.11 framing, raw socket
beachedmesh/flood.py       relay decisions: dedup and counter cancellation
beachedmesh/routes.py      route storage: hot table in RAM over sqlite
beachedmesh/control.py     unix socket: query the running daemon
beachedmesh/recover.py     detect a deaf radio, reload the driver
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

Set the node up. This generates the identity, installs the systemd service and
starts it:

```bash
sudo ./bin/beachedmesh-setup -i wlan1 --name pi4 --interval 60
```

Safe to re-run: an existing identity is never replaced, and the service is
restarted rather than duplicated. `--interval 60` is for testing — the default
is 6 hours. `--no-service` generates the identity and stops.

Setup renames the adapter you gave it to `BCHD0` — `BCHD1`, `BCHD2` and so on
if those are taken — and pins that name to the adapter's MAC with a udev rule
in `/etc/udev/rules.d/70-beachedmesh-net.rules`, so it survives a reboot. The
service is installed against the new name. This exists because `wlan0`/`wlan1`
are handed out in probe order: a boot that enumerates the radios the other way
round puts the *onboard* chip into monitor mode, which cannot inject, and the
node goes quiet for no visible reason. `--no-rename` keeps the kernel name.

The unit points at this checkout rather than copying it elsewhere, so
`git pull` updates the running service.

```bash
journalctl -u beachedmesh -f
```

To run it in the foreground instead:

```bash
sudo ./bin/beachedmesh -i wlan1 --name pi4
```

## Inspecting

What this node knows about reaching others:

```bash
./bin/beachedmesh-routes
./bin/beachedmesh-routes --stats
./bin/beachedmesh-routes --neighbours
./bin/beachedmesh-routes --node a1b2c3d4
```

Those read the database — everything ever learned, available whether or not
the service is running.

To see what the daemon currently *holds*, ask it:

```bash
./bin/beachedmesh-routes --live
./bin/beachedmesh-routes --flood
```

`--live` shows the hot table's actual contents and the LRU cap in effect.
`--flood` shows relay counters and pending relays, which exist for tens of
milliseconds and never touch disk. Both go over `/run/beachedmesh.sock`, which
is world-readable — inspecting a node is not privileged.

## Diagnostics

To watch traffic without running the service, or alongside it:

```bash
sudo ./bin/beachedmesh-monitor -i wlan1
```

It checks the interface is a USB adapter rather than the onboard radio, that
the driver advertises monitor mode, that monitor mode engages on channel 1,
and that the driver accepts injected frames. Then it prints only our traffic —
frames whose BSSID is `42:45:41:43:48:44` ("BEACHD") and whose packet magic is
`BCHD`. Ambient wifi is discarded.

Send an announce on demand, rather than waiting for the timer:

```bash
./bin/beachedmesh-announce
./bin/beachedmesh-announce -n 5
```

This asks the running service to announce, so it needs no root and no
interface — one process owns the radio, and an announce sent around it would
never enter its flooder or seen cache. With no service running it falls back
to its own socket, which then needs `sudo` and `-i wlan1`.

The other node should print the announce with a matching node_id, `verified`,
the name, `hops=0` and a signal reading.

Add `--hexdump` to the monitor to see raw packet bytes.

## Known fault: transmit works, receive is dead

Seen on both a Pi and a laptop, after moving the adapter between USB ports.
The node keeps transmitting -- peers hear its announces -- but it receives
nothing at all, not even ambient beacons from neighbouring access points.

**The service now detects this itself.** After the adapter passes every other
check at startup, it listens for three seconds; if not one frame arrives -- not
a beacon, not anything -- it reloads the mt76 stack and configures the adapter
again, up to twice, then runs anyway rather than exiting. So a reboot recovers
on its own, which matters because on one laptop this fault reproduced on
*every* boot: a warm reboot leaves the USB port powered, so the adapter is
never actually reset. `--no-listen-check` turns the test off.

**A reboot does not fix it by itself.** The USB port stays powered across a
warm reboot, so the adapter comes back in the same state. Reloading the driver
does, and
`beachedmesh-laptopreset` does the whole sequence -- confirm the radio is deaf,
reload, restart the service, confirm it hears again:

```bash
sudo ./bin/beachedmesh-laptopreset -i wlan1
```

It refuses to touch the driver if the radio is in fact receiving, so it is safe
to reach for first. `--check` diagnoses without changing anything. By hand:

```bash
sudo systemctl stop beachedmesh
sudo modprobe -r mt7925u mt7925_common mt792x_lib mt76_usb mt76 2>/dev/null
sleep 3
sudo modprobe mt7925u
sudo systemctl start beachedmesh
```

To confirm it is this fault rather than a configuration problem:

```bash
sudo tcpdump -i wlan1
```

On a working monitor-mode adapter this scrolls immediately with beacons from
every access point in range. Total silence means the receive path is dead
below anything in this repo -- check `iw dev wlan1 info` shows `type monitor`
*and* a `channel` line first, since a radio tuned to nothing behaves the same
way.

## Things to watch

- **The rate field.** If received announces report 11 Mbps, the adapter
  honoured our radiotap rate. Anything else means it ignored us and the link
  budget in DESIGN.md does not hold.
- **Power.** A tri-band WiFi 7 adapter draws far more than an onboard radio.
  Use a 3 A supply or a powered hub — brownout looks like random packet loss.
- **Stay on 2.4 GHz.** 6 GHz brings regulatory complexity and worse
  propagation. A mesh wants range, not throughput.
- **`mt76` monitor mode is young.** Injection on the A9000 is reported working
  on 6.18, but this is new ground.
- **Regulatory domain** caps TX power and forbids channels. Set it
  (`sudo iw reg set GB`) and leave the power alone.
