"""Offline proof of hytale-update: a fake docker + downloader on temp folders. `python3 tests/test_hytale_update.py`,
exit 0 = pass. Nothing here reaches a real server, network or docker."""
import fcntl, json, os, shutil, sys, tempfile, zipfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import hytale_update as hu, hytale_update_lib as lib, hytale_update_mods as hm

r = []
ok = lambda c, m: print(("ok   " if c else "FAIL ") + m) or r.append(bool(c))


class Fake(hu.Ops):
    """A Hytale server in a container. The installed version is the text of HytaleServer.jar. A mod in `broken`
    fails on versions >= its value: 'named' -> the log names it and the server stays up (ignore-broken-mods),
    'silent' -> the server crashes and the log names nobody."""
    def __init__(s, root, latest, broken=None, bad_versions=(), deprecated=False, zipped=False):
        s.root, s.latest, s.broken, s.bad = root, latest, broken or {}, set(bad_versions)
        s.deprecated, s.zipped, s.up, s.restarts, s.logs, s.calls = deprecated, zipped, False, 0, "", []

    def p(s, *a): return os.path.join(s.root, "server", *a)

    def run(s, argv, timeout=0):
        s.calls.append(argv[0] if argv[0] != "docker" else "docker " + argv[1])
        if argv[0] == "docker":
            verb = argv[1]
            if verb == "inspect":
                return 0, ("true" if s.up else "false") if "Running" in argv[3] else str(s.restarts)
            if verb == "start": s.start()
            if verb == "stop": s.up = False
            return 0, s.logs if verb == "logs" else ""
        if argv[:2] == ["fake-dl", "-print-version"]:
            return 0, f"checking...\n{s.latest}\n"
        if argv[:2] == ["fake-dl", "-download-path"]:
            dest = argv[2]
            if s.zipped:
                with zipfile.ZipFile(os.path.join(dest, f"{s.latest}.zip"), "w") as z:
                    z.writestr("HytaleServer.jar", s.latest); z.writestr("Assets.zip", "assets " + s.latest)
            else:
                open(os.path.join(dest, "HytaleServer.jar"), "w").write(s.latest)
            return 0, ""
        return 0, ""

    def start(s):
        ver = open(s.p("HytaleServer.jar")).read()
        lines, s.up = [f"[INFO] Hytale server {ver} starting"], True
        if ver in s.bad:
            lines.append("[SEVERE] Exception in thread main java.lang.IllegalStateException: boom"); s.up = False
        for m in lib.mod_files(s.p("mods")):
            kind, since = s.broken.get(m, (None, None))
            if kind and ver >= since:
                if kind == "named": lines.append(f"[SEVERE] Failed to load pending plugin {s.ident(m)}")
                else: lines.append("[SEVERE] java.lang.NoSuchFieldError: transport"); s.up = False
        if s.deprecated: lines.append("[WARN] --transport QUIC (Netty) is deprecated and will be removed in 0.9")
        if ver != "old" and os.path.isdir(s.p("universe")):
            open(s.p("universe", "migrated"), "w").write(ver)  # a new version may migrate the world
        s.logs = "\n".join(lines)

    def ident(s, m):  # a zip mod is named in the log by its manifest's Group:Name, like a real server
        if not m.endswith(".zip"):
            return m.rsplit(".", 1)[0]
        man = json.loads(zipfile.ZipFile(s.p("mods", m)).read("manifest.json"))
        return f"{man['Group']}:{man['Name']}"

    def sleep(s, t): pass

    def now(s): return 1_800_000_000


