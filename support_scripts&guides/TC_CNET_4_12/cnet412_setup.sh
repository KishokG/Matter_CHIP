#!/usr/bin/env bash
# =============================================================================
#  cnet412_setup.sh - QA helper for TC-CNET-4.12 on the Matter Test Harness
#
#  Run it on the TEST HARNESS (TH). It also configures the separate
#  "Thread Pi": the Raspberry Pi that runs the border router for Thread
#  network 1 (NOT the TH itself), over SSH.
#
#    ./cnet412_setup.sh setup   once per session / after a reboot
#    ./cnet412_setup.sh run     every test run
#    ./cnet412_setup.sh check   checks only
#    ./cnet412_setup.sh diag    collect information after a failure
#
#  Missing values are asked for when the script starts and remembered in
#  ~/.cnet412.conf (press Enter to reuse them). Environment variables with
#  the same names override the saved values:
#    THREAD_PI_IP  THREAD_PI_USER  DUT_DISCRIMINATOR  DUT_SETUP_CODE  INFRA_IFACE
# =============================================================================
set -uo pipefail

CONF="$HOME/.cnet412.conf"                 # remembered answers
PI_CACHE="$HOME/.cnet412_network1.env"     # network 1 details (contains the network key)
CERT_TOOL_DIR="${CERT_TOOL_DIR:-$HOME/certification-tool}"
CONFIG_FILE="$HOME/cnet412_config.json"
RUN_SCRIPT="$CERT_TOOL_DIR/scripts/utils/run_cnet_4_12.sh"
OTBR_START="$CERT_TOOL_DIR/backend/test_collections/matter/scripts/OTBR/otbr_start.sh"
RCP_DEV="${RCP_DEV:-/dev/ttyACM0}"
OTBR="otbr-chip"
SELF="$(readlink -f "$0")"

if [[ -t 1 ]]; then B=$'\e[1m'; R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; C=$'\e[36m'; N=$'\e[0m'
else B=""; R=""; G=""; Y=""; C=""; N=""; fi
step() { echo; echo "${B}${C}==> $*${N}"; }
ok()   { echo "  ${G}OK${N}    $*"; }
warn() { echo "  ${Y}WARN${N}  $*"; }
bad()  { echo "  ${R}FAIL${N}  $*"; }
die()  { echo; echo "${R}${B}ERROR:${N} $*" >&2; exit 1; }

