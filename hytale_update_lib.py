"""
hytale_update_lib: the pure and file-system parts of hytale-update (log reading, mods in and out, world backups,
server file install and rollback). Imported by hytale_update.py, never run. Nothing here runs a command or sleeps,
so tests/test_hytale_update.py can drive all of it on temp folders.
"""
import json
import os
import re
import shutil
import tarfile
import zipfile

# ⚠️ FROM COMMUNITY SOURCES, NOT HYTALE'S DOCS (hosting guides and plugin issue trackers, 2026). Verify each against
#    a real server log and fix the regex here; this table is the only place they live.
#    kind "mod": the line itself names the mod. kind "near": an exception line; the mod is named within NEAR lines.
FAILURE_PATTERNS = [
    ("pending-plugin", re.compile(r"Failed to load pending plugin", re.I), "mod"),
    ("version-range", re.compile(r"targets server version range", re.I), "mod"),
    ("manifest", re.compile(r"Failed to load manifest file", re.I), "mod"),
    ("duplicate", re.compile(r"Tried to load duplicate plugin", re.I), "mod"),
    ("plugin-error", re.compile(r"(Failed|Error|Could not) (to )?(load|enable|start)\w* plugin", re.I), "mod"),
    ("no-class", re.compile(r"NoClassDefFoundError|ClassNotFoundException|NoSuchMethodError", re.I), "near"),
    ("class-cast", re.compile(r"ClassCastException", re.I), "near"),
    ("java-too-old", re.compile(r"UnsupportedClassVersionError", re.I), "near"),
]
NEAR = 3
# Hytale logs a warning before it removes something (Update 8: the Netty transport, `--transport QUIC`, goes in 0.9).
DEPRECATION = re.compile(r"deprecat|will be removed|no longer supported|migration guide", re.I)
MOD_SUFFIXES = (".jar", ".zip", ".hmod")
SIDECAR = ".disabled.json"


def mod_files(folder):
    """Mod files in a folder, sorted. Sidecars and dot-files are not mods."""
    if not os.path.isdir(folder):
        return []
    return sorted(n for n in os.listdir(folder) if n.lower().endswith(MOD_SUFFIXES) and not n.startswith("."))


def mod_ids(path):
    """Names a log may use for this mod: the file name without extension, and the manifest's Name and Group:Name."""
    stem = os.path.basename(path).rsplit(".", 1)[0]
    ids = {stem}
    try:
        with zipfile.ZipFile(path) as z:
            m = json.loads(z.read("manifest.json"))
        if m.get("Name"):
            ids.add(m["Name"])
            if m.get("Group"):
                ids.update({f"{m['Group']}:{m['Name']}", f"{m['Group']}/{m['Name']}", f"{m['Group']}.{m['Name']}"})
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        pass
    return {i.lower() for i in ids if len(i) >= 3}


def _names(line, ident):
    return re.search(r"(?<![\w-])" + re.escape(ident) + r"(?![\w-])", line.lower()) is not None


def find_failed_mods(log_text, mods):
    """mods = {file: ids}. Returns {file: the log line that condemned it}. A failure line that names no mod is
    ignored here; the caller notices the server itself did not come up."""
    lines = log_text.splitlines()
    out = {}
    for i, line in enumerate(lines):
        for _, rx, kind in FAILURE_PATTERNS:
            if not rx.search(line):
                continue
            window = [line] if kind == "mod" else lines[max(0, i - NEAR): i + NEAR + 1]
            for f, ids in mods.items():
                if f not in out and any(_names(w, x) for w in window for x in ids):
                    out[f] = line.strip()[:300]
            break
    return out


def early_warnings(log_text, limit=10):
    seen = []
    for line in log_text.splitlines():
        s = line.strip()[:300]
        if DEPRECATION.search(s) and s not in seen:
            seen.append(s)
    return seen[:limit]


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(data, f, indent=1)
    os.replace(path + ".tmp", path)