def setup(mods=(), disabled=(), installed="old", **kw):
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "server", "mods")); os.makedirs(os.path.join(root, "server", "universe"))
    open(os.path.join(root, "server", "HytaleServer.jar"), "w").write(installed)
    open(os.path.join(root, "server", "config.json"), "w").write("{}")
    open(os.path.join(root, "server", "universe", "region.bin"), "w").write("world")
    for m in mods:
        open(os.path.join(root, "server", "mods", m), "w").write("mod")
    dis = os.path.join(root, "server", "mods-disabled")
    for m in disabled:
        os.makedirs(dis, exist_ok=True); open(os.path.join(dis, m), "w").write("mod")
        lib.save_json(os.path.join(dis, m + lib.SIDECAR), {"mod": m, "reason": "old", "disabled_since": "then"})
    cfg = hu.config(["--server-dir", os.path.join(root, "server"), "--backup-dir", os.path.join(root, "bk"),
                     "--version-cmd", "fake-dl -print-version", "--download-cmd", "fake-dl -download-path {dest}",
                     "--pause-cmd", "pause-idle", "--resume-cmd", "resume-idle", "--settle-seconds", "3",
                     "--poll-seconds", "1", "--idle-status", os.path.join(root, "idle.json"),
                     "--state-file", os.path.join(root, "state.json"), "--status-file", os.path.join(root, "st.json"),
                     "--alert-marker", os.path.join(root, "alert.json"), "--lock", os.path.join(root, "lock")])
    lib.save_json(cfg.state_file, {"installed_version": installed, "last_retry": 1_800_000_000 - 3600})
    return cfg, Fake(root, kw.pop("latest", "v2"), **kw)


st = lambda cfg: json.load(open(cfg.status_file))
on = lambda cfg: lib.mod_files(cfg.mods_dir)
dis = lambda cfg: lib.mod_files(cfg.disabled_dir)
jar = lambda cfg: open(os.path.join(cfg.server_dir, "HytaleServer.jar")).read()

print("1. new version, every mod fine: installed, mods untouched, server left asleep, hytale-idle resumed")
cfg, f = setup(mods=["a.jar", "b.jar"])
rc = hu.run(cfg, f); s = st(cfg)
ok(rc == 0 and s["result"] == "updated" and jar(cfg) == "v2", f"updated to v2 (rc {rc}, {s['result']})")
ok(on(cfg) == ["a.jar", "b.jar"] and not dis(cfg), "both mods still on")
ok(json.load(open(cfg.state_file))["installed_version"] == "v2", "state remembers v2")
ok(not f.up and f.calls[-1] == "resume-idle" and f.calls[0] == "docker inspect" and "pause-idle" in f.calls,
   "paused hytale-idle, left the server stopped, resumed hytale-idle")
ok(open(os.path.join(cfg.server_dir, "config.json")).read() == "{}", "config.json kept")
ok(not os.path.exists(cfg.alert_marker), "no alert")

print("2. one mod broken on the new version: disabled with a note, the rest stay on")
cfg, f = setup(mods=["a.jar", "bad.jar"], broken={"bad.jar": ("named", "v2")})
rc = hu.run(cfg, f); s = st(cfg)
ok(rc == 0 and s["result"] == "updated" and on(cfg) == ["a.jar"] and dis(cfg) == ["bad.jar"], "bad.jar off, a.jar on")
note = json.load(open(os.path.join(cfg.disabled_dir, "bad.jar" + lib.SIDECAR)))
ok("Failed to load pending plugin bad" in note["reason"] and note["server_version"] == "v2", f"note says why: {note}")
ok(s["newly_disabled"] == ["bad.jar"] and s["disabled_mods"][0]["mod"] == "bad.jar", "status lists it")

print("3. retry: a disabled mod that loads now goes back on; not due yet -> left alone")
cfg, f = setup(mods=["a.jar"], disabled=["fixed.jar"], installed="v2")
lib.save_json(cfg.state_file, {"installed_version": "v2", "last_retry": 1_800_000_000 - 8 * 86400})
rc = hu.run(cfg, f); s = st(cfg)
ok(rc == 0 and s["result"] == "mods-retried" and s["re_enabled"] == ["fixed.jar"], f"re-enabled ({s['result']})")
ok(on(cfg) == ["a.jar", "fixed.jar"] and not os.listdir(cfg.disabled_dir), "its note is gone")
cfg, f = setup(disabled=["later.jar"], installed="v2")
rc = hu.run(cfg, f)
ok(st(cfg)["result"] == "unchanged" and dis(cfg) == ["later.jar"] and "docker start" not in f.calls, "not due: no boot")

print("4. the new version will not start even with no mods: everything rolled back, alert")
cfg, f = setup(mods=["a.jar"], disabled=["x.jar"], bad_versions={"v2"})
rc = hu.run(cfg, f); s = st(cfg)
ok(rc == 1 and s["result"] == "rolled-back" and jar(cfg) == "old", f"old server back (rc {rc}, {s['result']})")
ok(not os.path.exists(os.path.join(cfg.world_dir, "migrated")), "world restored from the backup")
ok(on(cfg) == ["a.jar"] and dis(cfg) == ["x.jar"], "mods back exactly as they were")
ok(json.load(open(os.path.join(cfg.disabled_dir, "x.jar" + lib.SIDECAR)))["reason"] == "old", "x.jar keeps its note")
ok(s["old_version_boots"] is True and os.path.exists(cfg.alert_marker), "old version proven to boot; alert written")
ok(any("IllegalStateException" in l for l in s["key_log_lines"]), "alert quotes the log")
ok(json.load(open(cfg.state_file))["installed_version"] == "old", "state still says old")

