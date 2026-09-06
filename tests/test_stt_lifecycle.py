# ┌────────────────────────────────────────────────────────────────────────┐
# │                        test_stt_lifecycle.py                           │
# │        Browser Session Lifecycle / Process Ownership Diagnostics       │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_stt_lifecycle.py — standalone diagnostic for the STT browser session.

    .venv\\Scripts\\python tests\\test_stt_lifecycle.py [cycles]

Asserted checks (exits non-zero on failure):

  1. One ChromeDriver + one Chrome session per process; `get_shared_engine()` never
     builds a second one.
  2. Process count and RSS stay flat across many listen/transcribe cycles — i.e. no
     browser session is created per utterance and nothing accumulates.
  3. A killed ChromeDriver is recovered in place, quickly, with the dead session's
     processes actually reaped before the replacement is built.
  4. Shutdown terminates every process Kayra owns and NO process it does not own —
     run this with your own Chrome windows open, that is the interesting case.
  5. The recognition page is a secure context with echo cancellation actually applied.

It launches real Chrome and needs a working ChromeDriver, like the rest of the voice stack.
"""

import os
import sys
import time

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(project_root)

from modules.utils import print_banner, print_info, print_success, print_error, print_system

try:
    import psutil
except ImportError:
    print_error("psutil is required for this diagnostic (pip install psutil).")
    sys.exit(1)

from modules.speech_to_text import SpeechToTextEngine, SttState, get_shared_engine

CYCLES = int(sys.argv[1]) if len(sys.argv) > 1 else 15
FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print_success(f"PASS  {label}" + (f" [dim]{detail}[/dim]" if detail else ""))
    else:
        FAILURES.append(label)
        print_error(f"FAIL  {label} {detail}")


def browser_pids():
    """Every chrome/chromedriver PID on the machine, ours and the user's alike."""
    out = set()
    for p in psutil.process_iter(["name"]):
        name = (p.info["name"] or "").lower()
        if name in ("chrome.exe", "chromedriver.exe"):
            out.add(p.pid)
    return out


def owned_rss(engine):
    total = 0
    for pid in list(engine.owned_pids):
        try:
            total += psutil.Process(pid).memory_info().rss
        except psutil.Error:
            pass
    return total


if __name__ == "__main__":
    print_banner("KAYRA STT LIFECYCLE DIAGNOSTIC", "Session reuse, recovery & process ownership")

    foreign_before = browser_pids()
    print_info(f"chrome/chromedriver processes running before Kayra: {len(foreign_before)}")

    t0 = time.perf_counter()
    stt = get_shared_engine()
    print_info(f"STT session up in {time.perf_counter() - t0:.2f}s (state={stt.state})")
    time.sleep(2.0)
    stt.refresh_owned_processes()

    # ── 1. Single session ────────────────────────────────────────────────
    print_system("\n[1] Single-session guarantee")
    again = get_shared_engine()
    check("get_shared_engine() reuses the live session", again is stt)
    check("engine reached READY", stt.state in (SttState.READY, SttState.LISTENING),
          f"(state={stt.state})")

    owned = set(stt.owned_pids)
    drivers = [
        pid for pid in owned
        if psutil.pid_exists(pid) and "chromedriver" in psutil.Process(pid).name().lower()
    ]
    check("exactly one ChromeDriver is owned", len(drivers) == 1, f"({len(drivers)})")
    print_info(f"owned processes: {len(owned)}  RSS: {owned_rss(stt)/1e6:.1f}MB")

    # ── 2. Secure context / AEC ──────────────────────────────────────────
    print_system("\n[2] Recognition page")
    check("page is a secure context", stt._script("return window.isSecureContext;") is True)
    check("processed microphone stream acquired", stt._script("return !!window.kayraMicStream;") is True)
    check("echo cancellation is actually applied",
          stt._script("return window.kayraMicStream ? "
                      "window.kayraMicStream.getAudioTracks()[0].getSettings().echoCancellation "
                      ": null;") is True)

    # ── 3. No accumulation across listening cycles ───────────────────────
    print_system(f"\n[3] {CYCLES} listen/transcribe cycles")
    procs_start, rss_start = len(browser_pids()), owned_rss(stt)
    for i in range(CYCLES):
        stt._script(
            "window.speechQueue.push({text: arguments[0], start: Date.now()-600, end: Date.now()});",
            f"listening cycle number {i}",
        )
        result = stt.capture()
        assert result and result["text"], f"cycle {i} returned nothing"
    stt.refresh_owned_processes()
    procs_end, rss_end = len(browser_pids()), owned_rss(stt)

    print_info(f"browser processes: {procs_start} -> {procs_end} | "
               f"owned RSS: {rss_start/1e6:.1f}MB -> {rss_end/1e6:.1f}MB")
    check("no browser processes accumulated across cycles", procs_end <= procs_start,
          f"({procs_start} -> {procs_end})")
    check("owned RSS did not climb materially",
          rss_end - rss_start < 60e6, f"({(rss_end-rss_start)/1e6:+.1f}MB)")

    # ── 4. Crash recovery ────────────────────────────────────────────────
    print_system("\n[4] Crash recovery")
    dead_session = set(stt.owned_pids)
    old_driver_pid = stt._service_pid
    psutil.Process(old_driver_pid).kill()
    time.sleep(1.0)

    t0 = time.perf_counter()
    probe = stt._script("return 6 * 7;")
    recovery_seconds = time.perf_counter() - t0

    check("session recovered after ChromeDriver was killed", probe == 42)
    check("recovery is fast enough to be invisible", recovery_seconds < 10,
          f"({recovery_seconds:.1f}s)")
    check("a NEW driver was created", stt._service_pid != old_driver_pid)
    stale = [pid for pid in dead_session if psutil.pid_exists(pid)]
    check("dead session's processes were reaped before the new one started",
          not stale, f"(stale={stale})")
    check("still exactly one session after recovery",
          len([pid for pid in stt.owned_pids if psutil.pid_exists(pid)
               and "chromedriver" in psutil.Process(pid).name().lower()]) == 1)

    stt._script("window.speechQueue.push({text:'command after recovery', "
                "start: Date.now()-400, end: Date.now()});")
    after = stt.capture()
    check("capture() works through the recovered session",
          bool(after and "recovery" in after["text"].lower()), f"({after and after['text']!r})")

    # ── 5. Ownership-scoped shutdown ─────────────────────────────────────
    print_system("\n[5] Shutdown")
    kayra_pids = set(stt.refresh_owned_processes())
    stt.shutdown()
    time.sleep(2.0)

    survivors = [pid for pid in kayra_pids if psutil.pid_exists(pid)]
    check("every Kayra-owned browser process is gone", not survivors, f"(survivors={survivors})")
    check("engine reached STOPPED", stt.state == SttState.STOPPED, f"(state={stt.state})")

    killed_foreign = [pid for pid in foreign_before if not psutil.pid_exists(pid)]
    check("no unrelated chrome process was killed", not killed_foreign,
          f"(killed {len(killed_foreign)} of {len(foreign_before)})")
    if not foreign_before:
        print_info("note: no pre-existing Chrome was running, so the 'leave the user's "
                   "browser alone' check was vacuous — rerun with Chrome open to make it count.")

    print_system("\n" + "=" * 60)
    if FAILURES:
        print_error(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        sys.exit(1)
    print_success("All STT lifecycle checks passed.")
