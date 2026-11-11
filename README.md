# hytale-idle

Your Hytale server sleeps when nobody plays and wakes up when someone tries to join.
(This repository is `hytale-idle-server-autolaunch`; the tool it installs is called `hytale-idle`.)

A Hytale server holds gigabytes of memory and some CPU around the clock, even when the last player logged off
hours ago. On a small VPS that's memory your other services could use. hytale-idle stops the server after a
quiet spell and starts it again the moment a player knocks.

- **Zero changes to your server.** It works with the Docker container you already run: same image, same ports,
  same world. No proxy in front of the game traffic.
- **Can't be broken by a Hytale update.** It never parses the game's protocol. Asleep, any packet on the server's
  UDP port is a knock; awake, it only counts bytes received.
- **Never stops a server someone is on.** A connected player always sends more than the idle threshold, and a
  freshly started server always gets the full idle window first.
- **Saves the world on stop.** `docker stop -t 90` gives Hytale time to save.
- **Tells you (and your AI agents) whether it's down or just asleep.** See [Status](#status).

## How it feels for players

The first player to join a sleeping server sees the connection fail. About a minute later the world has
loaded, and they join again normally. Tell your players: "if it doesn't connect, try again in a minute."

## Install

Needs Linux with systemd, Docker and Python 3 (no packages beyond the standard library), and a Hytale server
running in a Docker container with its UDP port published (for example `-p 5520:5520/udp`).

```
git clone https://github.com/LewisIsWorking/hytale-idle-server-autolaunch
cd hytale-idle-server-autolaunch
./install.sh --dry-run      # see exactly what it would do; changes nothing
sudo ./install.sh           # asks which container and how long to wait
```

Non-interactive: `sudo ./install.sh --container hytale --idle-minutes 15 --yes`.
Remove: `sudo ./install.sh --uninstall` (your container is left as it is).

## Status

```
$ hytale-idle status
asleep: hytale starts on the next join attempt (last stop 2026-11-11T03:40:12Z)
```

The full state is in `/var/lib/hytale-idle/status.json`:

| Field | Meaning |
|---|---|
| `state` | `asleep`, `starting` or `running` |
| `last_start`, `last_stop`, `last_join_attempt` | UTC times |
| `up_since`, `received_in_window_bytes`, `players_likely`, `earliest_stop_in_minutes` | while running |

With `--status-http 127.0.0.1:8765` (the installer asks), the same JSON is served read-only at
`http://127.0.0.1:8765/status.json`, for dashboards, uptime monitors and AI agents that can't open a shell.
Bind it to localhost or a private address unless you want the world to see whether your server is up.

**For monitoring:** an `asleep` server is not an outage. Alert on `hytale-idle` itself not running
(`systemctl is-active hytale-idle`), not on the game port being closed while asleep.

## Settings

`hytale-idle --help` lists them. The useful ones:

| Option | Default | |
|---|---|---|
| `--container` | `hytale` | the container to watch |
| `--idle-minutes` | `15` | stop after this long with nobody playing |
| `--idle-kib` | `64` | "nobody playing" = less than this received in that window |
| `--stop-timeout` | `90` | seconds Hytale gets to save when stopping |
| `--status-file` | `/var/lib/hytale-idle/status.json` | `''` for none |
| `--status-http` | off | `HOST:PORT` to serve the status read-only |

To change them after installing, edit the `ExecStart` line in `/etc/systemd/system/hytale-idle.service`, then
`sudo systemctl daemon-reload && sudo systemctl restart hytale-idle`.

## Prior art, honestly

Nothing here is a new idea. [lazymc](https://github.com/timvisee/lazymc) and
[mc-router](https://github.com/itzg/mc-router) do this for Minecraft by speaking its protocol, and
[Lazytainer](https://github.com/vmorganp/Lazytainer) is a general Docker sleep/wake proxy that some Hytale
images already use. hytale-idle exists because it needs no change to an existing container and no proxy in the
traffic path, and because it reports a status that people and AI agents can read. If you already run
Lazytainer happily, keep it.

## Tests

`python3 tests/test_hytale_idle.py` (exit 0 = pass). It runs offline: port parsing, the stop rule, a real UDP
knock, the status file and the status endpoint.

## Support the project

If hytale-idle saves you a bigger VPS, you can sponsor it through GitHub Sponsors (the button on this repo).

## Licence

MIT. See [LICENSE](LICENSE).
