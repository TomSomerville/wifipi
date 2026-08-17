# Adapter setup

Brings up a mac80211 USB WiFi adapter in monitor mode and proves it can inject
frames. This is the foundation the mesh protocol sits on: no association, no
AP, raw 802.11 frames built by the host.

## Why a USB adapter

The host kernel must own the 802.11 MAC layer for injection to be possible.
Drivers that register with `mac80211` — `mt76`, `ath9k_htc`, `rt2800usb` — hand
the frame-building job to Linux, so monitor mode and injection are supported
code paths. The Raspberry Pi's onboard Broadcom radios do not: their MAC runs
in closed firmware on the chip, and the Pi 400's BCM43456 has no patch at all.

**Netgear A9000 = MediaTek MT7925U**, driver `mt7925u` (part of `mt76`).

| | |
| --- | --- |
| `mt7925u` driver added | kernel **6.7** |
| A9000 USB id added | kernel **6.18** |

**Kernel 6.18+ is a hard requirement** — use the current Raspberry Pi OS
(Trixie), which ships 6.18. On 6.7–6.17 the driver exists but does not know
this adapter's USB id, so the device enumerates and binds nothing. The setup
script checks the kernel version and confirms the id is in the driver's table
(`modinfo mt7925u`) before touching anything, rather than papering over it.

## Use

Find the adapter's interface name — no root needed:

```bash
./usb-wifi-setup.sh --status
```

Then bring it up:

```bash
sudo ./usb-wifi-setup.sh -i wlan1 -c 1
```

Checks the named interface is wireless, USB-attached (not the onboard SDIO
radio, which cannot inject) and bound to the expected driver, detaches
NetworkManager and wpa_supplicant from it, sets monitor mode and channel, then
confirms the interface really is type `monitor`.

```bash
sudo python3 ./verify-injection.py -i wlan1 -c 1
```

Checks the driver advertises monitor mode, captures ambient 802.11 traffic to
prove receive works, then injects 25 tagged frames.

To prove frames actually reach the air you need both nodes. Listener first:

```bash
sudo python3 ./verify-injection.py -i wlan1 -c 1 --listen
```

then send from the other Pi with the plain command above.

## What to watch for

- **Power.** A tri-band WiFi 7 adapter draws far more than the onboard radio.
  Use a 3 A supply or a powered hub. Brownout looks like random packet loss and
  driver resets — easy to mistake for a protocol bug.
- **Stay on 2.4 GHz.** 6 GHz adds regulatory/AFC complexity and propagates
  worse. A mesh wants range, not throughput.
- **`mt76` monitor mode is actively being fixed.** Injection on the A9000 is
  reported working on 6.18, but it is new ground.
- **Check the TX rate is honoured.** The design uses 1 Mbps DSSS for roughly
  15 dB of extra receiver sensitivity over 6 Mbps OFDM. Confirm a WiFi 7 part
  respects a legacy rate set via radiotap; if it does not, the range budget
  changes.
- **Regulatory domain** caps TX power and forbids channels. Set it
  (`sudo iw reg set GB`) and leave power alone.
