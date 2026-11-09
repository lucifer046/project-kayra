# ┌────────────────────────────────────────────────────────────────────────┐
# │                       automation/diagnostics.py                        │
# │     "Diagnose my computer": collect -> rate -> AI summary -> report    │
# └────────────────────────────────────────────────────────────────────────┘
"""
Kayra's machine doctor.

    collect (read-only probes)  ->  Finding(area, level, text, fix?)  ->  spoken summary
                                                                      ->  Reports/diagnosis-*.md
                                                                      ->  offered fixes

EVERY PROBE IS READ-ONLY. Nothing here changes the machine. Fixes are separate, named,
CONFIRM-gated actions ("fix it" -> "Shall I apply these three fixes?") and each one is a
fixed, first-party argument vector — the user's words never reach a command line.

The model only REWORDS. The findings and their severity are decided by the thresholds below;
the AI turns them into two to four spoken sentences, and when no model answers, a
deterministic sentence built from the same findings is spoken instead.

Scopes ("diagnose <scope>"): full / slow / heat / battery / network / disk / crashes /
security. A scope runs only the probes that answer its question.
"""

import os
import re
import time
import socket
import shutil

try:
    import psutil
except Exception:                      # pragma: no cover
    psutil = None

from kayra.automation import ai
from kayra.automation.common import announce

OK, WARN, BAD = "ok", "warn", "bad"
_RANK = {BAD: 0, WARN: 1, OK: 2}


class Finding:
    __slots__ = ("area", "level", "text", "fix")

    def __init__(self, area, level, text, fix=None):
        self.area, self.level, self.text, self.fix = area, level, text, fix

    def __repr__(self):
        return f"<Finding {self.level} {self.area}: {self.text}>"


class Fix:
    """A named remedy. `apply()` returns a short spoken result. `risky` fixes delete things."""
    __slots__ = ("label", "apply", "risky")

    def __init__(self, label, apply, risky=False):
        self.label, self.apply, self.risky = label, apply, risky


def _L():
    from kayra.automation import linux
    return linux


# ┌────────────────────────────────────────────────────────────────────────┐
# │                               PROBES                                   │
# └────────────────────────────────────────────────────────────────────────┘

def probe_cpu():
    out = []
    if psutil is None:
        return out
    cores = psutil.cpu_count() or 1
    load1, load5, _ = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
    usage = psutil.cpu_percent(interval=0.3)
    level = BAD if load5 > cores * 1.5 else WARN if load5 > cores else OK
    out.append(Finding("cpu", level if usage < 90 else max(level, WARN, key=_RANK.get),
                       f"CPU at {usage:.0f}%, load {load1:.1f}/{load5:.1f} on {cores} threads"))
    top = top_processes("cpu", 3)
    if top and top[0]["cpu"] >= 40:
        hog = top[0]
        out.append(Finding("cpu", WARN, f"{hog['name']} is using {hog['cpu']:.0f}% CPU",
                           Fix(f"close {hog['name']}", lambda n=hog["name"]: _close_app(n))))
    return out


def probe_memory():
    if psutil is None:
        return []
    memory, swap = psutil.virtual_memory(), psutil.swap_memory()
    level = BAD if memory.percent >= 92 else WARN if memory.percent >= 80 else OK
    out = [Finding("memory", level, f"memory {memory.percent:.0f}% used, "
                                    f"{memory.available / 1024 ** 3:.1f} GB free")]
    if swap.total and swap.percent >= 50:
        out.append(Finding("memory", WARN, f"swap {swap.percent:.0f}% used — RAM is running out"))
    top = top_processes("memory", 1)
    if level != OK and top:
        hog = top[0]
        out.append(Finding("memory", WARN, f"{hog['name']} holds {hog['memory']:.0f}% of RAM",
                           Fix(f"close {hog['name']}", lambda n=hog["name"]: _close_app(n))))
    return out