# first line of an ot-ctl answer (without "Done")
ot_th() { docker exec "$OTBR" ot-ctl "$@" 2>/dev/null | tr -d '\r' | grep -v '^Done$' | head -n1; }
ot_pi() { ot-ctl "$@" 2>/dev/null | tr -d '\r' | grep -v '^Done$' | head -n1; }
wait_for() {   # wait_for <seconds> <expected> <command...>
    local n="$1" want="$2" got=""; shift 2
    for ((i = 0; i < n; i++)); do got="$("$@")"; [[ "$got" == "$want" ]] && return 0; sleep 1; done
    echo "$got"; return 1
}
default_iface() { ip -o route show default 2>/dev/null | awk '{for(i=1;i<NF;i++) if($i=="dev"){print $(i+1); exit}}'; }
extpanid_of() { python3 -c "
import sys; b=bytes.fromhex(sys.argv[1]); i=0
while i+2<=len(b):
    t,l=b[i],b[i+1]
    if t==2: print(b[i+2:i+2+l].hex()); break
    i+=2+l" "$1"; }

# ---------------------------------------------------------------- user input
# ask VAR "question" validator [default]
#   env value wins; otherwise prompt. Enter reuses the last answer, or the default.
ask() {
    local var="$1" question="$2" check="$3" saved="" value hint=""
    [[ -f "$CONF" ]] && saved="$(grep -E "^$var=" "$CONF" | tail -n1 | cut -d= -f2-)"
    if [[ -n "$saved" ]]; then hint=" [Press Enter to reuse: $saved]"
    elif [[ -n "${4:-}" ]]; then saved="$4"; hint=" [Press Enter for default: $saved]"; fi
    if [[ -n "${!var:-}" ]]; then value="${!var}"
    else
        while true; do
            read -r -p "  $question$hint: " value || die "no input for $var"
            value="${value:-$saved}"
            if "$check" "$value"; then break; fi
            echo "  ${Y}Not valid, try again.${N}"
        done
    fi
    "$check" "$value" || die "$var='$value' is not valid"
    printf -v "$var" '%s' "$value"; export "${var?}"
    touch "$CONF"; chmod 600 "$CONF"
    grep -vE "^$var=" "$CONF" > "$CONF.tmp" 2>/dev/null; echo "$var=$value" >> "$CONF.tmp"; mv "$CONF.tmp" "$CONF"
}
is_host()  { [[ "$1" =~ ^[A-Za-z0-9._:-]+$ ]]; }
is_user()  { [[ "$1" =~ ^[a-z_][a-z0-9_-]*$ ]]; }
is_disc()  { [[ "$1" =~ ^[0-9]+$ ]] && (( 10#$1 <= 4095 )); }
is_code()  { [[ "$1" =~ ^[0-9]{8}$ ]]; }
is_iface() { [[ -n "$1" ]] && ip link show "$1" >/dev/null 2>&1; }

ask_pi() {
    echo "${B}Thread Pi${N} = the Raspberry Pi running the border router for Thread network 1 (not the TH)."
    ask THREAD_PI_IP   "Thread Pi IP address" is_host
    ask THREAD_PI_USER "Thread Pi SSH user"   is_user ubuntu
    PI="$THREAD_PI_USER@$THREAD_PI_IP"
}
ask_dut() {
    ask DUT_DISCRIMINATOR "DUT discriminator (0-4095)"   is_disc
    ask DUT_SETUP_CODE    "DUT setup code (8 digits)"    is_code
}
ask_iface() {
    local ifs
    ifs="$(ip -o link show 2>/dev/null | awk -F': ' '{print $2}' | cut -d@ -f1 | grep -E '^(wl|eth|en)' | paste -sd/ -)"
    ask INFRA_IFACE "TH network interface towards the Thread Pi (${ifs:-wlan0/eth0})" is_iface "$(default_iface)"
}
ssh_pi()   { ssh -o ConnectTimeout=8 "$PI" "$@"; }
ssh_pi_t() { ssh -t -o ConnectTimeout=8 "$PI" "$@"; }

# ============================================================ Thread Pi part
# Runs ON the Thread Pi as root (copied there by 'setup').
pi_part() {
    local out="$1" iface
    [[ $EUID -eq 0 ]] || die "pi part must run with sudo"
    command -v ot-ctl >/dev/null || die "ot-ctl not found: install ot-br-posix on the Thread Pi first (guide: common setup)"
    systemctl is-active --quiet otbr-agent || { systemctl start otbr-agent; sleep 5; }
    systemctl is-active --quiet otbr-agent && ok "otbr-agent running" || die "otbr-agent not running (journalctl -u otbr-agent)"
    iface="$(grep -oE -- '-B[[:space:]]+[^[:space:]"]+' /etc/default/otbr-agent 2>/dev/null | awk '{print $2}' | head -n1)"
    iface="${iface:-$(default_iface)}"

    if [[ "$(ot_pi state)" == "leader" ]]; then
        ok "Thread network 1 already running - kept"
    else
        ot-ctl thread stop >/dev/null; ot-ctl dataset init new >/dev/null; ot-ctl dataset channel 15 >/dev/null
        ot-ctl dataset commit active >/dev/null; ot-ctl ifconfig up >/dev/null; ot-ctl thread start >/dev/null
        wait_for 60 leader ot_pi state >/dev/null && ok "Thread network 1 created (channel 15)" || die "network 1 did not become leader"
    fi
    ot-ctl srp server enable >/dev/null
    wait_for 30 running ot_pi srp server state >/dev/null && ok "SRP server running" || die "SRP server not running"

    if [[ "$(sysctl -n net.ipv6.conf.all.forwarding)" != "1" ]]; then
        sysctl -qw net.ipv6.conf.all.forwarding=1
        echo "net.ipv6.conf.all.forwarding=1" > /etc/sysctl.d/99-cnet412-forwarding.conf
    fi
    ok "IPv6 forwarding on"

    if [[ "$(ip6tables -S FORWARD 2>/dev/null | awk '/^-P FORWARD/{print $3}')" == "DROP" ]] && ip6tables -S DOCKER-USER >/dev/null 2>&1; then
        cat > /etc/systemd/system/otbr-docker-fwd.service <<EOF
[Unit]
Description=Allow Thread -> LAN IPv6 forwarding with Docker
After=docker.service otbr-agent.service

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'ip6tables -C DOCKER-USER -i wpan0 -o $iface -j ACCEPT 2>/dev/null || ip6tables -I DOCKER-USER -i wpan0 -o $iface -j ACCEPT'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload; systemctl enable --now otbr-docker-fwd.service >/dev/null 2>&1; systemctl restart otbr-docker-fwd.service
        ok "firewall: Thread replies allowed (Docker rule, permanent)"
    else
        ok "firewall: nothing to change"
    fi

    umask 077
    {
        echo "T1_DATASET=$(ot_pi dataset active -x)"
        echo "T1_OMR=$(ot-ctl br omrprefix 2>/dev/null | tr -d '\r' | grep -oE 'f[cd][0-9a-f]{2}:[0-9a-f:]*::/64' | head -n1)"
        echo "PI_LL=$(ip -6 -o addr show dev "$iface" scope link | awk '{print $4}' | cut -d/ -f1 | head -n1)"
    } > "$out"
    chown "${SUDO_USER:-root}" "$out" 2>/dev/null; chmod 600 "$out"
}

# =================================================================== TH part
fix_cert_tool() {
    if grep -q 'backend/app/otbr_manager/avahi' "$OTBR_START" 2>/dev/null; then
        sed -i 's#backend/app/otbr_manager/avahi#backend/test_collections/matter/sdk_tests/support/otbr_manager/avahi#' "$OTBR_START"
        ok "fixed avahi path in otbr_start.sh"
    fi
    if ! grep -q 'seq 1 30' "$RUN_SCRIPT" && grep -q '\[\[ "$(ot_out srp server state)" == "running" \]\] || die' "$RUN_SCRIPT"; then
        cp -n "$RUN_SCRIPT" "$RUN_SCRIPT.orig"
        python3 - "$RUN_SCRIPT" <<'PY'
import re, sys
p = sys.argv[1]; s = open(p).read()
m = re.search(r'^(\s*)\[\[ "\$\(ot_out srp server state\)" == "running" \]\] \|\| die .*$', s, re.M)
if m:
    i = m.group(1)
    s = s[:m.start()] + (f'{i}for _ in $(seq 1 30); do [[ "$(ot_out srp server state)" == "running" ]] && break; sleep 1; done\n'
                         f'{i}[[ "$(ot_out srp server state)" == "running" ]] || die "SRP server is not running on $OTBR_CONTAINER"') + s[m.end():]
    open(p, "w").write(s)
PY
        ok "fixed SRP wait in run_cnet_4_12.sh (backup: .orig)"
    fi
    ok "cert tool scripts OK"
}

cmd_setup() {
    step "Questions"
    ask_pi; ask_dut; ask_iface

    step "Checking the TH"
    [[ -f "$RUN_SCRIPT" ]] || die "$RUN_SCRIPT missing - update the certification-tool"
    command -v th-cli >/dev/null || die "th-cli not found"
    [[ -e "$RCP_DEV" ]] || die "radio $RCP_DEV not found on the TH (set RCP_DEV=...)"
    ok "run_cnet_4_12.sh, th-cli and radio found"

    step "Thread Pi ($PI): network 1, SRP, forwarding, firewall  (asks for the Pi's sudo password)"
    scp -q -o ConnectTimeout=8 "$SELF" "$PI:/tmp/cnet412_setup.sh" || die "cannot reach $PI over SSH (see guide: SSH key)"
    ssh_pi_t "sudo bash /tmp/cnet412_setup.sh _pi /tmp/cnet412_network1.env" || die "Thread Pi part failed (see above)"
    ( umask 077; ssh_pi "cat /tmp/cnet412_network1.env; rm -f /tmp/cnet412_network1.env" > "$PI_CACHE" )
    # shellcheck disable=SC1090
    source "$PI_CACHE"; [[ -n "${T1_DATASET:-}" ]] || die "could not read network 1 from the Thread Pi"
    ok "network 1 details received (extpanid $(extpanid_of "$T1_DATASET"))"

    step "TH: fixes and network settings"
    fix_cert_tool
    printf 'net.ipv6.conf.%s.accept_ra=2\nnet.ipv6.conf.%s.accept_ra_rt_info_max_plen=64\n' "$INFRA_IFACE" "$INFRA_IFACE" \
        | sudo tee /etc/sysctl.d/99-thread-ra.conf >/dev/null && sudo sysctl -q --system
    ok "TH accepts the route to network 1 on $INFRA_IFACE"

    step "TH: Thread network 2 and test config"
    for try in 1 2; do
        (cd "$CERT_TOOL_DIR" && THREAD1_DATASET_HEX="$T1_DATASET" INFRA_IFACE="$INFRA_IFACE" \
            DUT_DISCRIMINATOR="$DUT_DISCRIMINATOR" DUT_SETUP_CODE="$DUT_SETUP_CODE" \
            bash "$RUN_SCRIPT" --start-otbr --prepare-only) && break
        [[ $try -eq 2 ]] && die "run_cnet_4_12.sh failed (see above)"; warn "retrying once"; sleep 5
    done
    local keep p xp
    xp="$(ot_th extpanid)"; [[ "$xp" =~ ^[0-9a-f]{16}$ ]] || die "network 2 not running after prepare"
    keep="fd11:$(printf '%x' "$((16#${xp:0:4}))"):0:0::/64"
    while read -r p _; do
        [[ "$p" == fd11:* && "$p" != "$keep" ]] && docker exec "$OTBR" ot-ctl prefix remove "$p" >/dev/null
    done < <(docker exec "$OTBR" ot-ctl prefix 2>/dev/null | tr -d '\r')
    docker exec "$OTBR" ot-ctl netdata register >/dev/null
    ok "network 2 ready, old prefixes removed"

    step "TH: route to network 1"
    for ((i = 0; i < 60; i++)); do ip -6 route show "$T1_OMR" | grep -q . && break; sleep 1; done
    if ! ip -6 route show "$T1_OMR" | grep -q .; then
        sudo ip -6 route replace "$T1_OMR" via "$PI_LL" dev "$INFRA_IFACE"
        warn "route added by hand (repeat 'setup' after a TH reboot)"
    fi
    ok "route to $T1_OMR"

    cmd_check
    echo; echo "${G}${B}Setup done.${N} Next: ${B}$0 run${N}"
}

cmd_check() {
    step "Checks"
    # shellcheck disable=SC1090
    [[ -f "$PI_CACHE" ]] && source "$PI_CACHE"
    local fails=0 s pix tc
    s="$(ot_th state)";            [[ "$s" == leader  ]] && ok "network 2 running"      || { bad "network 2 state '$s' -> run setup"; fails=$((fails+1)); }
    s="$(ot_th srp server state)"; [[ "$s" == running ]] && ok "network 2 SRP running"  || { bad "network 2 SRP '$s' -> run setup"; fails=$((fails+1)); }
    pix="$(python3 -c "import json;print(json.load(open('$CONFIG_FILE'))['test_parameters']['string-arg'].split(':',1)[1])" 2>/dev/null)"
    if [[ -n "$pix" && "$(extpanid_of "$pix")" == "$(ot_th extpanid)" ]]; then ok "test config matches network 2"
    else bad "test config doesn't match network 2 -> run setup"; fails=$((fails+1)); fi
    if [[ -n "${T1_OMR:-}" ]]; then
        ip -6 route show "$T1_OMR" | grep -q . && ok "route to network 1" || { bad "no route to network 1 -> run setup"; fails=$((fails+1)); }
    fi
    tc="$(find "$CERT_TOOL_DIR" -name TC_CNET_4_12.py -path '*python_testing*' 2>/dev/null | head -n1)"
    if [[ -n "$tc" ]] && ! grep -q 'wait_for_srp_addresses' "$tc"; then
        warn "TC_CNET_4_12.py has no step 7 fix yet: step 7 may fail with 'No IP addresses found' (known issue)"
    fi
    th-cli test-runner-status 2>/dev/null | grep -q EXECUTING && { bad "test runner busy -> th-cli abort-testing"; fails=$((fails+1)); }
    ((fails == 0)) && ok "ready to run" || echo "  ${R}${B}$fails problem(s) - fix before running${N}"
    return "$fails"
}

cmd_run() {
    cmd_check || die "fix the problems above first (usually: $0 setup)"
    local d c
    d="$(python3 -c "import json;print(json.load(open('$CONFIG_FILE'))['dut_config']['discriminator'])" 2>/dev/null)"
    c="$(python3 -c "import json;print(json.load(open('$CONFIG_FILE'))['dut_config']['setup_code'])" 2>/dev/null)"
    echo; echo "  DUT values from the last setup: discriminator ${B}$d${N}, setup code ${B}$c${N}"
    echo "  (different DUT? stop with Ctrl-C and run: $0 setup)"
    echo
    echo "${B}${Y}Factory-reset the DUT now and put it in commissioning mode.${N}"
    read -r -p "  Press Enter when the DUT is advertising... " _
    echo "${B}Answer the prompts within 60 s:  reuse info? -> 2 (NO)   |   commissioning mode -> 1 (DONE)${N}"
    echo "(the screen may stay silent for up to 3 minutes during commissioning)"
    th-cli run-tests --tests-list TC_CNET_4_12 --config "$CONFIG_FILE"
    echo; echo "Logs: ~/output_logs/   |   on failure: $0 diag"
}

cmd_diag() {
    ask_pi
    local f; f="$HOME/cnet412_diag_$(date +%Y%m%d_%H%M%S).txt"
    {
        echo "### TH network 2";  for c in state extpanid "srp server host" prefix; do echo "-- $c"; docker exec "$OTBR" ot-ctl $c; done
        echo "### TH routes";     ip -6 route
        echo "### TH mDNS";       timeout 6 avahi-browse -rpt _matter._tcp 2>&1
        echo "### Thread Pi network 1"
        ssh_pi_t 'sudo sh -c "for c in state extpanid \"srp server host\" \"br omrprefix\"; do echo -- \$c; ot-ctl \$c; done; ip6tables -S FORWARD; ip6tables -S DOCKER-USER 2>/dev/null"'
    } 2>&1 | tee "$f"
    echo; echo "${B}Saved: $f${N}  (attach it with the TH log from ~/output_logs/)"
}

case "${1:-}" in
    setup) cmd_setup ;;
    run)   cmd_run ;;
    check) cmd_check ;;
    diag)  cmd_diag ;;
    _pi)   pi_part "${2:?}" ;;
    *)     awk 'NR>1 && /^#/{sub(/^# ?/,""); print; next} NR>1{exit}' "$SELF" ;;
esac
