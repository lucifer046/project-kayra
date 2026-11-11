# ┌────────────────────────────────────────────────────────────────────────┐
# │                     test_voice_models_backup.py                        │
# │   Parts 14–16: Kayra's voice, local models and keys, backups, hygiene  │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_voice_models_backup.py — unit tier, no network, no model, no audio.

    .venv/bin/python tests/test_voice_models_backup.py

BROWSER_CONTROL_PLAN §13 N4 (Part 14), N2 (Part 15), N5 (Part 16). LM Studio is a fake server and
a recording process runner; key checks use a fake probe (nothing is ever sent to a provider);
backups run in a temporary project; `.env` writes are captured, never made.
"""

import os
import sys
import json
import stat
import shutil
import zipfile
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Checker, EnvironmentGuard, run  # noqa: E402
from test_linux_automation import Rig, RecordingRunner, ALL_TOOLS  # noqa: E402

from kayra.utils import print_banner  # noqa: E402
from kayra.automation import common, local_models, backup  # noqa: E402
from kayra.automation.policy import Status  # noqa: E402
from kayra.output import text_to_speech as T  # noqa: E402

check = Checker("voice, models, backups")
n = common.normalize_command
VOICES = ("af_bella", "am_adam", "bf_emma", "hf_alpha", "hf_beta", "hm_omega", "jf_alpha", "jm_kumo", "zf_xiaoxiao", "zm_yunxi")


class FakeEngine:
    """The running speech engine as the voice commands see it."""

    def __init__(self, speed=1.1):
        self.speed = speed

    def set_speed(self, rate):
        self.speed = T.clamp_speed(rate, self.speed)
        return self.speed


def section_voice():
    check.section("[1] Part 14 — speaking speed and a voice for each language")
    check("speed is clamped to 0.7–1.6 and garbage keeps the default", (T.clamp_speed(3), T.clamp_speed(0.1),
          T.clamp_speed("x"), T.clamp_speed("1.25")) == (1.6, 0.7, 1.1, 1.25))
    for text, lang in (("नमस्ते, मैं कायरा हूँ।", "hi"), ("こんにちは、カイラです", "ja"), ("你好，我是凯拉", "cmn"),
                       ("Hello there", None), ("Open नमस्ते YouTube please now", None), ("123 456", None)):
        check(f"script_language({text!r}) -> {lang}", T.script_language(text) == lang, T.script_language(text))
    check("a Hindi sentence gets a Hindi voice of the SAME gender", T.voice_for_language("hi", "af_bella", VOICES) == "hf_alpha"
          and T.voice_for_language("hi", "am_adam", VOICES) == "hm_omega")
    check("...Japanese and Mandarin too", T.voice_for_language("ja", "am_adam", VOICES) == "jm_kumo"
          and T.voice_for_language("cmn", "af_bella", VOICES) == "zf_xiaoxiao")
    check("no voice of that language installed -> None (the chosen voice keeps speaking)",
          T.voice_for_language("hi", "af_bella", ("af_bella",)) is None)

    class Engine(T.DynamicVoiceEngine):
        def __init__(self):                        # no model, no audio: only the choice is tested
            self.voice, self.follow_language, self._voices_path, self.speed = "af_bella", True, "", 1.1
    saved = T.voice_names
    T.voice_names = lambda path: VOICES
    try:
        e = Engine()
        check("English stays the chosen voice", e.voice_and_language("Hello") == ("af_bella", "en-us"))
        check("Hindi switches for that sentence only", e.voice_and_language("नमस्ते दोस्तों") == ("hf_alpha", "hi")
              and e.voice == "af_bella")
        e.follow_language = False
        check("'speak other languages in their own voice' off -> never switches", e.voice_and_language("नमस्ते")[0] == "af_bella")
        e.voice = "bf_emma"
        check("a British voice reads English the British way", e.voice_and_language("Hello") == ("bf_emma", "en-gb"))
        e.__dict__["_voice_once"] = {"Hi, I'm Kayra.": "am_adam"}
        check("a preview speaks once in the previewed voice, then the chosen one again",
              e.voice_and_language("Hi, I'm Kayra.")[0] == "am_adam" and e.voice_and_language("Hi, I'm Kayra.")[0] == "bf_emma")
        check("set_speed applies and clamps", e.set_speed(1.3) == 1.3 and e.set_speed(9) == 1.6)
    finally:
        T.voice_names = saved

    for said, target in (("speak a bit faster", "faster"), ("talk slower", "slower"), ("slow down your voice", "slower"),
                         ("speak at normal speed", "normal"), ("set your speaking speed to 1.3", "1.3"),
                         ("talk at 1.5x", "1.5"), ("what's your speaking speed", "query")):
        tokens = common.direct_tokens(said, lambda: {})
        a = n(tokens[0]) if tokens else None
        check(f"rules first: '{said}' -> voice.rate {target}", a is not None and a.key == "voice.rate" and a.target == target, tokens)
    check("'slow down' alone is not about Kayra's voice (it may be a video)", common.direct_tokens("slow down", lambda: {}) is None)
    writes = []
    import kayra.core.config as config
    saved = (common.SPEECH["engine"], config.write_env_values)
    engine = FakeEngine()
    common.SPEECH["engine"] = lambda: engine
    config.write_env_values = lambda updates: (writes.append(dict(updates)), True)[1]
    try:
        with Rig() as rig:
            r = rig.do("speak a bit faster")
            check("'speak a bit faster' -> +0.1, said, and saved for the next start", r.ok and engine.speed == 1.2
                  and writes[-1] == {"ASSISTANT_SPEECH_RATE": "1.2"} and "faster" in r.message, (r.message, writes))
            r = rig.do("speak at normal speed")
            check("'normal speed' -> 1.1", engine.speed == 1.1 and r.message == "Back to my normal speed.", r.message)
            engine.speed = 1.6
            r = rig.do("speak a bit faster")
            check("at the fastest already -> said so", "already my fastest" in r.message, r.message)
            r = rig.do("what's your speaking speed")
            check("'what's your speaking speed'", "1.6" in r.message and "faster than normal" in r.message, r.message)
        common.SPEECH["engine"] = None
        with Rig() as rig:
            r = rig.do("speak a bit faster")
            check("no voice running -> said so", r.status == Status.UNSUPPORTED, r.message)
    finally:
        common.SPEECH["engine"], config.write_env_values = saved


class FakeServer:
    """LM Studio's REST API: /api/v0/models."""

    def __init__(self, models, up=True):
        self.models, self.up = models, up

    def __call__(self, url, timeout):
        if not self.up:
            raise OSError("refused")
        if url.endswith("/api/v0/models"):
            return {"data": [dict(m) for m in self.models]}
        return {"data": [{"id": m["id"]} for m in self.models if m["state"] == "loaded"]}