def probe_heat():
    out = []
    readings = psutil.sensors_temperatures() if psutil and hasattr(psutil, "sensors_temperatures") else {}
    cpu = None
    for chip in ("k10temp", "coretemp", "zenpower", "cpu_thermal", "acpitz"):
        values = [e.current for e in readings.get(chip, []) if e.current]
        if values:
            cpu = max(values)
            break
    if cpu is not None:
        level = BAD if cpu >= 95 else WARN if cpu >= 85 else OK
        fix = None
        if level != OK and _power_profile() == "performance":
            fix = Fix("switch to the balanced power profile",
                      lambda: "Balanced power profile on." if _L()._ok(
                          "powerprofilesctl", "set", "balanced") else "Couldn't change it.")
        out.append(Finding("heat", level, f"CPU at {cpu:.0f}°C", fix))
    throttle = _cpu_throttle_ratio()
    if throttle is not None and throttle < 0.5 and (cpu or 0) >= 85:
        out.append(Finding("heat", WARN, f"CPU running at {throttle * 100:.0f}% of its max "
                                         f"clock — likely thermal throttling"))
    gpu = _nvidia_query("temperature.gpu,utilization.gpu")
    if gpu:
        temp, util = gpu
        level = BAD if temp >= 90 else WARN if temp >= 80 else OK
        out.append(Finding("heat", level, f"GPU at {temp}°C, {util}% busy"))
    return out


def probe_battery():
    from kayra import platforms
    status = platforms.battery_status()
    if not status:
        return []
    out = []
    health = status.get("health_percent")
    if health is not None:
        level = BAD if health < 60 else WARN if health < 80 else OK
        out.append(Finding("battery", level, f"battery health {health}% of design capacity"))
    if status.get("state") == "discharging" and status.get("watts"):
        watts = status["watts"]
        level = WARN if watts >= 25 else OK
        out.append(Finding("battery", level, f"drawing {watts:.0f} W on battery"
                           + (f", about {status['minutes_to_empty']} min left"
                              if status.get("minutes_to_empty") else "")))
        if level == WARN:
            top = top_processes("cpu", 1)
            if top:
                out.append(Finding("battery", WARN, f"biggest drain: {top[0]['name']} "
                                                    f"({top[0]['cpu']:.0f}% CPU)"))
            if _power_profile() == "performance":
                out.append(Finding("battery", WARN, "power profile is performance on battery",
                                   Fix("switch to power saver", lambda: "Power saver on." if
                                       _L()._ok("powerprofilesctl", "set", "power-saver")
                                       else "Couldn't change it.")))
    return out


def probe_disk():
    out = []
    if psutil is None:
        return out
    seen = set()
    for part in psutil.disk_partitions(all=False):
        if part.fstype in ("squashfs", "tmpfs", "devtmpfs", "overlay") or "/snap" in part.mountpoint:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        key = (usage.total, usage.used)
        if key in seen:                   # btrfs subvolumes report the same filesystem
            continue
        seen.add(key)
        level = BAD if usage.percent >= 95 else WARN if usage.percent >= 85 else OK
        out.append(Finding("disk", level, f"{part.mountpoint} {usage.percent:.0f}% full, "
                                          f"{usage.free / 1024 ** 3:.0f} GB free"))
    trash = _dir_size(os.path.expanduser("~/.local/share/Trash"))
    if trash > 500 * 1024 ** 2:
        out.append(Finding("disk", WARN, f"Trash holds {trash / 1024 ** 3:.1f} GB",
                           Fix("empty the Trash", _empty_trash, risky=True)))
    thumbs = _dir_size(os.path.expanduser("~/.cache/thumbnails"))
    if thumbs > 200 * 1024 ** 2:
        out.append(Finding("disk", WARN, f"thumbnail cache is {thumbs / 1024 ** 2:.0f} MB",
                           Fix("clear the thumbnail cache", _clear_thumbnails)))
    pkg = _dir_size("/var/cache/pacman/pkg")
    if pkg > 3 * 1024 ** 3:
        out.append(Finding("disk", WARN, f"package cache is {pkg / 1024 ** 3:.1f} GB",
                           Fix("trim the package cache (asks for your password)",
                               lambda: _in_terminal(["sudo", "paccache", "-rk2"]))))
    return out


