# MeshCore Test Bot — Home Assistant Add-on

A Home Assistant add-on that monitors a [MeshCore](https://meshcore.co.nz) device over USB serial and auto-replies to a few simple commands.

When it sees a channel message containing the trigger text (default: `"test"`), it replies with:

```
@<sender>: <N> hops from <device-name>
```

### Path commands

Inspired by [BlorkoBot](https://github.com/statico/blorkobot), the bot also reports the path a message travelled through the mesh, formatted as the node prefix of each hop:

```
path P1 → P2 → P3
```

where each `Pn` is the one-byte public-key prefix (uppercase hex) of a repeater in the path. A directly received message (zero hops) shows `path direct (0 hops)`.

| Trigger | Where | Response |
|---|---|---|
| `test` (the `dm_trigger_text`) | **Direct message** to the device | A **direct message** reply with the DM's path |
| `!path` | On the monitored channel (e.g. `#test`) | A **channel** reply with the message's path |
| `!dm` | On the monitored channel (e.g. `#test`) | A **direct message** to the sender with the message's path |

> The path is read from the device's RX log for the most recently received message of the matching type (direct vs. channel), since the decoded message events do not themselves carry the path. The `!dm` command resolves the sender's display name to a contact in order to address the reply, so the sender must be a known contact on the device.

## Installation

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**.
2. Click the three-dot menu (⋮) in the top-right and choose **Repositories**.
3. Add this URL:
   ```
   https://github.com/jbuberel/meshcore-ha-addon
   ```
4. Find **MeshCore Test Bot** in the store and click **Install**.

## Configuration

| Option | Default | Description |
|---|---|---|
| `serial_port` | `/dev/serial/by-id/usb-RAKwireless_...-if00` | Serial device path. The stable `/dev/serial/by-id/...` path is recommended over `/dev/ttyACMx`, which can renumber across reboots. |
| `baudrate` | `115200` | Serial baud rate |
| `channel_name` | `#test` | Channel name to monitor. The add-on queries the device at startup and uses the matching channel's index automatically. If no channel with this name is found, the add-on logs the channels it did find and exits. Leave empty to use `channel_idx` instead. |
| `channel_idx` | `1` | Channel index (0-based), used only when `channel_name` is empty. |
| `trigger_text` | `test` | Channel-message text that triggers the `@<sender>: N hops` reply (case-insensitive) |
| `dm_trigger_text` | `test` | Direct-message text that triggers a `path …` direct-message reply (case-insensitive) |
| `device_name` | *(empty)* | Override the device name used in replies. If empty, auto-detected from device. |
| `time_sync_enabled` | `false` | Enable the daily remote clock sync (see [Remote time sync](#remote-time-sync)). |
| `time_sync_at` | `03:30` | Local time (24-hour `HH:MM`) at which the daily sync runs. |
| `time_sync_devices` | *(empty list)* | Repeaters/room-servers to sync. Each entry has a `pubkey`, `password`, and optional `name`. |
| `mqtt_host` | *(empty)* | MQTT broker host for the dashboard button (see [Dashboard button](#dashboard-button-mqtt)). Leave empty to auto-discover the official Mosquitto add-on; set it only for any other broker. |
| `mqtt_port` | `1883` | MQTT broker port, used only when `mqtt_host` is set. |
| `mqtt_user` | *(empty)* | MQTT username, used only when `mqtt_host` is set. Leave empty for anonymous. |
| `mqtt_password` | *(empty)* | MQTT password, used only when `mqtt_host` is set. |

Example `options` in the add-on UI:

```yaml
serial_port: /dev/serial/by-id/usb-RAKwireless_WisCore_RAK4631_Board_XXXX-if00
baudrate: 115200
channel_name: "#test"
channel_idx: 1
trigger_text: test
dm_trigger_text: test
device_name: ""
time_sync_enabled: true
time_sync_at: "03:30"
time_sync_devices:
  - name: Repeater North
    pubkey: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
    password: secret-admin-password
  - name: Room Server
    pubkey: fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210
    password: another-password
```

## Channel selection

By default the add-on resolves the channel by **name**: at startup it queries the
device for its channels and uses the index of the channel named `channel_name`
(default `#test`). This means you no longer need to know the numeric index — just
the channel name as it appears on the device.

If no channel with that name is found on the device, the add-on logs an error
listing every channel name it found and exits, so you can correct the
`channel_name` value.

If `channel_name` is left empty, the add-on instead uses the static
`channel_idx` (zero-based, matching the order channels appear in the MeshCore
app).

## Remote time sync

Repeaters and room-servers keep their own clocks, which drift over time. When
`time_sync_enabled` is `true`, the add-on runs a **daily** clock sync: once per
day at `time_sync_at` (your HA server's local time) it connects to each device
in `time_sync_devices`, one at a time, and sets its clock to the Home Assistant
host's current time.

For each device it logs in with the admin password, measures the device's
clock skew, issues the firmware CLI command
[`time <epoch>`](https://docs.meshcore.io/cli_commands/#set-the-time-to-a-specific-timestamp),
and logs out. The time pushed is UTC epoch seconds, so it is correct regardless
of your server's timezone. Devices are synced sequentially with a short pause
between each. If a device can't be reached or the login fails, the error is
logged and the add-on moves on to the next one.

A successful sync reports the skew and the time that was set, e.g.
`Skew: -42 seconds, set time: 2026-07-05 12:32:48` (negative skew = the
device's clock was behind your HA server; the time shown is your server's
local time; skews too large to read in seconds get an approximation, e.g.
`(~781 days behind)` for a repeater that reverted to its firmware-build
date). Success means the device *confirmed* the change — the add-on waits
for the firmware's reply rather than assuming a sent command worked. CLI
confirmations travel the mesh unacknowledged, so if one is lost the add-on
re-queries the device's clock and still reports success when the clock
verifiably matches (marked `confirmation lost; verified via clock query`).
One quirk to know about: the firmware refuses to move a clock **backwards**,
so a device running *ahead* reports
`device refused: (ERR: clock cannot go backwards)`. Such a device will come
back into sync naturally once its clock is no longer ahead (or after a
restart resets its clock).

Each entry in `time_sync_devices` has:

| Field | Required | Description |
|---|---|---|
| `pubkey` | yes | The device's **full public key** — 64 hex characters (32 bytes). A short 6-byte prefix will **not** work; the login requires the full key. |
| `password` | yes | The device's remote-admin password. |
| `name` | no | A friendly label used only in the add-on log. Defaults to the start of the pubkey. |

```yaml
time_sync_enabled: true
time_sync_at: "03:30"
time_sync_devices:
  - name: Repeater North
    pubkey: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
    password: secret-admin-password
```

**Finding a device's public key:** in the MeshCore app, open the repeater or
room-server contact and copy its full public key. The device must already be a
known contact on the bot's companion radio so the sync command can be routed
to it — the add-on checks this itself before attempting a login, and logs
`not a known contact on this companion radio` (rather than trying and getting
a slower, less clear failure back from the device) if it isn't. If you see
that, either the device hasn't advertised to this companion radio yet, or the
configured `pubkey` doesn't match its actual key (e.g. it was copied before a
reflash, which generates a new key).

> The sync shares the same single command channel as the message auto-replies,
> so admin commands and replies never overlap on the serial link. A sync of
> several devices takes a few seconds; replies simply queue behind it.

### Dashboard button (MQTT)

If you have an MQTT broker set up (plus the MQTT integration in Home
Assistant), the add-on publishes a **Sync Now** `button` entity via MQTT
discovery, the same way Zigbee2MQTT provides its *Restart* button. It appears
under a **MeshCore Test Bot** device in **Settings → Devices & Services →
MQTT**, and you can add it to any dashboard as a button/tile card or use it
in automations (`button.press` on `button.meshcore_test_bot_sync_now`).

How the add-on finds the broker:

- **Official [Mosquitto broker](https://github.com/home-assistant/addons/tree/master/mosquitto)
  add-on:** nothing to configure — the add-on gets the host and credentials
  from the Supervisor automatically. Only this add-on registers itself with
  the Supervisor's MQTT service registry, so auto-discovery finds *only* it.
- **Any other broker** (external Mosquitto, a broker in a plain Docker
  container, etc.): set the `mqtt_host` / `mqtt_port` / `mqtt_user` /
  `mqtt_password` options. When `mqtt_host` is set, it always wins over
  auto-discovery.

**Where do MQTT credentials come from?** The auto-discovered credentials are
generated internally by the Mosquitto add-on — there is nothing to look up.
If you need to fill in `mqtt_user`/`mqtt_password` manually, the Mosquitto
add-on accepts either of these:

- any **Home Assistant user account** — the common approach is a dedicated,
  non-administrator user (e.g. `mqtt-bot`) created under **Settings → People
  → Users**, so MQTT access isn't tied to a person's login;
- a broker-local account defined in the Mosquitto add-on's **Configuration**
  tab under `logins:` (a list of `username`/`password` pairs).

For the Mosquitto add-on, `mqtt_host` is `core-mosquitto`; for external
brokers use their hostname/IP and whatever accounts that broker defines.

If neither is available (or no `time_sync_devices` are configured) the
entity simply isn't published; the add-on log explains what was tried. The
button shows *unavailable* while the add-on is stopped. Pressing it while a
sync is already running is a no-op, same as the panel button below.

### Manual trigger (sidebar panel)

The add-on also has its own entry in the Home Assistant sidebar (an *ingress*
panel) with a **Sync Now** button that runs the same sync immediately,
independent of the daily schedule — handy for testing your `time_sync_devices`
config or re-running after fixing a password, without waiting for
`time_sync_at`. The page updates itself with a per-device OK/FAILED result and
the failure detail once the run finishes.

This panel is visible to **any signed-in Home Assistant user with dashboard
access**, not just admins — there's no separate login or token, since it rides
on your existing Home Assistant session the same way any other add-on panel
does. It's never exposed on your LAN directly: Home Assistant's ingress proxy
is the only way to reach it.

Clicking the button while a sync is already running (from the schedule or
another click) is a no-op — the add-on refuses to double-queue a run and the
button disables itself until the current one finishes.

## USB serial device access

The add-on uses the `uart: true` flag, which grants the container the correct
cgroup device permissions for serial/UART hardware. Without it (or without the
device mapped under `devices:`), opening the port fails with
`Operation not permitted` (EPERM) even though the device node is visible.

To find your device's stable path in HA, go to **Settings → System → Hardware →
(⋮) All Hardware** and look for your MeshCore board (e.g.
`RAKwireless_WisCore_RAK4631`). Use its `/dev/serial/by-id/...` path as
`serial_port` — it survives reboots and replugs, unlike `/dev/ttyACMx`.

## Development

### Prerequisites
- Python 3.11+
- The Python dependencies in [`meshcore_test_bot/requirements.txt`](meshcore_test_bot/requirements.txt)
  ([`meshcore`](https://github.com/meshcore-dev/meshcore_py), `aiohttp`, `aiomqtt`)

### Running locally

```bash
pip install -r meshcore_test_bot/requirements.txt
SERIAL_PORT=/dev/ttyUSB0 CHANNEL_IDX=1 TRIGGER_TEXT=test python3 meshcore_test_bot/bot.py
```

Inside Home Assistant, `run.sh` fills in the MQTT broker credentials from the
Supervisor. For a local run, leave `MQTT_HOST` unset to skip the MQTT button
entirely, or point it at a broker yourself (`MQTT_HOST`, `MQTT_PORT`,
`MQTT_USER`, `MQTT_PASSWORD`).

### Building the Docker image locally

```bash
docker build \
  --build-arg BUILD_FROM=python:3.11-slim \
  -t meshcore-test-bot \
  ./meshcore_test_bot
```

### Releasing a new version

Releases are **fully automated from `config.yaml`** — there is no manual
`git tag` step.

1. Bump `version:` in [`meshcore_test_bot/config.yaml`](meshcore_test_bot/config.yaml)
   (semver: patch for fixes, minor for features). You **must** bump it — Home
   Assistant only pulls a new image when the version changes.
2. Commit and push to `main` (or merge a PR).
3. GitHub Actions builds all four architectures and publishes to GHCR, then
   reads the version and, if no matching `vX.Y.Z` tag exists yet, creates both
   the tag and a GitHub Release with generated notes. Pushing without bumping
   the version is a safe no-op.
