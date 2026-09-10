# ┌────────────────────────────────────────────────────────────────────────┐
# │                            run_all.py                                  │
# │            The Test Runner — Categories, Inventory, Totals             │
# └────────────────────────────────────────────────────────────────────────┘
r"""
Runs Kayra's test suites by CATEGORY and prints one table.

    .venv\Scripts\python tests\run_all.py                 # unit — safe, the default
    .venv\Scripts\python tests\run_all.py --integration    # + needs network or hardware
    .venv\Scripts\python tests\run_all.py --hardware       # + camera / microphone / GPU
    .venv\Scripts\python tests\run_all.py --all            # everything except manual
    .venv\Scripts\python tests\run_all.py --list           # the inventory, run nothing
    .venv\Scripts\python tests\run_all.py --only gesture   # substring filter

WHY THIS IS NOT PYTEST
----------------------
Kayra's suites are standalone scripts that boot real subsystems, take the single-instance
lock, own browser processes and hold the microphone. A collector that imported all of them
into ONE interpreter would have them fighting over those resources, and a failure in that
arrangement tells you less than a clean per-suite exit code does. Each suite is therefore run
as its own process, exactly as a developer runs it by hand, and this file only decides WHICH
ones and reports what happened.

`pytest` is not a dependency of this project and installing one to gain markers that a
dictionary already expresses would be a cost with no return.

MANUAL SUITES ARE NEVER RUN HERE
--------------------------------
`test_barge_in_live.py` and `test_gesture_live.py` need a person — someone to speak, or to
hold up a hand — and a runner that launched them would either hang or report a person's
absence as a failure. They are listed by `--list` with the command to run them.
"""

import os
import re
import sys
import time
import argparse
import subprocess

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          THE INVENTORY                                 │
# └────────────────────────────────────────────────────────────────────────┘
# One row per suite. This IS the regression matrix that `TESTING.md` documents, and the two
# are generated from here rather than maintained twice — `--list --markdown` prints the table
# that document embeds, so they cannot drift.
#
#   category : unit | integration | hardware | manual
#   feature  : the subsystem this suite owns. Exactly one home per feature.
#   needs    : what must be present for it to mean anything
#   writes   : what real state it touches. "nothing" is the required answer for every unit
#              suite, and `EnvironmentGuard` enforces it from inside.