def probe_services():
    L = _L()
    out = []
    for scope in ([], ["--user"]):
        code, text = L._run("systemctl", *scope, "--failed", "--no-legend", "--plain",
                            timeout=4.0)
        units = [line.split()[0] for line in text.splitlines() if line.strip()] if code == 0 else []
        for unit in units[:5]:
            fix = None
            if scope:                              # user services only; never system ones
                fix = Fix(f"restart {unit}", lambda u=unit: f"Restarted {u}." if L._ok(
                    "systemctl", "--user", "restart", u, timeout=10.0) else f"{u} still fails.")
            out.append(Finding("services", BAD, f"{'user ' if scope else ''}service {unit} failed",
                               fix))
    if not out:
        out.append(Finding("services", OK, "no failed services"))
    return out


def probe_crashes(days=7):
    L = _L()
    code, text = L._run("coredumpctl", "list", "--since", f"-{days}d", "--no-pager", "-q",
                        "--json=short", timeout=6.0)
    if code not in (0, 1):
        return []
    import json
    try:
        entries = json.loads(text) if text.strip() else []
    except ValueError:
        entries = []
    counts = {}
    for entry in entries if isinstance(entries, list) else []:
        exe = os.path.basename(str(entry.get("exe") or "unknown"))
        counts[exe] = counts.get(exe, 0) + 1
    if not counts:
        return [Finding("crashes", OK, f"no crashes in the last {days} days")]
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
    level = BAD if sum(counts.values()) >= 5 else WARN
    return [Finding("crashes", level, "crashes this week: " +
                    ", ".join(f"{n} ×{c}" for n, c in ranked))]


def probe_journal():
    L = _L()
    code, text = L._run("journalctl", "-p", "3", "-b", "--no-pager", "-o", "cat", "-n", "300",
                        timeout=6.0)
    if code != 0:
        return []
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    # Firmware table complaints (ACPI BIOS/AE_NOT_FOUND) and keyring/PAM chatter appear on
    # almost every laptop and mean nothing the user can act on. They are counted, not alarmed.
    benign = re.compile(r"^ACPI|AE_NOT_FOUND|_DSM|gkr-pam|Ignoring duplicate name|"
                        r"Stack trace of thread|^#\d+ |Process \d+ \(.+\) of user", re.I)
    noise = [l for l in lines if benign.search(l)]
    lines = [l for l in lines if not benign.search(l)]
    if not lines:
        return [Finding("errors", OK, "no actionable errors since boot"
                        + (f" ({len(noise)} harmless firmware messages)" if noise else ""))]
    common = {}
    for line in lines:
        key = re.sub(r"\d+", "#", line)[:90]
        common[key] = common.get(key, 0) + 1
    top = sorted(common.items(), key=lambda kv: -kv[1])[:3]
    # A handful of kernel notices since boot is normal; a stream of them is a symptom.
    level = OK if len(lines) < 20 else WARN if len(lines) < 100 else BAD
    return [Finding("errors", level, f"{len(lines)} error lines since boot; most frequent: " +
                    " | ".join(f"{k} (×{c})" for k, c in top))]


