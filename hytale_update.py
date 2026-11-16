#!/usr/bin/env python3
"""
hytale-update: keep a Dockerised Hytale server on the latest version, and turn off mods that do not work with it.

Run nightly (a systemd timer or cron) while the server sleeps. Each run:
1. Skips if anyone might be playing (hytale-idle says running/starting, or the container is up), or another run
   holds the lock. Pauses hytale-idle for the run, so a join attempt cannot start a half-installed server.
2. Asks the downloader for the latest version. Same as installed and no disabled mod due a retry: done.
3. Backs up the world, installs the new server files (the old ones are kept for rollback).
4. Boots with every mod, disabled ones included as a retry. A mod the log blames is moved to mods-disabled/
   with a note (why, which version, since when); a retried mod that now loads goes back on. A boot that fails
   naming no mod is narrowed down one mod at a time.
5. Server will not start even with no mods: the old server files and the world backup go back, the old version
   is booted to prove it, and an alert marker is written.
6. Leaves the server stopped (hytale-idle wakes it on the next join) and writes a status file with what changed
   and any deprecation warnings Hytale logged ("early warnings": things the next update will remove).
Everything external goes through Ops, so tests/test_hytale_update.py runs all of it offline with a fake server.
"""
import argparse
import fcntl
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

import hytale_update_lib as lib

__version__ = "0.2.0"


def log(msg): print(f"hytale-update: {msg}", flush=True)


def utc(t): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class Ops:
    """The only door to the outside world. Tests replace it."""
    def run(self, argv, timeout=1800):
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            return r.returncode, r.stdout + r.stderr
        except (OSError, subprocess.TimeoutExpired) as e:
            return 1, str(e)

    def sleep(self, s): time.sleep(s)

    def now(self): return time.time()


class Server:
    def __init__(self, cfg, ops):
        self.c, self.ops, self.docker = cfg, ops, shlex.split(cfg.docker)

    def _d(self, *a, timeout=600): return self.ops.run(self.docker + list(a), timeout)

    def field(self, fmt):
        rc, out = self._d("inspect", "-f", fmt, self.c.container)
        return out.strip() if rc == 0 else None

    def running(self): return self.field("{{.State.Running}}") == "true"

    def stop(self): self._d("stop", "-t", str(self.c.stop_timeout), self.c.container, timeout=self.c.stop_timeout + 60)

    def boot(self):
        """Start, give it settle_seconds (or until --ready-regex appears, at most boot_timeout), read the log, stop.
        Up = still running with no restart since start (the same rule as vps-image-refresh's check_hytale)."""
        since, before = utc(self.ops.now()), self.field("{{.RestartCount}}")
        self._d("start", self.c.container)
        waited, logs = 0, ""
        while True:
            step = min(self.c.poll_seconds, max(1, self.c.settle_seconds - waited))
            self.ops.sleep(step)
            waited += step
            logs = self._d("logs", "--since", since, self.c.container)[1]
            if not self.running():
                break
            if self.c.ready_regex:
                if re.search(self.c.ready_regex, logs) or waited >= self.c.boot_timeout:
                    break
            elif waited >= self.c.settle_seconds:
                break
        up = self.running() and self.field("{{.RestartCount}}") == before
        if up and self.c.ready_regex:
            up = re.search(self.c.ready_regex, logs) is not None
        self.stop()
        return up, logs


def idle_says_busy(path):
    return lib.load_json(path, {}).get("state") in ("running", "starting")


