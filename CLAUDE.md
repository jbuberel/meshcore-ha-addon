# CLAUDE.md

Guidance for working in this repo. The add-on is a Home Assistant add-on
(`meshcore_test_bot/`) that monitors a MeshCore device over USB serial and
auto-replies to channel and direct messages.

## Layout

- `meshcore_test_bot/bot.py` — the bot. Connects via the `meshcore` Python
  library, subscribes to message events, and replies. Also runs an optional
  daily remote time-sync: at `time_sync_at` it logs in to each configured
  repeater/room-server (`time_sync_devices`) and sends the firmware CLI
  `time <epoch>` to set its clock to the host's time. The sync runs *on the
  single reply worker* (enqueued via `enqueue_time_sync()` as a `("timesync",)`
  job), not from its own task, so admin commands stay serialized with message
  replies — the same one-coroutine-owns-the-link rule as everything else (see
  Lessons learned). An `aiohttp` server (started in `main()`, routes in
  `handle_web_*`) exposes this over HA ingress (`ingress`/`ingress_port` in
  `config.yaml`, no `ports:` mapping) as a sidebar panel with a manual
  "Sync Now" button — same `enqueue_time_sync()` path, so it can't race the
  scheduler. No `panel_admin` flag, so it's intentionally usable by any HA
  dashboard user, not just admins. The same trigger is also exposed as an HA
  `button` entity (`button.meshcore_test_bot_sync_now`) via MQTT discovery
  (`mqtt_button_task()` — the Zigbee2MQTT-Restart-button pattern): `run.sh`
  exports `MQTT_*` env vars from the `mqtt_host`/`mqtt_port`/`mqtt_user`/
  `mqtt_password` options when set, otherwise from the Supervisor services
  API (`services: mqtt:want` in `config.yaml` grants the endpoint;
  `hassio_api: true` is the documented flag for Supervisor API use; `want`
  keeps the broker optional). Two hard-won gotchas here: (1) the Supervisor
  injects `SUPERVISOR_TOKEN` into the container unconditionally, but the
  base image's s6-overlay v3 scrubs the environment for scripts, so run.sh
  **must** use the `#!/usr/bin/with-contenv bashio` shebang or every bashio
  Supervisor API call fails 401 with an empty token and discovery always
  reports no broker; (2) bashio runs with nounset, so any direct
  `${SUPERVISOR_TOKEN}` reference needs a `:-` default. The manual options
  exist because the services API is *only*
  populated by the official Mosquitto add-on — an external or containerized
  broker is invisible to it, so `bashio::services.available "mqtt"` returns
  false even when MQTT is otherwise fully working (the run.sh else-branch
  logs the raw `/services/mqtt` response for diagnosis). A retained config on
  `homeassistant/button/.../config`
  creates the entity, and a press publishes to a command topic whose handler
  only calls `enqueue_time_sync()` — never the device. With no broker the
  task isn't started; with a broker but no `time_sync_devices` it clears the
  retained discovery topic so no dead button lingers on dashboards. `aiomqtt`
  and its `paho-mqtt` dependency ship pure-Python wheels, so the Dockerfile
  needed no new apt packages for the arm builds (unlike pycryptodome).
  Before logging in to each device, `run_time_sync()` checks
  `mc.get_contact_by_key_prefix()` itself and skips
  with a specific message if the pubkey isn't a known contact — the companion
  radio would otherwise reject the login with `ERR_CODE_NOT_FOUND` anyway,
  since the login command never reaches the mesh if the local contact/routing
  table doesn't have that key. It also calls `mc.ensure_contacts(follow=True)`
  at the start of every run, since meshcore only marks the contact cache dirty
  on a new advertisement/path update — it never auto-refetches — so without
  this a device that starts advertising after the bot boots would otherwise
  stay invisible until a restart. Replies to firmware CLI commands arrive as
  ordinary `CONTACT_MSG_RECV` events with `txt_type == TXT_TYPE_CLI_DATA` (1);
  `send_cli_and_wait_reply()` matches them by pubkey prefix + txt_type, and
  `handle_contact_message` ignores them so they're never treated as DMs. The
  reported clock skew comes from the reply's `sender_timestamp` (the device's
  own clock when it sent the reply, second resolution) — *not* from parsing
  the "clock" reply text, which is only minute-resolution. Sync success is
  judged by the device's reply to `time <epoch>` ("OK - clock set: …"), not
  by MSG_SENT; the firmware refuses to set a clock backwards ("(ERR: clock
  cannot go backwards)"), so a device running ahead is reported as refused.
  CLI replies are sent once, unacknowledged — when the `time` confirmation
  is lost, `run_time_sync()` re-queries "clock" and accepts a residual skew
  ≤ 30 s as verified success. The meshcore library's "please consider using
  send_login_sync" warning is intentionally filtered out (`_DropLoginNag`):
  we drive login manually precisely because the sync helper can't
  distinguish LOGIN_FAILED from an unreachable device.