def probe_network():
    L = _L()
    out = []
    gateway = default_gateway()
    if gateway is None:
        return [Finding("network", BAD, "no default route — not connected to any network")]
    gw_ms = _ping(gateway)
    out.append(Finding("network", OK if gw_ms is not None else BAD,
                       f"router {gateway} " + (f"answers in {gw_ms:.0f} ms" if gw_ms is not None
                                               else "does not answer")))
    net_ms = _ping("1.1.1.1")
    out.append(Finding("network", OK if net_ms is not None and net_ms < 150 else
                       WARN if net_ms is not None else BAD,
                       "internet " + (f"answers in {net_ms:.0f} ms" if net_ms is not None
                                      else "is unreachable from the router")))
    started = time.perf_counter()
    try:
        socket.getaddrinfo("www.google.com", 443)
        dns_ms = (time.perf_counter() - started) * 1000
        out.append(Finding("network", OK if dns_ms < 300 else WARN,
                           f"DNS resolves in {dns_ms:.0f} ms"))
    except OSError:
        out.append(Finding("network", BAD, "DNS lookups are failing"))
    wifi = L.wifi_sentence() if L._have("nmcli") else ""
    match = re.search(r"signal (\d+)", wifi)
    if match:
        signal = int(match.group(1))
        out.append(Finding("network", OK if signal >= 50 else WARN, f"Wi-Fi signal {signal}%"))
    return out


def probe_updates():
    L = _L()
    if not L._have("checkupdates"):
        return []
    code, text = L._run("checkupdates", timeout=25.0)
    count = len([l for l in text.splitlines() if l.strip()]) if code == 0 else 0
    if code not in (0, 2):
        return []
    level = WARN if count >= 50 else OK
    return [Finding("updates", level, f"{count} package updates pending" if count
                    else "system is up to date")]


def probe_uptime():
    if psutil is None:
        return []
    days = (time.time() - psutil.boot_time()) / 86400
    return [Finding("uptime", WARN if days >= 14 else OK, f"up {days:.0f} days" if days >= 1
                    else "restarted today")]


def probe_security():
    L = _L()
    out = []
    code, text = L._run("ss", "-tlnH", timeout=3.0)
    exposed = []
    # Container and VM bridges (docker0, br-*, virbr*) are internal to this machine.
    public = set()
    if psutil is not None:
        for name, addrs in psutil.net_if_addrs().items():
            if name == "lo" or name.startswith(("docker", "br-", "virbr", "veth", "podman",
                                                "cni", "lxc", "tailscale", "wg")):
                continue
            public.update(a.address.split("%")[0] for a in addrs)
    if code == 0:
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                host, _, port = parts[3].rpartition(":")
                bare = host.strip("[]").split("%", 1)[0]
                # Exposed = listening on every address, or on an address a real network
                # interface carries. Loopback, bridges and unassigned stub addresses are not.
                if bare in ("*", "0.0.0.0", "::", "") or bare in public:
                    exposed.append(port)
    exposed = sorted(set(exposed), key=lambda p: int(p) if p.isdigit() else 0)
    out.append(Finding("security", WARN if exposed else OK,
                       f"ports open to the network: {', '.join(exposed[:8])}" if exposed
                       else "no ports open to the network"))
    firewall = any(L._ok("systemctl", "is-active", "--quiet", unit, timeout=2.0)
                   for unit in ("ufw", "firewalld", "nftables"))
    out.append(Finding("security", OK if firewall else WARN,
                       "a firewall is active" if firewall else "no firewall service is active"))
    # Omarchy's idle lock lives inside its shell (quickshell's `omarchy.idle` service).
    locker = psutil and any(p.info.get("name") in ("hypridle", "swayidle", "xss-lock",
                                                   "quickshell", "xidlehook")
                            for p in psutil.process_iter(["name"]))
    out.append(Finding("security", OK if locker else WARN,
                       "idle screen lock is running" if locker else "no idle screen lock running"))
    return out


SCOPES = {
    "full": (probe_cpu, probe_memory, probe_heat, probe_disk, probe_battery, probe_services,
             probe_crashes, probe_journal, probe_network, probe_uptime, probe_updates),
    "slow": (probe_cpu, probe_memory, probe_heat, probe_disk, probe_uptime),
    "heat": (probe_heat, probe_cpu),
    "battery": (probe_battery, probe_cpu, probe_heat),
    "network": (probe_network,),
    "disk": (probe_disk,),
    "crashes": (probe_crashes, probe_journal, probe_services),
    "security": (probe_security, probe_updates),
}