def run(cfg, ops):
    os.makedirs(os.path.dirname(cfg.lock) or ".", exist_ok=True)
    lock = open(cfg.lock, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return finish(cfg, ops, {"result": "skipped", "why": "another hytale-update run holds the lock"})
    srv = Server(cfg, ops)
    if idle_says_busy(cfg.idle_status) or srv.running():
        return finish(cfg, ops, {"result": "skipped", "why": "the server is up; someone may be playing"})
    if cfg.pause_cmd:
        ops.run(shlex.split(cfg.pause_cmd), 60)
    try:
        return update(cfg, ops, srv)
    finally:
        if srv.running():
            srv.stop()
        if cfg.resume_cmd:
            ops.run(shlex.split(cfg.resume_cmd), 60)


def update(cfg, ops, srv):
    from hytale_update_mods import settle_mods  # the boot loop, split out to keep each file readable
    state = lib.load_json(cfg.state_file, {})
    installed, now = state.get("installed_version"), ops.now()
    rc, out = ops.run(shlex.split(cfg.version_cmd), 300)
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    if rc != 0 or not lines:
        return finish(cfg, ops, {"result": "failed", "why": f"could not read the latest version: {out[-300:]}"}, True)
    latest = lines[-1].split()[-1]
    disabled = lib.mod_files(cfg.disabled_dir)
    retry_due = bool(disabled) and now - state.get("last_retry", 0) >= cfg.retry_days * 86400
    if latest == installed and not retry_due:
        return finish(cfg, ops, {"result": "unchanged", "installed_version": installed})
    report = {"installed_version": installed, "latest_version": latest, "world_backup": None}
    if os.path.isdir(cfg.world_dir):
        report["world_backup"] = lib.backup_world(cfg.world_dir, cfg.backup_dir, time.strftime(
            "%Y%m%dT%H%M%SZ", time.gmtime(now)), cfg.keep_backups)
    previous = os.path.join(cfg.server_dir, ".previous")
    new_files = latest != installed
    if new_files:
        staging = os.path.join(cfg.server_dir, ".staging")
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging)
        rc, out = ops.run([a.replace("{dest}", staging) for a in shlex.split(cfg.download_cmd)], 3600)
        if rc != 0:
            return finish(cfg, ops, dict(report, result="failed", why=f"download failed: {out[-300:]}"), True)
        keep = {os.path.basename(p.rstrip("/")) for p in (cfg.world_dir, cfg.mods_dir, cfg.disabled_dir)}
        keep |= set(cfg.keep)
        lib.install_files(lib.unpack_download(staging), cfg.server_dir, previous, keep)
        shutil.rmtree(staging, ignore_errors=True)
    outcome = settle_mods(cfg, srv, latest, utc(now))
    report.update(outcome)
    if outcome["up"]:
        state.update(installed_version=latest, last_retry=now)
        lib.save_json(cfg.state_file, state)
        return finish(cfg, ops, dict(report, result="updated" if new_files else "mods-retried"))
    # ⛔ Not even a bare server boots on this version: put everything back the way it was.
    if new_files:
        lib.rollback_files(cfg.server_dir, previous)
    if report["world_backup"]:
        lib.restore_world(report["world_backup"], cfg.world_dir)
    outcome["restore_mods"]()
    old_up = srv.boot()[0] if new_files else None
    why = ("the server did not start on the new version even with every mod off" if new_files else
           "the server did not start even with every mod off (no update was installed; mods put back as they were)")
    return finish(cfg, ops, dict(report, result="rolled-back" if new_files else "failed", old_version_boots=old_up,
                                 why=why), True)


def finish(cfg, ops, report, alert=False):
    report = {k: v for k, v in report.items() if not callable(v)}
    report.update(updated=utc(ops.now()), disabled_mods=lib.disabled_notes(cfg.disabled_dir))
    lib.save_json(cfg.status_file, report)
    summary = f"{report['result']}: " + ", ".join(f"{k}={report[k]}" for k in (
        "installed_version", "latest_version", "newly_disabled", "re_enabled", "why") if report.get(k))
    log(summary)
    for w in report.get("early_warnings") or []:
        log(f"early warning: {w}")
    if alert:
        lib.save_json(cfg.alert_marker, report)
    elif report["result"] != "skipped" and os.path.exists(cfg.alert_marker):
        os.remove(cfg.alert_marker)
    return 1 if alert else 0


def config(argv=None):
    # ⚠️ TODO(real server): paths, the downloader's location and flags, and how the container mounts the server
    #    folder are guesses until the VPS's `docker inspect hytale` is read. Every one is a flag.
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--version", action="version", version=f"hytale-update {__version__}")
    a = ap.add_argument
    a("--container", default="hytale")
    a("--docker", default="docker")
    a("--server-dir", default="/opt/hytale/server", help="host folder holding HytaleServer.jar (TODO: real mount)")
    a("--world-dir", default="", help="default: <server-dir>/universe")
    a("--mods-dir", default="", help="default: <server-dir>/mods")
    a("--disabled-dir", default="", help="default: <server-dir>/mods-disabled")
    a("--keep", nargs="*", default=["config.json", "permissions.json", "whitelist.json", "bans.json", "logs"],
      help="top-level names a download never replaces")
    a("--backup-dir", default="/var/backups/hytale")
    a("--keep-backups", type=int, default=5)
    a("--version-cmd", default="hytale-downloader -print-version", help="prints the latest version (last word)")
    a("--download-cmd", default="hytale-downloader -download-path {dest}", help="TODO: verify the real flags")
    a("--idle-status", default="/var/lib/hytale-idle/status.json")
    a("--pause-cmd", default="systemctl stop hytale-idle")
    a("--resume-cmd", default="systemctl start hytale-idle")
    a("--settle-seconds", type=int, default=90)
    a("--boot-timeout", type=int, default=300)
    a("--poll-seconds", type=int, default=10)
    a("--ready-regex", default="", help="a log line that means 'ready' (TODO: learn it from a real boot)")
    a("--stop-timeout", type=int, default=90)
    a("--retry-days", type=float, default=7, help="retry disabled mods this often even with no new version")
    a("--max-boots", type=int, default=12)
    a("--state-file", default="/var/lib/hytale-idle/update-state.json")
    a("--status-file", default="/var/lib/hytale-idle/update.json")
    a("--alert-marker", default="/var/lib/hytale-idle/update-alert.json")
    a("--lock", default="/run/hytale-update.lock")
    c = ap.parse_args(argv)
    c.world_dir = c.world_dir or os.path.join(c.server_dir, "universe")
    c.mods_dir = c.mods_dir or os.path.join(c.server_dir, "mods")
    c.disabled_dir = c.disabled_dir or os.path.join(c.server_dir, "mods-disabled")
    return c


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(run(config(), Ops()))