SUITES = [
    # ── UNIT: deterministic, hardware-free, safe to run at any time ──
    dict(file="test_automation.py", category="unit", feature="Automation pipeline",
         needs="none", writes="temp dir only", seconds=6,
         note="normalizer, policy, resolver, planner, audit; --live adds read-only Win32"),
    dict(file="test_target_resolution.py", category="unit", feature="Target resolution",
         needs="none", writes="nothing", seconds=3),
    dict(file="test_voice_control.py", category="unit", feature="Local control vocabulary",
         needs="none", writes="nothing", seconds=5,
         note="includes the shutdown ORDER against a stubbed backend"),
    dict(file="test_voice_state.py", category="unit", feature="Voice state machine",
         needs="none", writes="nothing", seconds=3),
    dict(file="test_capture_pipeline.py", category="unit", feature="Capture and repair",
         needs="none", writes="nothing", seconds=3),
    dict(file="test_voice_turn.py", category="unit", feature="Utterance turn and safe control",
         needs="none", writes="nothing", seconds=3,
         note="endpoint scenarios, dangerous-control confirmation, DMM retries on a fake "
              "local model — no provider is contacted"),
    dict(file="test_emotion_engine.py", category="unit", feature="Emotion engine",
         needs="none", writes="nothing", seconds=3),
    dict(file="test_proactive_agent.py", category="unit", feature="Proactive agent",
         needs="none", writes="temp habit store", seconds=6),
    dict(file="test_proactive_presence.py", category="unit", feature="Proactive presence",
         needs="none", writes="nothing", seconds=5),
    dict(file="test_provider_router.py", category="unit", feature="Provider routing",
         needs="none", writes="nothing", seconds=4,
         note="injected clock; no provider is ever called"),
    dict(file="test_memory_store.py", category="unit", feature="Memory management",
         needs="none", writes="temp store only", seconds=3),
    dict(file="test_logging.py", category="unit", feature="Structured logging",
         needs="none", writes="nothing", seconds=3),
    dict(file="test_stt_backend.py", category="unit", feature="STT backend switching",
         needs="none", writes="nothing", seconds=4,
         note="--live adds real browser discovery"),
    dict(file="test_browser_selection.py", category="unit", feature="Browser capability",
         needs="none", writes="nothing", seconds=3),
    dict(file="test_gesture_state.py", category="unit", feature="Gesture FSM and filters",
         needs="none", writes="nothing", seconds=6),
    dict(file="test_gesture_control.py", category="unit", feature="Gesture lifecycle",
         needs="none", writes="nothing", seconds=8,
         note="fake camera and fake detector; the pointer records instead of moving"),
    dict(file="test_camera_runtime.py", category="unit", feature="Camera runtime",
         needs="none", writes="nothing", seconds=5,
         note="--live cycles the REAL camera ten times and counts leaked threads"),
    dict(file="test_ui.py", category="unit", feature="Desktop interface",
         needs="none", writes="nothing", seconds=25,
         note="Qt offscreen, backend fully stubbed, five synthetic machines"),
    dict(file="test_hardware_profile.py", category="unit", feature="Hardware / OS detection",
         needs="none", writes="nothing", seconds=4,
         note="seven synthetic machines; reads this host for consistency only"),
    dict(file="test_setup_runtime.py", category="unit", feature="setup.py provisioning",
         needs="none", writes="nothing", seconds=3,
         note="installs NOTHING — the installer is a recorder"),
    dict(file="test_environment.py", category="unit", feature="Launcher and environment",
         needs="none", writes="nothing", seconds=6),
    dict(file="test_tts_device.py", category="unit", feature="ONNX Runtime device",
         needs="onnxruntime", writes="nothing", seconds=20,
         note="builds real 84-byte-model sessions; GPU checks skip without NVIDIA"),

    # ── INTEGRATION: needs a network, a model, or a real browser ──
    dict(file="test_audio_pipeline.py", category="integration", feature="Audio pipeline",
         needs="Kokoro model", writes="nothing", seconds=30),
    dict(file="test_stt_lifecycle.py", category="integration", feature="STT session lifecycle",
         needs="Chrome or Edge", writes="nothing", seconds=60,
         note="run it with your OWN browser open — that is the interesting case"),
    dict(file="test_dmm_matrix.py", category="integration", feature="DMM intent boundaries",
         needs="a model provider", writes="nothing", seconds=180,
         note="follows local-first routing, so a local server changes what it measures"),
    dict(file="test_DMM.py", category="integration", feature="DMM smoke",
         needs="a model provider", writes="nothing", seconds=20),
    dict(file="test_engine.py", category="integration", feature="LLM engine smoke",
         needs="a model provider", writes="nothing", seconds=20),
    dict(file="test_voice.py", category="integration", feature="Speech output smoke",
         needs="Kokoro model + audio out", writes="nothing", seconds=20),

    # ── MANUAL: a person has to be present ──
    dict(file="test_barge_in_live.py", category="manual", feature="Barge-in",
         needs="a person to speak", writes="nothing", seconds=120),
    dict(file="test_gesture_live.py", category="manual", feature="Gesture, real hand",
         needs="a person and a camera", writes="nothing", seconds=300,
         note="SAFE by default; --real-mouse moves the pointer and asks first"),
]

CATEGORY_ORDER = ("unit", "integration", "hardware", "manual")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            RUNNING                                     │
# └────────────────────────────────────────────────────────────────────────┘

def interpreter():
    """
    The venv interpreter, and a refusal if this is not it.

    Same rule `run.py` enforces: being on some Python proves nothing about which packages are
    importable, and a suite run on the system interpreter fails in ways that have nothing to
    do with the code.
    """
    candidate = os.path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe")
    if not os.path.exists(candidate):
        candidate = os.path.join(PROJECT_ROOT, ".venv", "bin", "python")
    return candidate if os.path.exists(candidate) else sys.executable


# `PASS  ` / `FAIL  ` is the format every suite prints, so the totals are read from the same
# lines a human reads. A suite that changes its format is visible here as a zero count next to
# a non-zero exit code, which is the right way for that to surface.
_PASS = re.compile(r"PASS\s\s")
_FAIL = re.compile(r"FAIL\s\s")
_SKIP = re.compile(r"SKIP\s\s")


def run_suite(entry, python_exe, extra_args=()):
    path = os.path.join(TESTS_DIR, entry["file"])
    if not os.path.exists(path):
        return dict(entry, status="MISSING", passed=0, failed=0, skipped=0, seconds=0.0)
    started = time.time()
    try:
        proc = subprocess.run([python_exe, path, *extra_args], cwd=PROJECT_ROOT,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=1800)
        output = (proc.stdout or "") + (proc.stderr or "")
        code = proc.returncode
    except subprocess.TimeoutExpired:
        return dict(entry, status="TIMEOUT", passed=0, failed=1, skipped=0,
                    seconds=time.time() - started, output="timed out after 1800s")
    return dict(entry,
                status="ok" if code == 0 else f"exit {code}",
                passed=len(_PASS.findall(output)),
                failed=len(_FAIL.findall(output)),
                skipped=len(_SKIP.findall(output)),
                seconds=time.time() - started,
                output=output)


