# Changelog

## 0.2.0 (unreleased, not yet run on a real server)

- `hytale_update.py`: keeps the server on the latest Hytale while it sleeps. Backs up the world, installs the new
  server files (old ones kept), boots with every mod, and moves any mod that fails to load to `mods-disabled/`
  with a note saying why. Disabled mods are retried after every update and weekly; ones that load go back on.
  If the server will not start even with no mods, everything is rolled back and an alert file is written.
  Deprecation warnings in the log are reported as early warnings. Offline proof: `tests/test_hytale_update.py`.

## 0.1.0

- First public release. Starts a Dockerised Hytale server on a join attempt and stops it after a quiet spell,
  without parsing the game's protocol. Status via `hytale-idle status`, a status file and an optional read-only
  HTTP endpoint. Setup wizard with `--dry-run` and `--uninstall`.
- Ran on a private server from 2026-11-11 before release.
