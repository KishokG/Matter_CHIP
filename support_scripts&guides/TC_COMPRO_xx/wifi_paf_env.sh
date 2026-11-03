#!/bin/bash -x
#
# wifi_paf_env.sh — WiFi-PAF (NAN-USD) environment manager for Matter COMPRO tests.
#
# Run the SAME commands on both the Proxy machine and the End Device machine —
# both need an identical, NAN-capable wpa_supplicant. There is no dut/ctrl split:
# whichever machine you're on, run the same subcommand.
#
# Subcommands:
#   initial_setup   Run ONCE per machine. Writes the Matter WiFi-PAF patch
#                   (embedded below), builds a patched wpa_supplicant, installs
#                   it everywhere it could get executed, and fixes the
#                   control-interface / D-Bus configuration. No external patch
#                   file needed.
#
#   renew_paf       Run before EVERY test session, on both machines. Brings the
#                   onboard WiFi interface down (WiFi-PAF only needs the USB
#                   dongle), resets state, and starts wpa_supplicant bound to
#                   the dongle, ready for all-devices-app / chip-lighting-app.
#
# usage: ./wifi_paf_env.sh [initial_setup|renew_paf]

set -e

CMD="$1"
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
        *"Writing the embedded"*)
            echo "Could not write the patch file. Check disk space and permissions in ${SCRIPT_DIR:-this directory}." ;;
        *"build dependencies"*)
            echo "apt could not install required packages. Check your network connection and that 'sudo apt-get update' works on its own." ;;
        *"Cloning hostap"*)
            echo "git clone or checkout failed. Check network access to w1.fi, and that the commit ${HOSTAP_COMMIT:-e17107912} still exists." ;;
        *"Applying Matter"*)
            echo "git apply failed — the patch did not match this hostap checkout. Do NOT re-run blindly:"
            echo "  cd ${BUILD_DIR:-~/wifi_paf_setup/hostap} && git apply --check ${PATCH_FILE:-wpa-supplicant-matter.patch}"
            echo "to see exactly which hunk failed." ;;
        *"Verifying the patch"*)
            echo "The patch applied but discovery_only never showed up in src/common/. The patch content or the pinned commit may be out of date." ;;
        *"Building wpa_supplicant"*)
            echo "The build itself failed, or the built binary is missing discovery_only. Scroll up for the actual compiler error." ;;
        *"Installing the patched binary"*)
            echo "Could not install to one of: /sbin/wpa_supplicant, \$(command -v wpa_supplicant), or the systemd unit's path."
            echo "A dangling symlink or a permissions issue is the most common cause. Check with: ls -la /sbin/wpa_supplicant" ;;
        *"Control interface"*)
            echo "D-Bus policy or config template setup failed. Check ${CONF_TEMPLATE:-wpa_supplicant-def.conf} exists and is writable." ;;
        *"No WiFi interface"*)
            echo "renew_paf could not find the dongle's interface. Check: lsusb, and lsmod | grep mt76" ;;
        *)
            echo "Re-run with the output above visible — the last few lines before this banner show the exact command that failed." ;;
    esac
    echo
    echo "Nothing past this point ran. Fix the above, then re-run:"
    echo "    ./wifi_paf_env.sh ${CMD}"
    echo
    exit "${exit_code}"
}
trap fail_handler ERR

die() {
    echo
    echo "########################################################################"
    echo "FAILED at step: ${CURRENT_STEP}"
    echo "$1"
    echo "########################################################################"
    echo
    echo "Nothing past this point ran. Fix the above, then re-run:"
    echo "    ./wifi_paf_env.sh ${CMD}"
    echo
    exit 1
}