_SCOPE_WORDS = (
    ("network", r"internet|network|wifi|wi-fi|connection|online|dns|ping"),
    ("battery", r"battery|drain|charging|power"),
    ("heat", r"\bfan\b|\bhot\b|heat|temperature|overheat|thermal"),
    ("slow", r"slow|lag|sluggish|freez|hang|performance"),
    ("disk", r"disk|storage|space|drive|ssd"),
    ("crashes", r"crash|error|failed|service|log"),
    ("security", r"secur|safe|hack|virus|port|firewall"),
)


def scope_for(text):
    lowered = (text or "").lower()
    for scope, pattern in _SCOPE_WORDS:
        if re.search(pattern, lowered):
            return scope
    return "full"


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        RUN, SUMMARIZE, REPORT                          │
# └────────────────────────────────────────────────────────────────────────┘

LAST = {"findings": [], "fixes": [], "report": None, "scope": None}


def run(scope="full"):
    findings = []
    for probe in SCOPES.get(scope, SCOPES["full"]):
        try:
            findings.extend(probe())
        except Exception as e:             # one broken probe must not sink the diagnosis
            findings.append(Finding(probe.__name__.replace("probe_", ""), WARN,
                                    f"could not check ({type(e).__name__})"))
    findings.sort(key=lambda f: _RANK[f.level])
    fixes = [f.fix for f in findings if f.fix is not None]
    LAST.update(findings=findings, fixes=fixes, scope=scope)
    return findings


def fallback_sentence(findings, scope):
    bad = [f for f in findings if f.level == BAD]
    warn = [f for f in findings if f.level == WARN]
    if not bad and not warn:
        return {"full": "Everything checks out. Your computer looks healthy.",
                "network": "Your network looks healthy."}.get(scope, "Everything there looks fine.")
    issues = bad + warn
    lead = f"I found {len(bad)} problem{'s' if len(bad) != 1 else ''}" if bad else \
        f"Nothing is broken, but {len(warn)} thing{'s' if len(warn) != 1 else ''} need attention"
    detail = "; ".join(f.text for f in issues[:3])
    return f"{lead}: {detail}."


def summarize(findings, scope):
    facts = "\n".join(f"[{f.level.upper()}] {f.area}: {f.text}" for f in findings)
    fixes = [f.fix.label for f in findings if f.fix]
    system = ("You are Kayra, a calm, precise and quietly confident desktop assistant. "
              "You are given the results of a computer diagnosis. Speak 2 to 4 short sentences: "
              "the overall verdict first, then the most important problems in plain language "
              "with the likely cause. Never invent findings. No markdown, no lists, no units "
              "spelled as symbols.")
    user = f"Scope: {scope}\nFindings:\n{facts}"
    if fixes:
        user += "\nFixes I can apply if asked: " + "; ".join(fixes)
    spoken = ai.summarize(system, user, fallback=fallback_sentence(findings, scope))
    if fixes and "fix" not in spoken.lower():
        spoken += f" I can fix {len(fixes)} of these — say \"fix it\"."
    return spoken


def write_report(findings, scope, spoken):
    try:
        from kayra.core.paths import reports_dir
        folder = reports_dir()
    except Exception:
        folder = os.path.join(os.path.expanduser("~"), "Documents")
    stamp = time.strftime("%Y-%m-%d_%H%M")
    path = os.path.join(folder, f"diagnosis_{scope}_{stamp}.md")
    icon = {OK: "✅", WARN: "⚠️", BAD: "❌"}
    lines = [f"# Kayra diagnosis — {scope}", "", f"_{time.strftime('%A %d %B %Y, %H:%M')}_", "",
             "## Summary", "", spoken, "", "## Findings", "", "| | Area | Result |",
             "|---|---|---|"]
    lines += [f"| {icon[f.level]} | {f.area} | {f.text} |" for f in findings]
    fixes = [f.fix.label for f in findings if f.fix]
    if fixes:
        lines += ["", "## Fixes Kayra can apply", ""] + [f"- {label}" for label in fixes]
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError:
        return None
    LAST["report"] = path
    return path


