name: Matter — Build on RPi

on:
  workflow_dispatch:
    inputs:
      build_mode:
        description: >
          'full' = clone + bootstrap + clean + build,
          'skip-clone' = pull + bootstrap + clean + build,
          'skip-all' = build only (same commit)
        required: true
        default: "skip-clone"
        type: choice
        options:
          - full
          - skip-clone
          - skip-all
      target_apps:
        description: "Override apps (comma-separated e.g. all-clusters-app,lighting-app). Empty = use config."
        required: false
        default: ""
  push:
    branches: [main, master]
    paths:
      - 'Matter_CI/config/build_config.yaml'
      - 'Matter_CI/scripts/build.sh'
      - '.github/workflows/matter_build.yml'

jobs:
  # ──────────────────────────────────────────────────────────
  # Job 1: Validate config — runs on GitHub runner (fast)
  # ──────────────────────────────────────────────────────────
  validate:
    name: Validate Config
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install PyYAML
        run: pip install pyyaml --quiet

      - name: Validate build_config.yaml
        run: |
          python3 Matter_CI/scripts/validate_config.py Matter_CI/config/build_config.yaml

  # ──────────────────────────────────────────────────────────
  # Job 2: Build — runs DIRECTLY on RPi (self-hosted runner)
  # No SSH, no Tailscale, no rsync needed!
  # ──────────────────────────────────────────────────────────
  build:
    name: Build on Raspberry Pi
    runs-on: self-hosted        # ← RPi picks this job directly
    needs: validate
    # No timeout-minutes limit — self-hosted runners have no time limit

    steps:
      # GitHub checks out the repo directly onto the RPi
      - name: Checkout repo
        uses: actions/checkout@v4

      # ── Print build mode ─────────────────────────────────
      - name: Print build mode
        run: |
          MODE="${{ github.event.inputs.build_mode || 'skip-clone' }}"
          echo "Build mode : ${MODE}"
          echo "Host       : $(hostname)"
          echo "User       : $(whoami)"
          echo "Arch       : $(uname -m)"
          echo "Date       : $(date)"
          case "${MODE}" in
            full)
              echo "→ clone SDK + bootstrap + clean + build"
              ;;
            skip-clone)
              echo "→ git pull/checkout + bootstrap + clean + build"
              ;;
            skip-all)
              echo "→ build only (no clone, no bootstrap, no clean)"
              ;;
          esac

      # ── Override enabled apps if provided ────────────────
      - name: Apply target_apps override
        if: ${{ github.event.inputs.target_apps != '' }}
        env:
          TARGET_APPS: ${{ github.event.inputs.target_apps }}
        run: |
          python3 - << 'PY'
          import yaml, os
          path = os.path.join(os.environ['GITHUB_WORKSPACE'], 'Matter_CI/config/build_config.yaml')
          with open(path) as f:
              cfg = yaml.safe_load(f)
          target = [t.strip() for t in os.environ['TARGET_APPS'].split(',') if t.strip()]
          for app in cfg['apps']:
              app['enabled'] = app['name'] in target
          with open(path, 'w') as f:
              yaml.dump(cfg, f, default_flow_style=False)
          print('Enabled apps set to:', target)
          PY

      # ── Install PyYAML if missing ────────────────────────
      - name: Check PyYAML
        run: |
          python3 -c 'import yaml' 2>/dev/null \
            && echo 'PyYAML present' \
            || (sudo apt-get install -y python3-yaml && echo 'PyYAML installed')

      # ── Run build.sh ─────────────────────────────────────
      - name: Run build.sh
        run: |
          BUILD_MODE="${{ github.event.inputs.build_mode || 'skip-clone' }}"

          # build.sh lives in Matter_CI/scripts/ inside the checked-out repo
          CONFIG="${GITHUB_WORKSPACE}/Matter_CI/config/build_config.yaml"
          SCRIPT="${GITHUB_WORKSPACE}/Matter_CI/scripts/build.sh"

          chmod +x "${SCRIPT}"

          # SDK dir comes from config
          export MATTER_SDK_DIR=$(python3 -c "
          import yaml
          cfg = yaml.safe_load(open('${CONFIG}'))
          print(cfg['rpi']['sdk_dir'])
          ")

          echo "SDK_DIR    : ${MATTER_SDK_DIR}"
          echo "BUILD_MODE : ${BUILD_MODE}"

          bash "${SCRIPT}" \
            --config "${CONFIG}" \
            --mode "${BUILD_MODE}"

      # ── Collect build summary ────────────────────────────
      - name: Collect build info
        if: always()
        run: |
          mkdir -p artifacts
          python3 Matter_CI/scripts/collect_build_info.py \
            Matter_CI/config/build_config.yaml \
            > artifacts/build_summary.txt 2>&1 || true
          cat artifacts/build_summary.txt

      # ── Upload artifact ──────────────────────────────────
      - name: Upload build summary
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: build-summary-${{ github.run_number }}
          path: artifacts/
          retention-days: 14

      # ── GitHub job summary ───────────────────────────────
      - name: Write job summary
        if: always()
        run: |
          MODE="${{ github.event.inputs.build_mode || 'skip-clone' }}"
          echo "## Matter Build — Run #${{ github.run_number }}" >> $GITHUB_STEP_SUMMARY
          echo "" >> $GITHUB_STEP_SUMMARY
          echo "| Field | Value |" >> $GITHUB_STEP_SUMMARY
          echo "|---|---|" >> $GITHUB_STEP_SUMMARY
          echo "| Build Mode | ${MODE} |" >> $GITHUB_STEP_SUMMARY
          echo "| Runner | self-hosted (kishok-rpi) |" >> $GITHUB_STEP_SUMMARY
          echo "| Triggered by | ${{ github.actor }} |" >> $GITHUB_STEP_SUMMARY
          echo "| Branch | ${{ github.ref_name }} |" >> $GITHUB_STEP_SUMMARY
          echo "| Target apps | ${{ github.event.inputs.target_apps || '(from config)' }} |" >> $GITHUB_STEP_SUMMARY
          echo "" >> $GITHUB_STEP_SUMMARY
          if [[ -f artifacts/build_summary.txt ]]; then
            echo '```' >> $GITHUB_STEP_SUMMARY
            cat artifacts/build_summary.txt >> $GITHUB_STEP_SUMMARY
            echo '```' >> $GITHUB_STEP_SUMMARY
          fi
