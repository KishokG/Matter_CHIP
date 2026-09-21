"""
Run this with no setup at all:

    python demo_with_sample_data.py

It builds a small, deliberately-imperfect fake sheet in memory (a few rows
modelled on the screenshots, with a handful of planted mistakes) and renders
the same HTML report `generate_report.py` would produce, so you can see the
output and sanity-check the rules before pointing the tool at your real
spreadsheet and service account.
"""

from pathlib import Path
from types import SimpleNamespace

import generate_report as gr

# ---- Tab 1 sample rows: columns A..G ---------------------------------------
# Row 1: perfectly clean.
# Row 2: empty column E, and Column G missing "-c default_config.json".
# Row 3: B/C id mismatch, and "--PICS<PICS File>" spacing issue in F, and PICS
#        missing from G.
# Row 4: F uses a hyphenated ".py" filename instead of underscores.
# Row 5: D has "endpoint" but F is missing --endpoint (argument mismatch).
# Row 6: extra test case that won't exist in the master tab at all.

TAB1_SAMPLE = [
    [
        "WebRTC Transport",
        "[TC-WEBRTC-1.5] Validate that the camera (DUT) can start a WebRTC session by issuing an ProvideOffer command - PROVISIONAL",
        "TC-WEBRTC-1.5",
        "No arguments required the test case can be selected directly for execution",
        "rm -rf /tmp/chip_* && ./chip-camera-app --camera-framerate 30",
        "python3 TC_WEBRTC_1_5.py --commissioning-method on-network --discriminator 3840 --passcode 20202021 --storage-path admin_storage.json",
        "th-cli run-tests --tests-list TC_WEBRTC_1_5 --project-id <id> -n <run name>",
    ],
    [
        "WebRTC Transport",
        "[TC-WEBRTC-1.4] Validate Non-Deferred Offer Flow for Battery-Powered Camera in Standby Mode",
        "TC-WEBRTC-1.4",
        '"int-arg": "minFrameRate:15"',
        "",
        "python3 TC_WEBRTC_1_4.py --commissioning-method on-network --discriminator 3840 --passcode 20202021 --storage-path admin_storage.json --int-arg minFrameRate:15",
        "th-cli run-tests --tests-list TC_WEBRTC_1_4 --project-id <id> -n <run name>",
    ],
    [
        "Access Control Enforcement",
        "[TC-ACE-2.4] Attribute read subscription report - [DUT as Server]",
        "TC-ACE-2.3",
        '"PICS":"required"',
        "rm -rf /tmp/chip_* && ./chip-all-clusters-app",
        "python3 TC_ACE_2_4.py --commissioning-method on-network --PICS<PICS File> --storage-path admin_storage.json",
        "th-cli run-tests --tests-list TC_ACE_2_4 --project-id <id> -n <run name> -c default_config.json",
    ],
    [
        "Access Control Enforcement",
        "[TC-ACE-1.1] Privileges[DUT-Commissionee]",
        "TC-ACE-1.1",
        "No arguments required the test case can be selected directly for execution",
        "rm -rf /tmp/chip_* && ./chip-all-clusters-app",
        "python3 TC-ACE-1.1.py --commissioning-method on-network --storage-path admin_storage.json",
        "th-cli run-tests --tests-list TC_ACE_1_1 --project-id <id> -n <run name> -c default_config.json",
    ],
    [
        "WebRTC Transport",
        "[TC-WEBRTC-1.6] Validate Two-Way-Talk Full-Duplex support in camera(DUT)",
        "TC-WEBRTC-1.6",
        '"endpoint":"1", "int-arg":"minFrameRate:15"',
        "rm -rf /tmp/chip_* && ./chip-camera-app --camera-test-audiosrc --camera-audio-playback --camera-framerate 30",
        "python3 TC_WEBRTC_1_6.py --commissioning-method on-network --discriminator 3840 --passcode 20202021 --storage-path admin_storage.json --int-arg minFrameRate:15",
        "th-cli run-tests --tests-list TC_WEBRTC_1_6 --project-id <id> -n <run name> -c default_config.json",
    ],
    [
        "Access Control Enforcement",
        "[TC-ACE-9.9] Made up test case for the demo",
        "TC-ACE-9.9",
        "No arguments required the test case can be selected directly for execution",
        "rm -rf /tmp/chip_* && ./chip-all-clusters-app",
        "python3 TC_ACE_9_9.py --commissioning-method on-network --storage-path admin_storage.json",
        "th-cli run-tests --tests-list TC_ACE_9_9 --project-id <id> -n <run name> -c default_config.json",
    ],
]

# ---- Tab 2 sample rows: columns A..F (only A, B, C, D, F are read) ---------
# Includes one row marked "UI-Python" that's deliberately missing from TAB1
# ("TC-ACE-1.2"), and a cluster-name row that will disagree with TAB1's
# "WebRTC Transport" to demonstrate the mismatch check.

TAB2_SAMPLE = [
    ["Access Control Enforcement", "Access Control Cluster", "[TC-ACE-1.1] Privileges[DUT-Commissionee]", "TC-ACE-1.1", "Core Server Test Case", "UI-Automated"],
    ["Access Control Enforcement", "Access Control Cluster", "[TC-ACE-1.2] Subscriptions[DUT-Commissionee]", "TC-ACE-1.2", "Core Server Test Case", "UI-Python"],
    ["Access Control Enforcement", "Access Control Cluster", "[TC-ACE-2.4] Attribute read subscription report - [DUT as Server]", "TC-ACE-2.4", "Core Server Test Case", "UI-Python"],
    ["WebRTC Camera", "WebRTC Transport Cluster", "[TC-WEBRTC-1.4] Validate Non-Deferred Offer Flow", "TC-WEBRTC-1.4", "App Server Test Case", "UI-Python"],
    ["WebRTC Camera", "WebRTC Transport Cluster", "[TC-WEBRTC-1.5] Validate ProvideOffer", "TC-WEBRTC-1.5", "App Server Test Case", "UI-Python"],
    ["WebRTC Camera", "WebRTC Transport Cluster", "[TC-WEBRTC-1.6] Validate Two-Way-Talk", "TC-WEBRTC-1.6", "App Server Test Case", "UI-Python"],
]


def main():
    args = SimpleNamespace(tab1="Python Script Validation Procedure (sample)",
                            tab2="All_TCs_Available_In_cert_repo_details (sample)",
                            output="demo_report.html")
    report_data = gr.build_report_data(args, TAB1_SAMPLE, TAB2_SAMPLE)
    output_path = Path(args.output).resolve()
    gr.render_html(report_data, output_path)
    print(f"Demo report written to {output_path}")


if __name__ == "__main__":
    main()
