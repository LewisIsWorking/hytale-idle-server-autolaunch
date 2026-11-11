import importlib.util, socket, threading, time
spec = importlib.util.spec_from_file_location("h", __import__("os").path.join(__import__("os").path.dirname(__file__), "..", "hytale_idle.py")); h = importlib.util.module_from_spec(spec); spec.loader.exec_module(h)
ok = lambda c, m: print(("ok   " if c else "FAIL ") + m) or c
r = []
info = {"HostConfig": {"PortBindings": {"5520/udp": [{"HostIp": "", "HostPort": "5520"}, {"HostIp": "::", "HostPort": "5520"}], "8080/tcp": [{"HostPort": "8080"}]}}}
r.append(ok(h.udp_ports(info) == [5520], "udp ports: only the UDP one, deduped"))
r.append(ok(h.udp_ports({"HostConfig": {}}) == [], "no bindings -> none"))
W, B = 900, 65536
s = [(0, 0), (60, 10), (900, 1000), (960, 1100)]
r.append(ok(h.should_stop(s, 960, 0, W, B), "quiet window -> stop"))
r.append(ok(not h.should_stop(s, 960, 100, W, B), "up less than the window -> keep"))
s2 = [(0, 0), (60, 10), (900, 900_000), (960, 900_100)]
r.append(ok(not h.should_stop(s2, 960, 0, W, B), "player traffic in window -> keep"))
r.append(ok(not h.should_stop([(500, 0), (960, 0)], 960, 0, W, B), "no sample old enough -> keep"))
r.append(ok(not h.should_stop([(0,0),(60,0),(960,70000)], 960, 0, W, B), "70 KiB in window -> keep"))
# knock: real UDP on a free port
p = 25520
def send(): time.sleep(0.5); c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); c.sendto(b"hi", ("127.0.0.1", p))
threading.Thread(target=send).start()
r.append(ok(h.wait_for_knock([p], 5) is True, "IPv4 packet on the dual-stack socket counts as a knock"))
r.append(ok(h.wait_for_knock([p], 0.5) is False, "silence -> no knock"))
b = h.bind_udp(p)
r.append(ok(h.wait_for_knock([p], 0.5) is None, "port busy -> None (retry)")); b.close()
import os; r.append(ok(h.rx_bytes(os.getpid()) >= 0, "rx_bytes reads /proc net/dev"))
import json as _j, tempfile as _t, os as _o
_p = _o.path.join(_t.mkdtemp(), "sub", "status.json"); h.write_status(_p, state="asleep", ports=[5520])
_d = _j.load(open(_p)); r.append(ok(_d["state"] == "asleep" and "updated" in _d, "status file written atomically with a timestamp"))
h.write_status("", state="x"); r.append(ok(True, "empty path = no status file, no error"))
# status for people: one line per state
r.append(ok(h.describe({"state": "asleep", "container": "hytale"}).startswith("asleep: hytale starts on the next join"), "describe: asleep"))
r.append(ok("join again in about a minute" in h.describe({"state": "starting", "last_join_attempt": "t"}), "describe: starting"))
r.append(ok("nobody seems to be playing" in h.describe({"state": "running", "players_likely": False}), "describe: running, empty"))
r.append(ok(h.describe({}).startswith("unknown"), "describe: no status"))
# status over HTTP: read-only, the file as written, 503 before the first write, 404 elsewhere
import urllib.request as _u, urllib.error as _e
_d2 = _t.mkdtemp(); _sp = _o.path.join(_d2, "status.json")
_srv = h.serve_status("127.0.0.1:0", _sp); _port = _srv.server_address[1]
try:
    _u.urlopen(f"http://127.0.0.1:{_port}/status.json"); r.append(ok(False, "http: 503 before any status"))
except _e.HTTPError as e: r.append(ok(e.code == 503, "http: 503 before any status"))
h.write_status(_sp, state="asleep", container="hytale")
_got = _j.loads(_u.urlopen(f"http://127.0.0.1:{_port}/status.json").read())
r.append(ok(_got["state"] == "asleep", "http: serves the status file"))
try:
    _u.urlopen(f"http://127.0.0.1:{_port}/etc/passwd"); r.append(ok(False, "http: other paths 404"))
except _e.HTTPError as e: r.append(ok(e.code == 404, "http: other paths 404"))
_srv.shutdown()
print("PASS" if all(r) else "FAILED")
raise SystemExit(0 if all(r) else 1)
