# Thread MeshCoP Commissioning — Complete Setup Guide

---

## 1. Native OTBR (Raspberry Pi)

### Setup

```bash
git clone https://github.com/openthread/ot-br-posix.git ~/ot-br-posix
cd ~/ot-br-posix
sudo ./script/bootstrap
INFRA_IF_NAME=wlan0 ./script/setup
```

The `otbr-agent` service is now enabled and will start upon reboot. To instead start
the service immediately without rebooting, use the `server` script:
```bash
./script/server
```

### Config: `/etc/default/otbr-agent`

```
OTBR_AGENT_OPTS="-I wpan0 -B wlan0 spinel+hdlc+uart:///dev/ttyACM0 trel://wlan0"
OTBR_NO_AUTO_ATTACH=0
```
```bash
sudo systemctl restart otbr-agent
```

### Web UI on port 8080

```bash
sudo systemctl edit otbr-web
```
Add:
```ini
[Service]
Environment="OTBR_WEB_OPTS=-I wpan0 -a 0.0.0.0 -p 8080"
```
```bash
sudo systemctl restart otbr-web
```
Access at: `http://<pi-ip>:8080/`

### Service status

```bash
sudo systemctl status otbr-agent
sudo systemctl status otbr-web
sudo journalctl -u otbr-web -n 50 --no-pager
```

### Core commands

```bash
sudo ot-ctl state                          # leader / router / child
sudo ot-ctl ifconfig up
sudo ot-ctl thread start
sudo ot-ctl dataset active -x              # get dataset hex
sudo ot-ctl dataset set active <hex>       # set dataset
sudo ot-ctl ba port                        # Border Agent port
sudo ot-ctl scan
sudo ot-ctl factoryreset
```

---

## 2. Build Python Controller

```bash
cd ~/connectedhomeip
python3 scripts/checkout_submodules.py --platform linux --shallow --recursive
source scripts/bootstrap.sh
source scripts/activate.sh
./scripts/build_python.sh -i out/python_env --enable_thread_meshcop true
```

---

## 3. Build chip-tool

```bash
cd ~/connectedhomeip
scripts/examples/gn_build_example.sh examples/chip-tool out/chip-tool 'chip_mdns="platform"'
```

---

## 4. ESP-IDF Setup (One-Time)

> **Note:** Thread MeshCoP is not supported in ESP-IDF v5.5.1. Use the latest
> available tag instead — currently **v5.5.5**.

```bash
cd ~
git clone -b v5.5.5 --recursive --depth 1 --shallow-submodule https://github.com/espressif/esp-idf.git
cd esp-idf
./install.sh
```

To update an existing ESP-IDF checkout to v5.5.5:
```bash
cd path/to/esp-idf
git fetch --depth 1 origin v5.5.5
git reset --hard FETCH_HEAD
git submodule update --depth 1 --recursive --init
git clean -ffdx
./install.sh
```

### Matter environment for ESP32

```bash
cd ~/connectedhomeip
source scripts/bootstrap.sh -p all,esp32
```

---

## 5. Build & Flash ESP32-H2 Firmware

Every session, activate both environments in this order:
```bash
cd path/to/esp-idf
source export.sh

cd ~/connectedhomeip
source scripts/activate.sh

export IDF_CCACHE_ENABLE=1   # optional
```

### Build

```bash
cd examples/all-clusters-app/esp32
idf.py set-target esp32h2
idf.py -D 'SDKCONFIG_DEFAULTS=sdkconfig_m5stack.defaults' build
```

> The `SDKCONFIG_DEFAULTS` file name changes depending on the target chip — e.g.
> use the defaults file matching your board (H2, C3, C6, etc.). Check the
> `examples/all-clusters-app/esp32` directory for the available `sdkconfig_*.defaults`
> files and pick the one matching your target.

Clean rebuild if switching chip targets:
```bash
rm -rf build sdkconfig managed_components dependencies.lock
idf.py set-target esp32h2
idf.py -D 'SDKCONFIG_DEFAULTS=sdkconfig_m5stack.defaults' build
```

### Flash & monitor

```bash
idf.py -p /dev/ttyACM0 erase_flash
idf.py -p /dev/ttyACM0 flash monitor
```
Exit monitor: `Ctrl+]`

---

## 6. Run Python Test

```bash
python3 TC_SC_TC_2_1.py \
  --in-test-commissioning-method thread-meshcop \
  --thread-dataset-hex <dataset-hex> \
  --thread-ba-host <pi-ip> \
  --thread-ba-port <port> \
  --discriminator 3840 \
  --passcode 20202021 \
  --no-wildcard-subscription
```

---

## 7. Commissioning Commands

```bash
# Thread MeshCoP
./out/chip-tool/chip-tool pairing thread-meshcop <node-id> \
  hex:<dataset-hex> <manual-pairing-code> \
  --thread-ba-host <pi-ip> --thread-ba-port <port>

# BLE-then-Thread (simpler, uses discriminator + passcode)
./out/chip-tool/chip-tool pairing ble-thread <node-id> \
  hex:<dataset-hex> 20202021 3840
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `otbr-web` crash-loop, "Address already in use (errno 98)" on port 80/8080 | `docker ps -a` → stop/remove whatever's holding the port: `docker stop otbr && docker rm otbr` |
| `ot-ctl ba port` returns `0` | Thread not formed yet — check `ot-ctl state`, run `ifconfig up` + `thread start` |
| Config edits not applying | `sudo systemctl restart otbr-agent` after editing `/etc/default/otbr-agent` |
| Petition/commissioning fails instantly (<50ms) | Port/network reachability issue — check `-B` backbone interface matches real LAN interface |
| Intermittent mDNS SRV parse failures during Thread MeshCoP | Use **native** OTBR, not Docker — Avahi reflects mDNS across all Docker bridges/veths and duplicates records, causing parse races |
| ESP32 build fails after switching chip targets | `rm -rf build sdkconfig managed_components dependencies.lock`, re-run `set-target` + `build` |
| ESP32 not flashing / not detected | Confirm correct `/dev/ttyACM0` or `/dev/ttyUSB0` port; hold BOOT button during flash if required by the board |
| `chip-tool` "Invalid address" error on `--thread-ba-host` | Binary may be built with `chip_inet_config_enable_ipv4=false` — use an IPv6 address instead, or rebuild with IPv4 enabled |
| Thread MeshCoP not working on ESP-IDF | Confirm you're on v5.5.5 or later, not v5.5.1 |