print("5. someone may be playing: skipped before anything runs")
cfg, f = setup(mods=["a.jar"]); lib.save_json(cfg.idle_status, {"state": "running"})
ok(hu.run(cfg, f) == 0 and st(cfg)["result"] == "skipped" and not f.calls, "hytale-idle says running -> skipped")
cfg, f = setup(); f.up = True
ok(hu.run(cfg, f) == 0 and st(cfg)["result"] == "skipped" and "fake-dl" not in f.calls, "container up -> skipped")
cfg, f = setup(); held = open(cfg.lock, "w"); fcntl.flock(held, fcntl.LOCK_EX)
ok(hu.run(cfg, f) == 0 and "lock" in st(cfg)["why"] and not f.calls, "another run holds the lock -> skipped")
held.close()

print("6. no new version: nothing installed, booted or backed up")
cfg, f = setup(mods=["a.jar"], installed="v2")
ok(hu.run(cfg, f) == 0 and st(cfg)["result"] == "unchanged" and "docker start" not in f.calls
   and not os.path.exists(cfg.backup_dir), "unchanged")

print("7. deprecation warnings are kept as early warnings")
cfg, f = setup(mods=["a.jar"], deprecated=True); hu.run(cfg, f)
ok(any("--transport QUIC" in w for w in st(cfg)["early_warnings"]), "Netty/QUIC warning recorded")

print("8. a crash that names no mod: found by adding mods back one at a time")
cfg, f = setup(mods=["a.jar", "crash.jar", "c.jar"], broken={"crash.jar": ("silent", "v2")})
rc = hu.run(cfg, f); s = st(cfg)
ok(rc == 0 and on(cfg) == ["a.jar", "c.jar"] and dis(cfg) == ["crash.jar"], f"crash.jar off, others on ({s['result']})")
ok(json.load(open(os.path.join(cfg.disabled_dir, "crash.jar" + lib.SIDECAR)))["reason"] == hm.UNNAMED,
   "its note says the log named no mod")

print("9. a mod named in the log by its manifest (Group:Name), not its file name")
cfg, f = setup(broken={"weird-file-1.2.zip": ("named", "v2")})
with zipfile.ZipFile(os.path.join(cfg.mods_dir, "weird-file-1.2.zip"), "w") as z:
    z.writestr("manifest.json", json.dumps({"Group": "Coo", "Name": "Lanterns", "Version": "1.2"}))
hu.run(cfg, f)
ok(dis(cfg) == ["weird-file-1.2.zip"], "matched by Coo:Lanterns")

print("10. a zipped download is unpacked; backups are pruned")
cfg, f = setup(zipped=True); cfg.keep_backups = 2
for i in range(3):
    lib.backup_world(cfg.world_dir, cfg.backup_dir, f"2026010{i}T000000Z", 2)
hu.run(cfg, f)
ok(jar(cfg) == "v2" and os.path.exists(os.path.join(cfg.server_dir, "Assets.zip")), "zip unpacked and installed")
ok(len(os.listdir(cfg.backup_dir)) == 2, "only the newest 2 backups kept")

print("11. log reading on its own")
mods = {"lanterns.jar": {"lanterns"}, "core.jar": {"core"}}
log = "[SEVERE] java.lang.NoClassDefFoundError: com/x/Y\n  at ...\n[SEVERE] while enabling lanterns\nok"
ok(lib.find_failed_mods(log, mods) == {"lanterns.jar": "[SEVERE] java.lang.NoClassDefFoundError: com/x/Y"},
   "an exception blames the mod named within 3 lines")
ok(lib.find_failed_mods("[INFO] loaded corelanterns fine\n[SEVERE] Failed to load pending plugin core-extra", mods) == {},
   "no blame from a partial name")

print("PASS" if all(r) else "FAILED")
raise SystemExit(0 if all(r) else 1)