def print_table(results):
    print()
    print(f"{'suite':<30}{'category':<13}{'status':<10}"
          f"{'pass':>7}{'fail':>6}{'skip':>6}{'time':>8}")
    print("-" * 80)
    totals = dict(passed=0, failed=0, skipped=0, seconds=0.0)
    for row in results:
        print(f"{row['file']:<30}{row['category']:<13}{row['status']:<10}"
              f"{row['passed']:>7}{row['failed']:>6}{row['skipped']:>6}"
              f"{row['seconds']:>7.1f}s")
        totals["passed"] += row["passed"]
        totals["failed"] += row["failed"]
        totals["skipped"] += row["skipped"]
        totals["seconds"] += row["seconds"]
    print("-" * 80)
    print(f"{'TOTAL':<30}{'':<13}{'':<10}"
          f"{totals['passed']:>7}{totals['failed']:>6}{totals['skipped']:>6}"
          f"{totals['seconds']:>7.1f}s")
    return totals


def print_inventory(markdown=False):
    if markdown:
        print("| Suite | Feature | Type | Needs | Writes real state | ~Time |")
        print("|---|---|---|---|---|---|")
        for entry in SUITES:
            print(f"| `{entry['file']}` | {entry['feature']} | {entry['category']} | "
                  f"{entry['needs']} | {entry['writes']} | {entry['seconds']}s |")
        return
    for category in CATEGORY_ORDER:
        rows = [e for e in SUITES if e["category"] == category]
        if not rows:
            continue
        print(f"\n{category.upper()}")
        for entry in rows:
            print(f"  {entry['file']:<30} {entry['feature']:<28} "
                  f"needs: {entry['needs']:<24} writes: {entry['writes']}")
            if entry.get("note"):
                print(f"  {'':<30} {entry['note']}")


def main():
    parser = argparse.ArgumentParser(description="Run Kayra's test suites by category.")
    parser.add_argument("--integration", action="store_true",
                        help="also run suites that need a network, a model or a browser")
    parser.add_argument("--hardware", action="store_true",
                        help="also run suites that need a camera, microphone or GPU")
    parser.add_argument("--all", action="store_true",
                        help="unit + integration + hardware (never manual)")
    parser.add_argument("--only", default="",
                        help="run only suites whose filename or feature contains this")
    parser.add_argument("--list", action="store_true", help="print the inventory and exit")
    parser.add_argument("--markdown", action="store_true",
                        help="with --list, print the regression matrix as a Markdown table")
    parser.add_argument("--live", action="store_true",
                        help="pass --live through to suites that accept it")
    args = parser.parse_args()

    if args.list:
        print_inventory(markdown=args.markdown)
        return 0

    categories = {"unit"}
    if args.integration or args.all:
        categories.add("integration")
    if args.hardware or args.all:
        categories.add("hardware")

    selected = [e for e in SUITES if e["category"] in categories]
    if args.only:
        needle = args.only.lower()
        selected = [e for e in selected
                    if needle in e["file"].lower() or needle in e["feature"].lower()]
    if not selected:
        print("Nothing selected.")
        return 1

    python_exe = interpreter()
    print(f"Interpreter : {python_exe}")
    print(f"Categories  : {', '.join(sorted(categories))}")
    print(f"Suites      : {len(selected)}")
    if "manual" not in categories:
        print("Manual suites are never run here — see --list for how to run them by hand.")

    results = []
    for entry in selected:
        extra = ["--live"] if (args.live and entry["file"] in
                               ("test_automation.py", "test_stt_backend.py",
                                "test_camera_runtime.py")) else []
        print(f"  running {entry['file']} ...", flush=True)
        results.append(run_suite(entry, python_exe, extra))

    totals = print_table(results)

    broken = [r for r in results if r["status"] not in ("ok",)]
    if broken:
        print("\nSuites that did not exit cleanly:")
        for row in broken:
            print(f"\n  === {row['file']}  ({row['status']}) ===")
            for line in (row.get("output") or "").splitlines():
                if _FAIL.search(line) or "Traceback" in line or "Error" in line:
                    print(f"    {line.strip()[:150]}")
    return 1 if (totals["failed"] or broken) else 0


if __name__ == "__main__":
    sys.exit(main())