def diagnose(text="", progress=True):
    """The whole flow. Returns (spoken, report_path)."""
    scope = scope_for(text)
    if progress:
        announce({"full": "Running a full diagnosis. Give me about fifteen seconds.",
                  "network": "Checking your network."}.get(scope, f"Checking {scope}."),
                 urgent=True)
    findings = run(scope)
    spoken = summarize(findings, scope)
    path = write_report(findings, scope, spoken)
    return spoken, path


def apply_fixes():
    """Runs every fix the last diagnosis offered. Reached only after the user said yes."""
    fixes = LAST.get("fixes") or []
    if not fixes:
        return "There's nothing from the last diagnosis for me to fix."
    results = []
    for fix in fixes:
        try:
            results.append(fix.apply() or "Done.")
        except Exception as e:
            results.append(f"{fix.label} failed ({type(e).__name__}).")
    LAST["fixes"] = []
    return " ".join(results)


def describe_fixes():
    fixes = LAST.get("fixes") or []
    return "; ".join(f.label for f in fixes)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     HEALTH WATCH (background)                          │
# └────────────────────────────────────────────────────────────────────────┘
# Cheap checks only — psutil and /proc, no process spawned except the failed-services check,
# which runs at most every ten minutes. Each alert is spoken once per ALERT_REPEAT_S.

ALERT_REPEAT_S = 2 * 3600
_WATCH = {"spoken": {}, "hot_strikes": 0, "net_strikes": 0, "services_at": 0.0,
          "known_failed": set()}


def health_alerts(now=None):
    """New alerts worth speaking right now. Called by the workflow watcher every minute."""
    now = now or time.time()
    alerts = []
    if psutil is not None:
        for part in psutil.disk_partitions(all=False):
            if part.mountpoint in ("/", os.path.expanduser("~")) or part.mountpoint == "/home":
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                except OSError:
                    continue
                if usage.percent >= 92:
                    alerts.append((f"disk:{part.mountpoint}",
                                   f"Heads up: {part.mountpoint} is {usage.percent:.0f} percent full."))
        temps = psutil.sensors_temperatures() if hasattr(psutil, "sensors_temperatures") else {}
        cpu = max([e.current for chip in ("k10temp", "coretemp") for e in temps.get(chip, [])
                   if e.current] or [0])
        _WATCH["hot_strikes"] = _WATCH["hot_strikes"] + 1 if cpu >= 97 else 0
        if _WATCH["hot_strikes"] >= 3:
            alerts.append(("heat", f"Your processor has been above {cpu:.0f} degrees for a few "
                                   "minutes. Want me to diagnose the heat?"))
    _WATCH["net_strikes"] = _WATCH["net_strikes"] + 1 if default_gateway() is None else 0
    if _WATCH["net_strikes"] == 2:
        alerts.append(("network", "You've lost your network connection."))
    if now - _WATCH["services_at"] >= 600:
        _WATCH["services_at"] = now
        L = _L()
        code, text = L._run("systemctl", "--user", "--failed", "--no-legend", "--plain",
                            timeout=4.0)
        failed = {l.split()[0] for l in text.splitlines() if l.strip()} if code == 0 else set()
        for unit in failed - _WATCH["known_failed"]:
            alerts.append((f"svc:{unit}", f"The {unit.replace('.service', '')} service just failed."))
        _WATCH["known_failed"] = failed
    fresh = []
    for key, sentence in alerts:
        if key not in _WATCH["spoken"] or now - _WATCH["spoken"][key] >= ALERT_REPEAT_S:
            _WATCH["spoken"][key] = now
            fresh.append(sentence)
    return fresh


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              HELPERS                                   │
# └────────────────────────────────────────────────────────────────────────┘

