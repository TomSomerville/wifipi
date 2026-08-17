# wifipi

An association-free mesh network over 802.11. Nodes never join an access point,
never associate, and never touch the kernel networking stack — each sits in
monitor mode on a fixed channel and injects raw 802.11 frames that every other
node in range receives directly. Similar in spirit to Meshtastic, but on WiFi
hardware.

## Status

Hardware bring-up. The protocol is not written yet; first the adapters have to
prove they can do monitor mode and frame injection.

## Hardware

**Netgear A9000** USB adapters (MediaTek MT7925U, driver `mt7925u`) on
Raspberry Pi, running **Raspberry Pi OS Trixie — kernel 6.18+ is required**.

The onboard Broadcom radios on a Pi are not usable for this. Their 802.11 MAC
runs in closed firmware on the chip, so the host cannot hand it arbitrary
frames; nexmon can patch some of them, but not the Pi 400's BCM43456. A USB
adapter whose driver registers with `mac80211` puts frame construction back in
the kernel, where injection is a supported path rather than a firmware exploit.

## Layout

| Path | Purpose |
| --- | --- |
| [install/](install/) | Adapter setup and injection verification |
| `install/usb-wifi-setup.sh` | Put an adapter into monitor mode on a channel |
| `install/verify-injection.py` | Prove monitor RX works and frames inject |

See [install/README.md](install/README.md) for setup and the hardware caveats.

## Quick start

On each node, find the adapter and bring it up:

```bash
./install/usb-wifi-setup.sh --status
```

```bash
sudo ./install/usb-wifi-setup.sh -i wlan1 -c 1
```

Verify injection locally:

```bash
sudo python3 install/verify-injection.py -i wlan1 -c 1
```

To prove frames reach the air, start the listener on the second node first:

```bash
sudo python3 install/verify-injection.py -i wlan1 -c 1 --listen
```