# ── mods in and out ───────────────────────────────────────────────────────────
def disable(mods_dir, disabled_dir, name, reason, version, when):
    """Move a mod to the disabled folder with a note saying why. A mod disabled before keeps its first date."""
    os.makedirs(disabled_dir, exist_ok=True)
    side = os.path.join(disabled_dir, name + SIDECAR)
    old = load_json(side, {})
    shutil.move(os.path.join(mods_dir, name), os.path.join(disabled_dir, name))
    save_json(side, {"mod": name, "reason": reason, "server_version": version,
                     "disabled_since": old.get("disabled_since", when), "last_tried": when})


def try_again(disabled_dir, mods_dir, name):
    """Put a disabled mod back for a trial boot. Its note stays until the trial passes (confirm) or fails."""
    shutil.move(os.path.join(disabled_dir, name), os.path.join(mods_dir, name))


def confirm(disabled_dir, name):
    side = os.path.join(disabled_dir, name + SIDECAR)
    if os.path.exists(side):
        os.remove(side)


def disabled_notes(disabled_dir):
    return [load_json(os.path.join(disabled_dir, n + SIDECAR), {"mod": n}) for n in mod_files(disabled_dir)]


# ── world backups ─────────────────────────────────────────────────────────────
def backup_world(world_dir, backup_dir, stamp, keep):
    """world -> backup_dir/world-<stamp>.tar.gz; only the newest `keep` archives stay."""
    os.makedirs(backup_dir, exist_ok=True)
    path = os.path.join(backup_dir, f"world-{stamp}.tar.gz")
    with tarfile.open(path + ".part", "w:gz") as t:
        t.add(world_dir, arcname=os.path.basename(world_dir.rstrip("/")))
    os.replace(path + ".part", path)
    archives = sorted(n for n in os.listdir(backup_dir) if n.startswith("world-") and n.endswith(".tar.gz"))
    for old in archives[:-keep] if keep > 0 else []:
        os.remove(os.path.join(backup_dir, old))
    return path


def restore_world(archive, world_dir):
    parent = os.path.dirname(world_dir.rstrip("/"))
    if os.path.exists(world_dir):
        shutil.rmtree(world_dir)
    with tarfile.open(archive) as t:
        t.extractall(parent, filter="data")


# ── server files ──────────────────────────────────────────────────────────────
def unpack_download(staging):
    """The downloader may leave one zip; unpack it. Returns the folder holding the server files."""
    entries = [n for n in os.listdir(staging) if not n.startswith(".")]
    if len(entries) == 1 and entries[0].lower().endswith(".zip"):
        out = os.path.join(staging, ".unpacked")
        with zipfile.ZipFile(os.path.join(staging, entries[0])) as z:
            z.extractall(out)
        return out
    return staging


def install_files(src, server_dir, previous_dir, keep=()):
    """Copy every top-level entry of src into server_dir. What it replaces moves to previous_dir first, and a
    manifest records what was added, so rollback_files can put the old server back exactly. Entries named in
    `keep` (the world, mods, configs) are never replaced, even if the download carries them."""
    if os.path.exists(previous_dir):
        shutil.rmtree(previous_dir)
    os.makedirs(previous_dir)
    replaced, added = [], []
    for name in sorted(os.listdir(src)):
        if name.startswith(".") or name in keep:
            continue
        dst = os.path.join(server_dir, name)
        if os.path.lexists(dst):
            shutil.move(dst, os.path.join(previous_dir, name))
            replaced.append(name)
        else:
            added.append(name)
        (shutil.copytree if os.path.isdir(os.path.join(src, name)) else shutil.copy2)(os.path.join(src, name), dst)
    save_json(os.path.join(previous_dir, ".manifest.json"), {"replaced": replaced, "added": added})
    return replaced + added


def rollback_files(server_dir, previous_dir):
    m = load_json(os.path.join(previous_dir, ".manifest.json"), None)
    if m is None:
        return False
    for name in m["added"] + m["replaced"]:
        p = os.path.join(server_dir, name)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        elif os.path.lexists(p):
            os.remove(p)
    for name in m["replaced"]:
        shutil.move(os.path.join(previous_dir, name), os.path.join(server_dir, name))
    return True
