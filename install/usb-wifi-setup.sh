#!/usr/bin/env bash
# Bring up a mac80211 USB WiFi adapter in monitor mode, ready for injection.
#
#   sudo ./usb-wifi-setup.sh -i wlan1            monitor mode on channel 1
#   ./usb-wifi-setup.sh --status                 what is attached (no root)
#
# Targets the Netgear A9000 (MediaTek MT7925U), which needs kernel 6.18+.

# Stop at the first error rather than carrying on with broken state.
set -euo pipefail

DRIVER=mt7925u

# Fixed for now: every node must sit on the same channel to hear each other,
# and channel hopping is a later problem.
CHANNEL=1

RED=$'\e[31m' GRN=$'\e[32m' YEL=$'\e[33m' RST=$'\e[0m'
info() { printf '%s==>%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s[!]%s %s\n' "$YEL" "$RST" "$*" >&2; }   # stderr, so it
die()  { printf '%s[x]%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }  # survives >log

# Print the header comment block as usage, so help and docs cannot drift apart.
usage() { awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; }

indent() { sed 's/^/  /'; }

IFACE=""
STATUS_ONLY=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        -i|--iface)   IFACE="$2"; shift 2 ;;
        --status)     STATUS_ONLY=1; shift ;;
        -h|--help)    usage; exit 0 ;;
        *)            die "unknown option: $1" ;;
    esac
done

# ---- inspection ------------------------------------------------------------

iface_type() { iw dev "$1" info 2>/dev/null | awk '/type/{print $2; exit}'; }

show_status() {
    echo "kernel:  $(uname -r)   (this adapter needs 6.18+)"
    printf '\nwireless interfaces:\n'; iw dev 2>/dev/null | indent
    printf '\nUSB devices:\n';         lsusb 2>/dev/null | indent
    printf '\nmt76 modules:\n';        lsmod | grep -E '^mt7' | indent
}

if (( STATUS_ONLY )); then
    show_status
    exit 0
fi

[[ -n "$IFACE" ]] || die "-i <interface> is required (try --status to see what is attached)"
[[ $EUID -eq 0 ]] || die "must run as root"

# ---- verify the interface is the adapter we mean ---------------------------

# An interface only exists once the driver matched the device id and bound, so
# its presence already proves the kernel knows this adapter. What still needs
# checking is that it is the dongle and not the Pi's onboard SDIO radio.
verify_iface() {
    local dev drv
    [[ -e "/sys/class/net/$1" ]] || die "no such interface: $1

Adapter attached?   lsusb
Driver messages?    dmesg | tail -40
What is present?    $0 --status

A missing interface usually means the adapter is unplugged, or the kernel is
older than 6.18 and does not carry this adapter's USB id."

    [[ -e "/sys/class/net/$1/phy80211" ]] || die "$1 is not a wireless interface"

    dev="$(readlink -f "/sys/class/net/$1/device")"
    [[ "$dev" == *usb* ]] \
        || die "$1 is not USB-attached -- that looks like the onboard radio, which cannot inject"

    drv="$(readlink -f "/sys/class/net/$1/device/driver" 2>/dev/null)"; drv="${drv##*/}"
    [[ "$drv" == "$DRIVER" ]] || warn "$1 is driven by ${drv:-none}, expected $DRIVER"

    info "$1: ${drv:-unknown} over USB"
}

verify_iface "$IFACE"

# ---- monitor mode ----------------------------------------------------------

# Stop the network stack yanking the interface back into managed mode.
command -v nmcli >/dev/null && nmcli device set "$IFACE" managed no 2>/dev/null
pkill -f "wpa_supplicant.*$IFACE" 2>/dev/null || true

info "setting $IFACE to monitor mode on channel $CHANNEL"
ip link set "$IFACE" down
# 'otherbss' asks the driver for frames from every BSS, not just one.
iw dev "$IFACE" set monitor otherbss 2>/dev/null \
    || iw dev "$IFACE" set type monitor \
    || die "$IFACE does not support monitor mode"
ip link set "$IFACE" up
iw dev "$IFACE" set channel "$CHANNEL" \
    || warn "could not set channel $CHANNEL (check your regulatory domain)"

[[ "$(iface_type "$IFACE")" == monitor ]] \
    || die "$IFACE is type '$(iface_type "$IFACE")', not monitor"

info "$IFACE is in monitor mode on channel $CHANNEL"
echo
iw dev "$IFACE" info
echo
iw reg get | head -8   # regulatory domain caps TX power and forbids channels
echo
info "verify injection:  sudo python3 $(dirname "$0")/verify-injection.py -i $IFACE"
