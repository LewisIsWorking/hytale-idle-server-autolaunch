# For AI agents

You are probably here because a Hytale server's port is closed and you are deciding whether that is an outage.

## Is it down, or asleep?

Read the state before acting:

- Shell on the server: `hytale-idle status`, or `cat /var/lib/hytale-idle/status.json`.
- No shell: if the operator enabled it, `GET http://<status-http address>/status.json`.

| `state` | Meaning | What to do |
|---|---|---|
| `asleep` | Stopped on purpose: nobody played for `idle_minutes`. | Nothing. The next join attempt starts it. |
| `starting` | A join attempt woke it; the world is loading. | Wait about a minute, then try again. |
| `running` | Up. `players_likely` says whether someone seems to be on. | Normal checks apply. |

## Do not

- Treat `asleep` as an outage, alert on it, or "fix" it.
- Restart the container or change its restart policy to keep it awake. That defeats the point, and
  hytale-idle stops it again after the idle window anyway.
- Stop the container while `players_likely` is true.

## Do

- Alert if the `hytale-idle` service itself is not running: `systemctl is-active hytale-idle`.
- To wake the server deliberately, send any UDP packet to its game port, or run `docker start <container>`.
- To keep it awake for a while (maintenance, an event): `systemctl stop hytale-idle`, and
  `systemctl start hytale-idle` afterwards.

## Code map

- `hytale_idle.py`: everything. Pure helpers (`udp_ports`, `should_stop`, `describe`) are covered by
  `tests/test_hytale_idle.py`. The main loop sleeps on the UDP ports while the container is stopped, and samples
  received bytes from `/proc/<pid>/net/dev` while it runs.
- `install.sh`: the setup wizard (`--dry-run` changes nothing).
