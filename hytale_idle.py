#!/usr/bin/env python3
"""
hytale-idle: start the Hytale container when someone tries to join, stop it when nobody has played for a while.

Minecraft has lazymc and mc-router for this, which speak its protocol. This one does not parse Hytale's protocol
at all, so a Hytale update cannot break it:

- STOPPED: listen on the container's published UDP ports (Hytale uses QUIC, default 5520). The first packet that
  arrives is someone joining: close the ports, `docker start` the container. That first attempt fails; the player
  joins again once the world has loaded (about a minute).
- RUNNING: every minute, read how many bytes the container has received. A connected player sends packets all the
  time; an empty server receives almost nothing. When less than --idle-kib arrived in the last --idle-minutes (and
  the server has been up at least that long), `docker stop -t 90` so the world is saved.

⛔ It never stops a server someone is on, and a server started by hand also gets the full window first.
Runs from systemd (hytale-idle.service, set up by install.sh); logs: `journalctl -u hytale-idle -f`.
Is it down or asleep? `hytale-idle status`, the status file, or (with --status-http) GET /status.json.
"""
import argparse
import http.server
import json
import os
import select
import socket
import subprocess
import sys
import threading
import time

__version__ = "0.1.0"
DEFAULT_STATUS = "/var/lib/hytale-idle/status.json"


def log(msg): print(f"hytale-idle: {msg}", flush=True)


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def inspect(container):
    r = docker("inspect", container)
    if r.returncode != 0:
        return None
    return json.loads(r.stdout)[0]


def udp_ports(info):
    """Host UDP ports the container publishes. Pure."""
    ports = set()
    for key, binds in ((info.get("HostConfig") or {}).get("PortBindings") or {}).items():
        if key.endswith("/udp"):
            for b in binds or []:
                if b.get("HostPort"):
                    ports.add(int(b["HostPort"]))
    return sorted(ports)


def rx_bytes(pid):
    """Bytes received by every interface but lo inside the container's network namespace."""
    total = 0
    with open(f"/proc/{pid}/net/dev") as f:
        for line in f.readlines()[2:]:
            name, data = line.split(":", 1)
            if name.strip() != "lo":
                total += int(data.split()[0])
    return total


def should_stop(samples, now, started_at, idle_s, idle_bytes):
    """Pure. samples = [(time, rx_bytes)], oldest first. True only when the server has been up for the whole
    window AND a sample at least idle_s old exists AND less than idle_bytes arrived since it."""
    if now - started_at < idle_s:
        return False
    old = [s for s in samples if now - s[0] >= idle_s]
    if not old:
        return False
    base = old[-1]
    return samples[-1][1] - base[1] < idle_bytes


def bind_udp(port):
    """One socket for IPv4 and IPv6 where the host has IPv6, else IPv4 only."""
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        addr = ("::", port)
    except OSError:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        addr = ("0.0.0.0", port)
    try:
        s.bind(addr)
    except OSError:
        s.close()
        raise
    return s


def wait_for_knock(ports, poll_s):
    """Bind the ports and wait up to poll_s for a packet. True = someone knocked. None = could not bind."""
    socks = []
    try:
        for p in ports:
            socks.append(bind_udp(p))
    except OSError as e:
        [s.close() for s in socks]
        log(f"cannot listen on {ports} yet ({e}); retrying")
        return None
    try:
        ready, _, _ = select.select(socks, [], [], poll_s)
        for s in ready:
            _, addr = s.recvfrom(2048)
            log(f"join attempt from {addr[0]}:{addr[1]}")
        return bool(ready)
    finally:
        [s.close() for s in socks]


def write_status(path, **fields):
    """Atomically replace the status file agents and people read (state, times, traffic). Never fatal."""
    if not path:
        return
    fields["updated"] = utc(time.time())
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w") as f:
            json.dump(fields, f, indent=1)
        os.replace(path + ".tmp", path)
    except OSError as e:
        log(f"cannot write {path} ({e})")


def serve_status(addr, path):
    """Serve the status file read-only over HTTP (GET / or /status.json), so dashboards and AI agents can tell
    'asleep' from 'down' without shell access. Off unless --status-http is given; bind it to localhost or a
    private address unless you mean the world to see your server's state."""
    host, _, port = addr.rpartition(":")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/", "/status.json"):
                self.send_error(404)
                return
            try:
                body = open(path, "rb").read()
                code = 200
            except OSError:
                body, code = b'{"state": "unknown", "why": "no status written yet"}', 503
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer((host.strip("[]") or "127.0.0.1", int(port)), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"status served on http://{addr}/status.json")
    return server


