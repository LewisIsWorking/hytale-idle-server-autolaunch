"""
hytale_update_mods: the boot loop of hytale-update. Boots the server until it comes up with every mod that can load,
turning off the ones that cannot. Imported by hytale_update.py, never run.

- Every disabled mod is put back on first, as a retry. A mod the log blames goes (back) to mods-disabled/.
- After any mod is disabled, the server is booted again, so success always means "this exact set came up".
- A failed boot that names no mod: every mod goes off, the bare server is booted, then the mods come back one at a
  time and each one whose boot fails stays off. A bare server that will not boot is a server problem, not a mod
  problem: the caller rolls back.
- At most cfg.max_boots boots, so a run always ends.
"""
import os
import shutil

import hytale_update_lib as lib

UNNAMED = "the server did not start with this mod on (the log named no mod)"


def settle_mods(cfg, srv, version, when):
    mods, off = cfg.mods_dir, cfg.disabled_dir
    os.makedirs(mods, exist_ok=True)
    before_on, before_off = set(lib.mod_files(mods)), set(lib.mod_files(off))
    notes = {f: lib.load_json(os.path.join(off, f + lib.SIDECAR), None) for f in before_off}
    for f in before_off:
        lib.try_again(off, mods, f)
    out = {"up": False, "boots": 0, "early_warnings": [], "key_log_lines": []}

    def boot():
        out["boots"] += 1
        up, logs = srv.boot()
        for w in lib.early_warnings(logs):
            if w not in out["early_warnings"]:
                out["early_warnings"].append(w)
        failed = lib.find_failed_mods(logs, {f: lib.mod_ids(os.path.join(mods, f)) for f in lib.mod_files(mods)})
        out["key_log_lines"] = list(failed.values()) + logs.splitlines()[-20:]
        return up, failed

    def off_(f, why): lib.disable(mods, off, f, why, version, when)

    while out["boots"] < cfg.max_boots:
        on = lib.mod_files(mods)
        up, failed = boot()
        for f, line in failed.items():
            off_(f, line)
        if failed:
            continue
        if up or not on:
            out["up"] = up
            break
        # Down, and the log blames nobody: find the culprit by adding the mods back one at a time.
        for f in on:
            off_(f, UNNAMED)
        if not boot()[0]:
            break
        out["up"] = True
        for f in on:
            if out["boots"] >= cfg.max_boots:
                break  # the rest stay off with UNNAMED; the next run retries them
            lib.try_again(off, mods, f)
            up, failed = boot()
            if not up or failed:
                off_(f, failed.get(f) or next(iter(failed.values()), UNNAMED))
                for g in failed:
                    if g != f and g in lib.mod_files(mods):
                        off_(g, failed[g])
        break

    now_on, now_off = set(lib.mod_files(mods)), set(lib.mod_files(off))
    if out["up"]:
        for f in now_on:
            lib.confirm(off, f)
    out["newly_disabled"] = sorted(now_off & before_on)
    out["re_enabled"] = sorted(now_on & before_off)
    out["still_disabled"] = sorted(now_off & before_off)

    def restore_mods():
        """Put every mod back where it was before this run (for a rollback to the old version)."""
        for f in before_on & set(lib.mod_files(off)):
            lib.try_again(off, mods, f)
            lib.confirm(off, f)
        for f in before_off & set(lib.mod_files(mods)):
            shutil.move(os.path.join(mods, f), os.path.join(off, f))
        for f, note in notes.items():  # this run may have rewritten a note; put the original back
            if note is not None:
                lib.save_json(os.path.join(off, f + lib.SIDECAR), note)
    out["restore_mods"] = restore_mods
    if not out["up"]:
        out["newly_disabled"] = out["re_enabled"] = []
    return out
