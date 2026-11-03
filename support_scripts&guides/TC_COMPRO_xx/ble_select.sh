#!/bin/bash -x
#
# ble_select.sh — switch which Bluetooth controller is active: the Raspberry Pi's
# onboard chip, or a USB Bluetooth adapter (e.g. a TP-Link/Realtek dongle).
#
# Run this on whichever machine (Proxy or End Device) needs to switch. Both
# controllers can be physically present at once — this only changes which one
# is powered up, so all-devices-app / chip-lighting-app talk to the right one.
#
# usage: ./ble_select.sh [usb|onboard]

set -e

ROLE="$1"
CURRENT_STEP="startup"

fail_handler() {
    local exit_code=$?
    echo
    echo "########################################################################"
    echo "FAILED at step: ${CURRENT_STEP}"
    echo "Exit code: ${exit_code}"
    echo "########################################################################"
    echo
    case "${CURRENT_STEP}" in
        *"finding controllers"*)
            echo "Could not find both an onboard (Bus: UART) and a USB (Bus: USB) controller." ;;
        *"switching"*)
            echo "hciconfig up/down failed. Check: hciconfig -a" ;;
        *)
            echo "Re-run with the output above visible." ;;
    esac
    echo
    exit "${exit_code}"
}
trap fail_handler ERR

die() {
    echo
    echo "########################################################################"
    echo "FAILED: $1"
    echo "########################################################################"
    exit 1
}

usage() {
    cat << 'EOF'

usage: ./ble_select.sh [usb|onboard]

  usb       Power off the onboard Bluetooth chip, power on the USB adapter.
            Use this to work around onboard-chip issues (e.g. the earlier
            RF coexistence problem with a WiFi-PAF dongle) or to test with a
            specific external chipset.

  onboard   Power off the USB adapter, power on the onboard chip.
            Use this to go back to the Pi's built-in Bluetooth.

No argument was given — pick one of the two above and re-run, e.g.:
    ./ble_select.sh usb
    ./ble_select.sh onboard

EOF
}

if [ "$ROLE" != "usb" ] && [ "$ROLE" != "onboard" ]; then
    usage
    exit 1
fi

CURRENT_STEP="finding controllers"
echo "=== ${CURRENT_STEP} ==="

# Identify each controller by its bus type: the onboard Pi chip is always UART,
# a USB Bluetooth dongle is always USB. This works regardless of which hciN
# number either one currently has.
ONBOARD_HCI=""
USB_HCI=""
CURRENT_NAME=""
while read -r line; do
    if [[ "${line}" =~ ^(hci[0-9]+): ]]; then
        CURRENT_NAME="${BASH_REMATCH[1]}"
    fi
    if [[ "${line}" == *"Bus: UART"* ]]; then
        ONBOARD_HCI="${CURRENT_NAME}"
    fi
    if [[ "${line}" == *"Bus: USB"* ]]; then
        USB_HCI="${CURRENT_NAME}"
    fi
done < <(hciconfig -a)

if [ -z "${ONBOARD_HCI}" ] && [ -z "${USB_HCI}" ]; then
    die "No Bluetooth controller found at all. Check: hciconfig -a, lsusb"
fi

EFFECTIVE_ROLE="${ROLE}"
if [ "${ROLE}" == "usb" ] && [ -z "${USB_HCI}" ]; then
    echo "WARNING: no USB Bluetooth controller found (check it's plugged in: lsusb)."
    echo "Falling back to onboard, since that's what's actually available."
    EFFECTIVE_ROLE="onboard"
fi
if [ "${ROLE}" == "onboard" ] && [ -z "${ONBOARD_HCI}" ]; then
    echo "WARNING: no onboard Bluetooth controller found (unusual — check: hciconfig -a)."
    echo "Falling back to usb, since that's what's actually available."
    EFFECTIVE_ROLE="usb"
fi

echo "Onboard controller: ${ONBOARD_HCI:-not found}"
echo "USB controller:     ${USB_HCI:-not found}"

CURRENT_STEP="switching to ${EFFECTIVE_ROLE}"
echo "=== ${CURRENT_STEP} ==="

if [ "${EFFECTIVE_ROLE}" == "usb" ]; then
    [ -n "${ONBOARD_HCI}" ] && sudo hciconfig "${ONBOARD_HCI}" down
    sudo hciconfig "${USB_HCI}" up
    ACTIVE_HCI="${USB_HCI}"
else
    [ -n "${USB_HCI}" ] && sudo hciconfig "${USB_HCI}" down
    sudo hciconfig "${ONBOARD_HCI}" up
    ACTIVE_HCI="${ONBOARD_HCI}"
fi

sleep 1

CURRENT_STEP="verifying"
ACTIVE_STATE="$(hciconfig "${ACTIVE_HCI}" | grep -o "UP RUNNING" || true)"
if [ -z "${ACTIVE_STATE}" ]; then
    die "${ACTIVE_HCI} did not come up. Check: hciconfig -a, and dmesg -T | tail -30"
fi

# The --ble-controller <N> flag (all-devices-app / chip-lighting-app) refers to
# the numeric suffix of the hciN device, e.g. hci1 -> --ble-controller 1.
ACTIVE_INDEX="${ACTIVE_HCI#hci}"

echo
echo "########################################################################"
if [ "${EFFECTIVE_ROLE}" != "${ROLE}" ]; then
    echo "SUCCESS (with fallback): requested '${ROLE}', using '${EFFECTIVE_ROLE}' (${ACTIVE_HCI})"
else
    echo "SUCCESS: now using ${EFFECTIVE_ROLE} Bluetooth (${ACTIVE_HCI})"
fi
echo "########################################################################"
hciconfig -a
echo
echo "Launch your app with:"
echo "    --ble-controller ${ACTIVE_INDEX}"
echo
echo "To switch back later, run:"
if [ "${EFFECTIVE_ROLE}" == "usb" ]; then
    echo "    ./ble_select.sh onboard"
else
    echo "    ./ble_select.sh usb"
fi
echo