def describe(st):
    """One line for people from a status dict. Pure."""
    state = st.get("state", "unknown")
    if state == "asleep":
        return f"asleep: {st.get('container')} starts on the next join attempt (last stop {st.get('last_stop') or 'never'})"
    if state == "starting":
        return f"starting: a join attempt at {st.get('last_join_attempt')} woke it; join again in about a minute"
    if state == "running":
        who = "players are on" if st.get("players_likely") else "nobody seems to be playing"
        return f"running since {st.get('up_since')}; {who}; earliest stop in {st.get('earliest_stop_in_minutes')} min"
    return f"{state}: {st.get('why', 'no status yet')}"


def utc(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if t else None

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_STATUS
        try:
            st = json.load(open(path))
        except (OSError, ValueError) as e:
            sys.exit(f"hytale-idle: no status at {path} ({e}); is the service running?")
        print(describe(st))
        return
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--version", action="version", version=f"hytale-idle {__version__}")
    ap.add_argument("--container", default="hytale")
    ap.add_argument("--idle-minutes", type=float, default=15)
    ap.add_argument("--idle-kib", type=float, default=64, help="received less than this in the window = idle")
    ap.add_argument("--poll-seconds", type=float, default=60)
    ap.add_argument("--stop-timeout", type=int, default=90, help="seconds Hytale gets to save on stop")
    ap.add_argument("--status-file", default=DEFAULT_STATUS, help="'' = none")
    ap.add_argument("--status-http", default="", metavar="HOST:PORT",
                    help="also serve the status file read-only over HTTP, e.g. 127.0.0.1:8765 ('' = off)")
    a = ap.parse_args()
    idle_s, idle_bytes = a.idle_minutes * 60, a.idle_kib * 1024

    info = inspect(a.container)
    if info is None:
        sys.exit(f"hytale-idle: no container named {a.container}")
    ports = udp_ports(info)
    if not ports:
        sys.exit(f"hytale-idle: {a.container} publishes no UDP port")
    log(f"watching {a.container}: UDP {ports}, stop after {a.idle_minutes:g} min under {a.idle_kib:g} KiB received")
    if a.status_http and a.status_file:
        serve_status(a.status_http, a.status_file)

    samples, started_at, pid = [], time.time(), None
    st = {"container": a.container, "ports": ports, "idle_minutes": a.idle_minutes, "idle_kib": a.idle_kib,
          "last_start": None, "last_stop": None, "last_join_attempt": None}
    while True:
        info = inspect(a.container)
        if info is None:
            log("container is gone; waiting")
            time.sleep(a.poll_seconds)
            continue
        state = info["State"]
        if not state.get("Running"):
            samples, pid = [], None
            write_status(a.status_file, state="asleep", **st)
            knocked = wait_for_knock(ports, a.poll_seconds)
            if knocked is None:
                time.sleep(5)
            elif knocked:
                st["last_join_attempt"] = utc(time.time())
                r = docker("start", a.container)
                log("started" if r.returncode == 0 else f"start failed: {r.stderr.strip()}")
                started_at = time.time()
                st["last_start"] = utc(started_at)
                write_status(a.status_file, state="starting", **st)
            continue
        if state.get("Pid") != pid:  # (re)started, by us or by hand: give it the full window
            pid, samples, started_at = state.get("Pid"), [], time.time()
        try:
            now = time.time()
            samples.append((now, rx_bytes(pid)))
            samples = [s for s in samples if now - s[0] <= idle_s + 3 * a.poll_seconds]
        except (OSError, ValueError) as e:
            log(f"cannot read traffic ({e}); not stopping")
            time.sleep(a.poll_seconds)
            continue
        if should_stop(samples, now, started_at, idle_s, idle_bytes):
            got = samples[-1][1] - [s for s in samples if now - s[0] >= idle_s][-1][1]
            log(f"idle ({got} bytes in {a.idle_minutes:g} min); stopping")
            r = docker("stop", "-t", str(a.stop_timeout), a.container)
            log("stopped" if r.returncode == 0 else f"stop failed: {r.stderr.strip()}")
            st["last_stop"] = utc(time.time())
            continue
        old = [x for x in samples if now - x[0] >= idle_s]
        window = samples[-1][1] - (old[-1][1] if old else samples[0][1])
        left = max(0.0, (idle_s - (now - started_at)) / 60)
        write_status(a.status_file, state="running", up_since=utc(started_at), received_in_window_bytes=window,
                     players_likely=window >= idle_bytes, earliest_stop_in_minutes=round(left, 1), **st)
        time.sleep(a.poll_seconds)


if __name__ == "__main__":
    main()