class LmsRunner(RecordingRunner):
    """`lms` recorded; a load marks the model loaded on the fake server."""

    def __init__(self, server, fail=False):
        super().__init__(ALL_TOOLS)
        self.server, self.fail = server, fail

    def run(self, argv, timeout=4.0, input_text=None, stdin_path=None):
        self.ran.append((list(argv), input_text))
        if argv[1:3] == ["unload", "--all"]:
            for m in self.server.models:
                m["state"] = "not-loaded"
        elif argv[1:2] == ["load"] and not self.fail:
            for m in self.server.models:
                if m["id"] == argv[2]:
                    m["state"] = "loaded"
        return (1 if self.fail and argv[1:2] == ["load"] else 0), ""


def section_models():
    check.section("[2] Part 15 — LM Studio by voice, a local speed test, key health")
    for said, key, target in (("which model is loaded", "models.status", "local"), ("what model are you using", "models.status", "local"),
                              ("load qwen", "models.load", "qwen"), ("load the qwen 3.5 model", "models.load", "qwen 3.5"),
                              ("switch the local model to gemma", "models.load", "gemma"), ("unload the local model", "models.unload", "local"),
                              ("how fast is my local model", "models.speed", "local"), ("check my api keys", "models.keys", "cloud"),
                              ("are my keys working", "models.keys", "cloud")):
        tokens = common.direct_tokens(said, lambda: {})
        a = n(tokens[0]) if tokens else None
        check(f"rules first: '{said}' -> {key} {target!r}", a is not None and a.key == key and a.target == target, tokens)
    for said in ("load more comments", "load the page"):
        check(f"not a model: '{said}'", common.direct_tokens(said, lambda: {}) is None)
    from kayra.automation import interpreter
    from kayra.automation.policy import classify_action
    check("a model can never load / unload models, check keys (which costs money), change the voice or restore a backup",
          interpreter.validate(["load qwen", "check my api keys", "speak a bit faster", "restore my last backup", "browser list tabs"],
                               n, classify_action) == ["browser list tabs"])

    server = FakeServer([{"id": "google/gemma-4-e4b", "state": "loaded", "type": "vlm"},
                         {"id": "qwen/qwen3.5-9b", "state": "not-loaded", "type": "vlm"},
                         {"id": "text-embedding-nomic-embed-text-v1.5", "state": "not-loaded", "type": "embeddings"},
                         {"id": "meta/llama-3.2-3b", "state": "not-loaded", "type": "llm"}])
    saved = (local_models.HTTP_GET, local_models._lms)
    local_models.HTTP_GET, local_models._lms = server, (lambda: "/opt/lms")
    before = len(common.ANNOUNCED)
    try:
        with Rig(runner=LmsRunner(server)) as rig:
            r = rig.do("which model is loaded")
            check("N2.3: what is loaded, and that it can see images", r.message == "gemma-4-e4b is loaded. It can see images.", r.message)
            r = rig.do("load qwen")
            calls = [a for a, _ in rig.runner.ran]
            check("N2.3: loading UNLOADS first (one 8–9B model fits), then loads by its full id",
                  calls == [["/opt/lms", "unload", "--all"], ["/opt/lms", "load", "qwen/qwen3.5-9b", "-y"]]
                  and r.message == "qwen3.5-9b is loaded (unloaded gemma-4-e4b to make room).", (calls, r.message))
            check("N2.3: ...and says it will take a while before it starts", any("Loading qwen3.5-9b" in a for a in common.ANNOUNCED[before:]))
            r = rig.do("load qwen")
            check("N2.3: loading what is loaded is a no-op", r.message == "qwen3.5-9b is already loaded." and len(rig.runner.ran) == 2)
            r = rig.do("load llama")
            r2 = rig.do("which model is loaded")
            check("N2.3: a model without vision is said to need one for screen questions", "can't see images" in r2.message, r2.message)
            r = rig.do("unload the local model")
            check("N2.3: unload", r.ok and r.message.startswith("Unloaded llama-3.2-3b."), r.message)
            r = rig.do("which model is loaded")
            check("N2.3: nothing loaded -> the downloaded ones are named", r.message.startswith("No local model is loaded. You have"), r.message)
            r = rig.do("load mistral")
            check("N2.3: an unknown model names the real ones", r.status == Status.NOT_FOUND and "gemma-4-e4b" in r.message, r.message)
        with Rig(runner=LmsRunner(server, fail=True)) as rig:
            server.models[0]["state"] = "loaded"
            r = rig.do("load qwen")
            check("N2.3: a failed load is said, with what was unloaded", not r.ok and "couldn't load" in r.message
                  and "loads on demand" in r.message, r.message)
        server.up = False
        with Rig() as rig:
            r = rig.do("which model is loaded")
            check("N2.3: LM Studio not running -> said so", r.status == Status.NOT_FOUND, r.message)
    finally:
        local_models.HTTP_GET, local_models._lms = saved

    import kayra.core.config as config
    asked = []
    saved = (local_models.PROBE, config.env)
    local_models.PROBE = lambda provider, key, model: (asked.append((provider, model)), {
        "Claude": {"ok": False, "message": "The key is valid but the Claude account has no credit."},
        "Groq": {"ok": True, "message": "Works."}}.get(provider, {"ok": False, "message": "?"}))[1]
    fake_env = {"CLAUDE_API_KEY": "sk-test", "GROQ_API_KEY": "gsk-test"}
    config.env = lambda name, default=None: fake_env.get(name, default)
    try:
        r = local_models.key_health()
        check("N2.4: one tiny probe per STORED key (Gemini has none), said plainly",
              [p for p, _m in asked] == ["Claude", "Groq"] and r.message ==
              "Claude: The key is valid but the Claude account has no credit. Groq works.", (asked, r.message))
        r = local_models.key_health(speak_only_problems=True)
        check("N2.4: at boot only a problem is spoken", r.message == "Key check: Claude: The key is valid but the Claude account has no credit.",
              r.message)
        spoken = []
        check("N2.4: the boot check is OFF unless KEY_HEALTH_ON_BOOT=on (it costs money)",
              local_models.boot_check(spoken.append) is False and not spoken)
        fake_env["KEY_HEALTH_ON_BOOT"] = "on"
        check("N2.4: ...on -> runs once and speaks the problem", local_models.boot_check(spoken.append) and len(spoken) == 1)
    finally:
        local_models.PROBE, config.env = saved