usage() {
    cat << 'EOF'

usage: ./wifi_paf_env.sh [initial_setup|renew_paf]

  initial_setup   Run ONCE on this machine (do this on the Proxy AND the End
                  Device, separately — same command both places). Writes the
                  Matter WiFi-PAF patch, builds and installs a patched
                  wpa_supplicant, fixes ctrl_interface and D-Bus. Nothing
                  else needed beforehand — the patch is embedded in this script.

  renew_paf       Run before EVERY test run, on both machines. Brings the
                  onboard WiFi interface down and starts wpa_supplicant bound
                  to the WiFi-PAF dongle. Requires initial_setup to have been
                  run at least once.

No argument was given — pick one of the two above and re-run, e.g.:
    ./wifi_paf_env.sh initial_setup
    ./wifi_paf_env.sh renew_paf

EOF
}

if [ "$CMD" != "initial_setup" ] && [ "$CMD" != "renew_paf" ]; then
    usage
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_TEMPLATE="${SCRIPT_DIR}/wpa_supplicant-def.conf"

# ---------------------------------------------------------------------------
# initial_setup — one-time build + install, same on every machine
# ---------------------------------------------------------------------------
if [ "$CMD" == "initial_setup" ]; then

    PATCH_FILE="${SCRIPT_DIR}/wpa-supplicant-matter.patch"
    HOSTAP_COMMIT="e17107912"
    BUILD_DIR="${HOME}/wifi_paf_setup/hostap"

    if [ -f "${PATCH_FILE}" ]; then
        echo "NOTE: ${PATCH_FILE} already exists — it will be overwritten with the built-in copy."
    fi
    if [ -d "${BUILD_DIR}" ]; then
        echo "NOTE: ${BUILD_DIR} already exists — it will be deleted and re-cloned from scratch."
    fi

    CURRENT_STEP="[1/8] Writing the embedded Matter WiFi-PAF patch"
    echo "=== ${CURRENT_STEP} ==="
    cat > "${PATCH_FILE}" << 'PATCH_EOF'
diff --git a/src/common/nan_de.c b/src/common/nan_de.c
index 2af1afd73..1196ec617 100644
--- a/src/common/nan_de.c
+++ b/src/common/nan_de.c
@@ -910,7 +910,8 @@ static void nan_de_rx_publish(struct nan_de *de, struct nan_de_service *srv,
 		nan_de_tx_multicast(de, srv, instance_id);
 	}
 