- `meshcore_test_bot/config.yaml` — HA add-on manifest. **`version:` here drives
  releases** (see Releasing).
- `meshcore_test_bot/run.sh` — bashio entrypoint; exports each `config.yaml`
  option as an env var that `bot.py` reads.
- `.github/workflows/build.yml` — builds/pushes multi-arch images to GHCR on push
  to `main`; the `release` job runs only on `v*` tags.

## Releasing

Releases are **fully automated from `config.yaml`** — there is no manual
`git tag` step.

1. Bump `version:` in `meshcore_test_bot/config.yaml` (semver: patch for fixes,
   minor for features) and push to `main` / merge a PR. You **must** bump it —
   HA only pulls a new image when the version changes.
2. The `build` job builds and pushes multi-arch images to GHCR.
3. The `release` ("Tag and Release") job reads that version and, if no matching
   `vX.Y.Z` tag exists yet, uses `softprops/action-gh-release` to create **both**
   the tag (at the merge commit) and a GitHub Release with generated notes.
   Pushing `main` without bumping the version is a safe no-op — the tag-exists
   check skips the release step.

Notes / gotchas:

- Tags are an **output** of the `main` build, not a trigger. The workflow has no
  `tags: v*` trigger — don't add one, and don't push tags manually (it would
  desync the tag from the automated flow).
- Tags created with `GITHUB_TOKEN` do not re-trigger workflows, so there is no
  build loop.
- The workflow only acts on the *current* `config.yaml` version; it never
  backfills tags for versions that were merged before this automation existed.

## Lessons learned

### The `meshcore` library is not concurrency-safe per command; never send from inside a receive callback

`start_auto_message_fetching()` runs a background loop that calls `get_msg()`
every 0.1s until the device returns `NO_MORE_MSGS`. Incoming messages are
dispatched to subscribed callbacks as **concurrent background tasks**. The
command layer (`commands/base.py::send`) correlates responses **by event type
only** — there is no per-request sequence number and **no shared lock** across
concurrent `send()` calls.

Consequence: if a receive callback issues its own device command
(`send_msg`, `send_chan_msg`, `ensure_contacts`, …), that command runs
concurrently with the auto-fetch drain loop and the two race on the single
serial connection. Observed symptom: the drain loop never reaches
`NO_MORE_MSGS`, the firmware keeps re-delivering the just-received message, and
the bot replies to it ~10×/second forever (the 0.1s cadence in the logs is the
fetch loop, not over-the-air retransmits). `ensure_contacts()` (a multi-frame
`get_contacts`) inside a callback widens the race window dramatically.

**Rule: receive callbacks decide *what* to reply; a single worker task does the
sending.** `bot.py` funnels replies through an `asyncio.Queue` drained by one
`reply_worker`. Callbacks only enqueue (`put_nowait`) — they never touch the
device. Preload contacts once at startup so `get_contact_by_*` are pure local
lookups inside callbacks. This mirrors BlorkoBot
(<https://github.com/statico/blorkobot>): its plugin returns a string and the
host process (Remote Terminal for MeshCore) owns all device I/O.

### MeshCore delivery is at-least-once — handlers must be idempotent

The firmware can re-deliver the same message. Always dedupe before acting, keyed
on a stable per-message identity: `sender_timestamp` + sender
(`pubkey_prefix` / `channel_idx`) + `text`. See `already_handled()` in `bot.py`.
This both stops duplicate replies and breaks any re-delivery feedback loop (the
first re-delivery is ignored instead of triggering another send). BlorkoBot does
the equivalent with a monotonic `after_id` message cursor and a persisted
seen-ID set.

### Inspecting the meshcore library

It is not committed here. To read its source: `python3 -m pip download meshcore
--no-deps`, then `unzip` the wheel. Key files: `meshcore.py`
(`start_auto_message_fetching`), `commands/base.py` (`send`/correlation),
`commands/messaging.py` (`get_msg`/`send_msg`), `reader.py` (packet parsing —
`path`, `path_len`, `path_hash_size`, `sender_timestamp`), `events.py`
(dispatcher; async callbacks run as background tasks).

### Path/hop reporting

Decoded `CONTACT_MSG_RECV` / `CHANNEL_MSG_RECV` events do **not** carry the
packet path. Capture it from the `RX_LOG_DATA` event the firmware emits just
before each decoded message, using meshcore's own parsed fields (`path`,
`path_hash_size`, `path_len`) rather than re-parsing the raw header (the header
packs `path_hash_size` into the top bits of the path byte and may carry a 4-byte
transport prefix). Keep DM and channel path state separate.