def top_processes(by="cpu", limit=5):
    """[{name, pid, cpu, memory}] sorted by `by`. Kayra's own process tree is left out."""
    if psutil is None:
        return []
    procs = list(psutil.process_iter(["pid", "name", "memory_percent"]))
    for proc in procs:
        try:
            proc.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    time.sleep(0.25)
    me = os.getpid()
    rows = []
    for proc in procs:
        try:
            if proc.pid in (0, me):
                continue
            # Percent of the WHOLE machine, so "50%" means half of every core.
            rows.append({"name": proc.info["name"] or "?", "pid": proc.pid,
                         "cpu": proc.cpu_percent(None) / max(1, psutil.cpu_count() or 1),
                         "memory": proc.info.get("memory_percent") or 0.0})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    # Group by name so twenty Chrome renderers count as one Chrome.
    grouped = {}
    for row in rows:
        entry = grouped.setdefault(row["name"], {"name": row["name"], "pid": row["pid"],
                                                 "cpu": 0.0, "memory": 0.0})
        entry["cpu"] += row["cpu"]
        entry["memory"] += row["memory"]
    key = "memory" if by in ("memory", "ram") else "cpu"
    return sorted(grouped.values(), key=lambda r: -r[key])[:limit]


def _close_app(name):
    L = _L()
    result = L.close_target(name)
    return result.message or f"Asked {name} to close."


def default_gateway():
    try:
        with open("/proc/net/route", encoding="utf-8") as handle:
            for line in handle.readlines()[1:]:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000" and int(fields[3], 16) & 2:
                    raw = bytes.fromhex(fields[2])[::-1]
                    return socket.inet_ntoa(raw)
    except (OSError, ValueError):
        return None
    return None


def _ping(host):
    code, text = _L()._run("ping", "-c", "2", "-W", "1", "-q", host, timeout=5.0)
    match = re.search(r"= [\d.]+/([\d.]+)/", text) if code == 0 else None
    return float(match.group(1)) if match else None


def _power_profile():
    code, text = _L()._run("powerprofilesctl", "get", timeout=2.0)
    return text.strip() if code == 0 else None


def _nvidia_query(fields):
    L = _L()
    if not L._have("nvidia-smi"):
        return None
    code, text = L._run("nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits",
                        timeout=3.0)
    if code != 0 or not text.strip():
        return None
    try:
        return [int(float(v)) for v in text.strip().splitlines()[0].split(",")]
    except ValueError:
        return None


def _cpu_throttle_ratio():
    try:
        with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq") as cur, \
                open("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq") as top:
            return int(cur.read()) / max(1, int(top.read()))
    except (OSError, ValueError):
        return None


def _dir_size(path, limit_files=50000):
    total, seen = 0, 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for name in files:
            seen += 1
            if seen > limit_files:
                return total
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _empty_trash():
    base = os.path.expanduser("~/.local/share/Trash")
    removed = 0
    for sub in ("files", "info"):
        folder = os.path.join(base, sub)
        if not os.path.isdir(folder):
            continue
        for entry in os.listdir(folder):
            path = os.path.join(folder, entry)
            try:
                shutil.rmtree(path) if os.path.isdir(path) and not os.path.islink(path) \
                    else os.remove(path)
                removed += 1
            except OSError:
                pass
    return f"Emptied the Trash ({removed // 2 or removed} items)."


def _clear_thumbnails():
    folder = os.path.expanduser("~/.cache/thumbnails")
    shutil.rmtree(folder, ignore_errors=True)
    return "Cleared the thumbnail cache."


def _in_terminal(argv):
    """Opens a terminal running a FIXED command so the user can type their own password."""
    L = _L()
    term = L._terminal_argv(list(argv))
    if term and L.RUNNER.spawn(term):
        return "I've opened a terminal for that; it will ask for your password."
    return "I couldn't open a terminal."