-	if (!de->offload && !srv->subscribe.active && req_instance_id == 0) {
+	if (!de->offload && !srv->subscribe.active && req_instance_id == 0 &&
+	    !srv->subscribe.discovery_only) {
 		/* Passive subscriber replies with a Follow-up message without
 		 * Service Specific Info field if it received a matching
 		 * unsolicited Publish message. */
diff --git a/src/common/nan_de.h b/src/common/nan_de.h
index b2688a83c..6b9e2ad1f 100644
--- a/src/common/nan_de.h
+++ b/src/common/nan_de.h
@@ -154,6 +154,11 @@ struct nan_subscribe_params {
 
 	/* Proximity ranging flag */
 	bool proximity_ranging;
+
+	/* If true, suppress the automatic Follow-up sent to the publisher when
+	 * a passive subscriber matches an unsolicited Publish frame.  Use for
+	 * discovery-only scans where no session is to be established. */
+	bool discovery_only;
 };
 
 /* Returns -1 on failure or >0 subscribe_id */
diff --git a/wpa_supplicant/dbus/dbus_new_handlers.c b/wpa_supplicant/dbus/dbus_new_handlers.c
index 76a9297eb..8a9cec6a3 100644
--- a/wpa_supplicant/dbus/dbus_new_handlers.c
+++ b/wpa_supplicant/dbus/dbus_new_handlers.c
@@ -6855,6 +6855,10 @@ DBusMessage * wpas_dbus_handler_nan_subscribe(DBusMessage *message,
 			for (i = 0; i < entry.array_len; i++)
 				int_array_add_unique(
 					&freq_list, entry.uint16array_value[i]);
+		} else if (os_strcmp(entry.key, "discovery_only") == 0 &&
+			   entry.type == DBUS_TYPE_BOOLEAN) {
+			params.discovery_only = entry.bool_value;
+			wpa_dbus_dict_entry_clear(&entry);
 		} else {
 			wpa_printf(MSG_DEBUG,
 				   "dbus: NANSubscribe - unsupported dict entry '%s'",
diff --git a/wpa_supplicant/defconfig b/wpa_supplicant/defconfig
index 84ac8ba12..2a8f3e9f3 100644
--- a/wpa_supplicant/defconfig
+++ b/wpa_supplicant/defconfig
@@ -681,4 +681,4 @@ CONFIG_DPP2=y
 #CONFIG_NO_WMM_AC=y
 
 # Wi-Fi Aware unsynchronized service discovery (NAN USD)
-#CONFIG_NAN_USD=y
+CONFIG_NAN_USD=y
PATCH_EOF
    echo "OK: wrote ${PATCH_FILE}"

    CURRENT_STEP="[2/8] Installing build dependencies"
    echo "=== ${CURRENT_STEP} ==="
    sudo apt-get update
    sudo apt-get install -y git build-essential pkg-config \
        libnl-3-dev libnl-genl-3-dev libnl-route-3-dev libssl-dev

    CURRENT_STEP="[3/8] Cloning hostap @ ${HOSTAP_COMMIT}"
    echo "=== ${CURRENT_STEP} ==="
    rm -rf "${BUILD_DIR}"
    mkdir -p "$(dirname "${BUILD_DIR}")"
    git clone https://w1.fi/hostap.git "${BUILD_DIR}"
    cd "${BUILD_DIR}"
    git checkout "${HOSTAP_COMMIT}"

    CURRENT_STEP="[4/8] Applying Matter WiFi-PAF patch"
    echo "=== ${CURRENT_STEP} ==="
    git apply "${PATCH_FILE}"

    CURRENT_STEP="[5/8] Verifying the patch landed"
    echo "=== ${CURRENT_STEP} ==="
    if ! grep -rq "discovery_only" src/common/; then
        echo "ERROR: discovery_only not found in src/common/ after applying the patch. Stopping."
        exit 1
    fi
    echo "OK: discovery_only present in patched source."

    CURRENT_STEP="[6/8] Building wpa_supplicant"
    echo "=== ${CURRENT_STEP} ==="
    cd wpa_supplicant
    cp defconfig .config
    make -j"$(nproc)" all

    BUILT_BIN="$(pwd)/wpa_supplicant"
    if ! strings "${BUILT_BIN}" | grep -q discovery_only; then
        echo "ERROR: built binary does not contain discovery_only. Stopping."
        exit 1
    fi
    echo "OK: built binary contains discovery_only ($(strings "${BUILT_BIN}" | grep -c nan_usd) nan_usd references)."

    CURRENT_STEP="[7/8] Installing the patched binary everywhere it could get executed"
    echo "=== ${CURRENT_STEP} ==="
    # Three places can end up running "wpa_supplicant" on a machine like this:
    #   1. Wherever a bare `wpa_supplicant` command resolves via $PATH
    #   2. /sbin/wpa_supplicant   <- renew_paf launches this exact path
    #   3. Whatever systemd's wpa_supplicant.service unit points at
    # All three must be the patched build.
    sudo systemctl stop wpa_supplicant 2>/dev/null || true
    sudo pkill -9 wpa_supplicant 2>/dev/null || true

    TARGETS=("/sbin/wpa_supplicant")
    PATH_BIN="$(command -v wpa_supplicant || true)"
    if [ -n "${PATH_BIN}" ]; then
        TARGETS+=("$(readlink -f "${PATH_BIN}")")
    fi
    if [ -f /lib/systemd/system/wpa_supplicant.service ]; then
        UNIT_BIN="$(grep -oP '(?<=ExecStart=)\S+' /lib/systemd/system/wpa_supplicant.service | head -n1)"
        [ -n "${UNIT_BIN}" ] && TARGETS+=("${UNIT_BIN}")
    fi
    TARGETS=($(printf '%s\n' "${TARGETS[@]}" | sort -u))

    for TARGET_BIN in "${TARGETS[@]}"; do
        if [ -L "${TARGET_BIN}" ] && [ ! -e "${TARGET_BIN}" ]; then
            echo "Removing dangling symlink at ${TARGET_BIN}"
            sudo rm -f "${TARGET_BIN}"
        fi
        if [ -f "${TARGET_BIN}" ] && [ ! -f "${TARGET_BIN}.orig" ]; then
            sudo cp "${TARGET_BIN}" "${TARGET_BIN}.orig"
            echo "Backed up existing binary to ${TARGET_BIN}.orig"
        fi
        sudo mkdir -p "$(dirname "${TARGET_BIN}")"
        sudo cp "${BUILT_BIN}" "${TARGET_BIN}"
        sudo chmod +x "${TARGET_BIN}"
        echo "OK: installed patched wpa_supplicant at ${TARGET_BIN}"
    done

    CURRENT_STEP="[8/8] Control interface, D-Bus policy, service state"
    echo "=== ${CURRENT_STEP} ==="
    CONF_MISSING=0
    if [ -f "${CONF_TEMPLATE}" ]; then
        if ! grep -q "^ctrl_interface=" "${CONF_TEMPLATE}"; then
            sed -i '1i ctrl_interface=DIR=/run/wpa_supplicant' "${CONF_TEMPLATE}"
            echo "Added ctrl_interface line to ${CONF_TEMPLATE}"
        fi
    else
        echo "WARNING: ${CONF_TEMPLATE} not found — renew_paf will fail without it."
        echo "NOTE: this config is for the dongle's NAN-dedicated wpa_supplicant instance."
        echo "It should NOT pre-associate to a regular AP -- an AP-connected radio can"
        echo "block or interfere with WiFi-PAF/NAN discovery on most chipsets. The apps"
        echo "themselves join a network later, over D-Bus, once commissioning needs it."
        cat > "${CONF_TEMPLATE}" << EOF
ctrl_interface=DIR=/run/wpa_supplicant
update_config=1
EOF
        chmod 600 "${CONF_TEMPLATE}"
        echo "Created a minimal (no AP association) ${CONF_TEMPLATE}"
    fi

    DBUS_POLICY="/etc/dbus-1/system.d/wpa_supplicant.conf"
    if [ ! -f "${DBUS_POLICY}" ]; then
        sudo tee "${DBUS_POLICY}" > /dev/null << 'EOF'
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
    <policy user="root">
        <allow own="fi.w1.wpa_supplicant1"/>
        <allow send_destination="fi.w1.wpa_supplicant1"/>
        <allow send_interface="fi.w1.wpa_supplicant1"/>
        <allow receive_sender="fi.w1.wpa_supplicant1" receive_type="signal"/>
    </policy>
    <policy context="default">
        <deny own="fi.w1.wpa_supplicant1"/>
        <deny send_destination="fi.w1.wpa_supplicant1"/>
        <deny receive_sender="fi.w1.wpa_supplicant1" receive_type="signal"/>
    </policy>
</busconfig>
EOF
        echo "Created ${DBUS_POLICY}"
    fi
    sudo systemctl reload dbus

    # Disabled (no auto-start at boot) but NOT masked, so CHIP's own D-Bus
    # auto-activation fallback still works and will launch the patched binary.
    sudo systemctl disable wpa_supplicant 2>/dev/null || true
    sudo systemctl unmask wpa_supplicant 2>/dev/null || true

    echo
    echo "########################################################################"
    echo "SUCCESS: initial_setup complete"
    echo "########################################################################"
    echo "Patched wpa_supplicant installed at: ${TARGETS[*]}"
    echo "Next: run ./wifi_paf_env.sh renew_paf before each test session."
    exit 0
fi

# ---------------------------------------------------------------------------
# renew_paf — run before every test session, same on every machine
# ---------------------------------------------------------------------------
if [ "$CMD" == "renew_paf" ]; then

    CURRENT_STEP="checking prerequisites"
    if [ ! -f "${CONF_TEMPLATE}" ]; then
        die "${CONF_TEMPLATE} not found. Run ./wifi_paf_env.sh initial_setup first, or create this file."
    fi

    CURRENT_STEP="bringing the onboard WiFi interface down"
    echo "=== ${CURRENT_STEP} (WiFi-PAF needs the USB dongle only) ==="
    sudo ip link set wlan0 down 2>/dev/null || true
    sudo rmmod brcmfmac_wcc 2>/dev/null || true
    sudo rmmod brcmfmac 2>/dev/null || true
    sleep 1

    CURRENT_STEP="stopping NetworkManager and any running wpa_supplicant"
    echo "=== ${CURRENT_STEP} ==="
    sudo systemctl stop NetworkManager
    # Prefer a graceful shutdown over killall: if wpa_supplicant is mid an active
    # NAN session, an abrupt kill can leave the dongle's radio/MAC in a bad state
    # (the same failure mode as a forced `rmmod` mid-operation) and the interface
    # can vanish from `iw dev` on the very next run. `wpa_cli terminate` lets it
    # tear down first.
    for sock in /run/wpa_supplicant/*; do
        [ -S "${sock}" ] || continue
        iface_name="$(basename "${sock}")"
        sudo wpa_cli -i "${iface_name}" terminate 2>/dev/null || true
    done
    sleep 1
    sudo killall wpa_supplicant 2>/dev/null || true
    sleep 1

    CURRENT_STEP="clearing /tmp/chip* and syslog"
    sudo rm -f /tmp/chip*
    sudo truncate -s 0 /var/log/syslog

    CURRENT_STEP="copying wpa_supplicant config template"
    sudo cp "${CONF_TEMPLATE}" /etc/wpa_supplicant/wpa_supplicant.conf

    CURRENT_STEP="detecting the WiFi-PAF dongle interface"
    wifi_name=$(iw dev | awk '$1=="Interface"{print $2}' | grep -v p2p | head -n1)

    if [ -z "$wifi_name" ]; then
        echo "No interface found on first check — the dongle's driver may have been left in a"
        echo "bad state by an abrupt wpa_supplicant kill on a previous run. Reloading the driver..."
        DONGLE_DRIVER="mt76x2u"
        sudo rmmod "${DONGLE_DRIVER}" 2>/dev/null || true
        sleep 2
        sudo modprobe "${DONGLE_DRIVER}" 2>/dev/null || true
        sleep 3
        wifi_name=$(iw dev | awk '$1=="Interface"{print $2}' | grep -v p2p | head -n1)
    fi

    if [ -z "$wifi_name" ]; then
        die "No WiFi interface detected, even after reloading ${DONGLE_DRIVER}. Most likely cause: the dongle isn't plugged in — check 'lsusb' for it first. If it IS listed there, check 'sudo dmesg -T | tail -30' for 'firmware upload failed' (a wedged chip needing a physical unplug/replug)."
    fi
    echo "Detected WiFi interface: ${wifi_name}"

    CURRENT_STEP="bringing the interface up"
    sudo rfkill unblock wifi
    sudo ip link set ${wifi_name} up
    sleep 1

    CURRENT_STEP="starting wpa_supplicant"
    sudo rm -f wpalog.txt
    sudo /sbin/wpa_supplicant \
        -u -s \
        -i ${wifi_name} \
        -c /etc/wpa_supplicant/wpa_supplicant.conf \
        -B -ddd -f wpalog.txt

    sleep 2
    if ! pgrep -x wpa_supplicant > /dev/null; then
        die "wpa_supplicant exited right after starting. Check the last lines of wpalog.txt for the real error."
    fi

    echo
    echo "########################################################################"
    echo "SUCCESS: renew_paf complete"
    echo "########################################################################"
    echo "wpa_supplicant running on ${wifi_name}. Verify with:"
    echo "  sudo wpa_cli -i ${wifi_name} status"
    echo "  busctl status fi.w1.wpa_supplicant1"

    sudo truncate -s 0 /var/log/syslog
    exit 0
fi
