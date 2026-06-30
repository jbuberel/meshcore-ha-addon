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
  single reply worker* (enqueued as a `("timesync",)` job), not from its own
  task, so admin commands stay serialized with message replies — the same
  one-coroutine-owns-the-link rule as everything else (see Lessons learned).
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