def section_backup():
    check.section("[3] Part 16 — backups without keys, restore that asks, the hygiene sweep")
    for said, key in (("back up my kayra data", "backup.create"), ("make a backup", "backup.create"),
                      ("restore my last backup", "backup.restore"), ("what backups do i have", "backup.list")):
        tokens = common.direct_tokens(said, lambda: {})
        a = n(tokens[0]) if tokens else None
        check(f"rules first: '{said}' -> {key}", a is not None and a.key == key, tokens)
    from kayra.automation.policy import classify_action
    verdict = lambda said: str(getattr(classify_action(n(said))[0], "name", classify_action(n(said))[0])).upper()
    check("restoring asks first; backing up does not", verdict("restore my last backup") == "CONFIRM"
          and verdict("back up my kayra data") == "ALLOW", (verdict("restore my last backup"), verdict("back up my kayra data")))
    root = tempfile.mkdtemp(prefix="kayra-backup-")
    saved_root = backup.ROOT
    backup.ROOT = root
    try:
        os.makedirs(os.path.join(root, "data", "Files"))
        files = {"conversation.json": '[{"role": "user", "content": "hi"}]', "profile.json": '{"name": "Ana"}',
                 "aliases.json": "{}", "Files/notes.json": "[]", "kayra.lock": "123"}
        for name, text in files.items():
            with open(os.path.join(root, "data", name), "w") as handle:
                handle.write(text)
        with open(os.path.join(root, ".env"), "w") as handle:
            handle.write("# comment\nASSISTANT_NAME=Kayra\nCLAUDE_API_KEY=sk-ant-real-secret\nGROQ_API_KEY=@keyring\n"
                         "ASSISTANT_SPEECH_RATE=1.2\nLOCAL_API_KEY=lm-studio\n")
        r = backup.create()
        path = r.detail["path"]
        archive = zipfile.ZipFile(path)
        names = set(archive.namelist())
        settings = archive.read("settings.env").decode()
        check("N5.1: every data file is in the backup, the run lock is not",
              {"data/conversation.json", "data/profile.json", "data/aliases.json", "data/Files/notes.json"} <= names
              and "data/kayra.lock" not in names, sorted(names))
        check("N5.1: NO key is in it — not a value, not a @keyring marker", "sk-ant-real-secret" not in settings
              and "CLAUDE_API_KEY" not in settings and "GROQ_API_KEY" not in settings and "LOCAL_API_KEY" not in settings
              and "ASSISTANT_SPEECH_RATE=1.2" in settings, settings)
        manifest = json.loads(archive.read("manifest.json"))
        check("N5.1: the manifest names what was left out", set(manifest["omitted_secrets"]) ==
              {"CLAUDE_API_KEY", "GROQ_API_KEY", "LOCAL_API_KEY"} and "keys stayed out" in r.message, (manifest, r.message))
        check("N5.1: the backup file is private (0600)", stat.S_IMODE(os.stat(path).st_mode) == 0o600)
        with open(os.path.join(root, "data", "profile.json"), "w") as handle:
            handle.write('{"name": "changed"}')
        with open(os.path.join(root, ".env"), "w") as handle:
            handle.write("ASSISTANT_NAME=Other\nCLAUDE_API_KEY=sk-ant-new-secret\n")
        r = backup.restore(path)
        restored_env = open(os.path.join(root, ".env")).read()
        check("N5.1: restore brings the data back", json.load(open(os.path.join(root, "data", "profile.json"))) == {"name": "Ana"})
        check("N5.1: ...merges the settings back WITHOUT touching the key that is there now",
              "ASSISTANT_NAME=Kayra" in restored_env and "CLAUDE_API_KEY=sk-ant-new-secret" in restored_env
              and "ASSISTANT_SPEECH_RATE=1.2" in restored_env, restored_env)
        check("N5.1: ...and first saved what it replaced", r.detail.get("safety") and os.path.exists(r.detail["safety"])
              and json.loads(zipfile.ZipFile(r.detail["safety"]).read("data/profile.json")) == {"name": "changed"})
        evil = os.path.join(root, "backups", "kayra-backup-evil.zip")
        with zipfile.ZipFile(evil, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"kind": "kayra-backup", "created": "x", "files": []}))
            archive.writestr("data/../../escaped.txt", "x")
            archive.writestr("/etc/evil", "x")
        backup.restore(evil)
        check("N5.1: a backup can never write outside data/ ('..', absolute paths)", not os.path.exists(os.path.join(root, "escaped.txt"))
              and not os.path.exists(os.path.join(os.path.dirname(root), "escaped.txt")))
        for i in range(14):
            backup.create(label=f"n{i}")
        check("N5.1: the last ten are kept", len([f for f in os.listdir(os.path.join(root, "backups")) if f.endswith(".zip")]) == 10)
        r = backup.list_backups()
        check("N5.1: list", r.message.startswith("10 backups; the newest is from"), r.message)
    finally:
        backup.ROOT = saved_root
        shutil.rmtree(root, ignore_errors=True)

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import run_all
    tree = tempfile.mkdtemp(prefix="kayra-tree-")
    try:
        os.makedirs(os.path.join(tree, "data"))
        os.makedirs(os.path.join(tree, "logs"))
        with open(os.path.join(tree, "data", "a.json"), "w") as handle:
            handle.write("1")
        before = run_all.snapshot_tree(tree)
        with open(os.path.join(tree, "data", "a.json"), "w") as handle:
            handle.write("12")
        with open(os.path.join(tree, "logs", "automation.log"), "w") as handle:
            handle.write("x")
        with open(os.path.join(tree, "data", "kayra.lock"), "w") as handle:
            handle.write("1")
        lines = run_all.diff_tree(before, run_all.snapshot_tree(tree))
        check("N5.3: the sweep names a file a suite added or changed in the developer's tree (the app's lock aside)",
              lines == ["added:   " + os.path.join("logs", "automation.log"), "changed: " + os.path.join("data", "a.json")], lines)
    finally:
        shutil.rmtree(tree, ignore_errors=True)


def main():
    print_banner("VOICE, LOCAL MODELS, BACKUPS (Parts 14–16)")
    with EnvironmentGuard() as guard:
        section_voice()
        section_models()
        section_backup()
    check.section("[4] The suite has no side effects")
    check("nothing the developer owns was modified", not guard.modified(), guard.report())
    return check.finish()


if __name__ == "__main__":
    sys.exit(run(main))
