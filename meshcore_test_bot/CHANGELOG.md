# Changelog

## 1.7.0

- New `!help` command: send it on the monitored channel or as a direct
  message and the bot replies with a one-line summary of its commands
  (trigger word, `!path`, `!dm`, `!help`, and the DM path trigger).

## 1.6.5

- Log lines now include timestamps. The timestamped format was already
  configured, but the meshcore library's own import-time `basicConfig()` call
  was silently overriding it, leaving Python's bare default format.

## 1.6.4

- Treat the firmware's "clock cannot go backwards" refusal as success when the
  measured skew shows the device is already in sync (within ±10 s). An
  accurate device always refuses a pushed epoch because mesh transit makes it
  stale on arrival; only a device that is genuinely ahead is a real failure.

## 1.6.3

- Report large clock skews in humanized form (minutes/hours/days) instead of
  huge raw second counts.
- When the "OK - clock set" confirmation is lost in transit, re-query the
  device clock and accept a residual skew ≤ 30 s as verified success.
- Silence the meshcore library's "please consider using send_login_sync"
  warning — login is driven manually by design.

## 1.6.2

- Fix MQTT discovery failing with 401: run `run.sh` under `with-contenv` so
  the s6-overlay-scrubbed environment still exposes `SUPERVISOR_TOKEN` to
  bashio's Supervisor API calls.

## 1.6.1

- Add `hassio_api: true` so `SUPERVISOR_TOKEN` is injected for the MQTT
  service discovery call.

## 1.6.0

- Report measured clock skew for each time-sync target and confirm the
  set-time by the device's own reply, not just MSG_SENT.
- Add manual `mqtt_host`/`mqtt_port`/`mqtt_user`/`mqtt_password` options for
  external brokers that the Supervisor services API can't see (it only knows
  the official Mosquitto add-on).

## 1.5.0

- Expose the manual time-sync trigger as a Home Assistant `button` entity
  (`button.meshcore_test_bot_sync_now`) via MQTT discovery, so it can be
  placed on dashboards and used in automations.

## 1.4.0

- Add an ingress web UI (sidebar panel) with a manual "Sync Now" button.
- Check the local contact list before attempting a remote login and report a
  clear "not a known contact" error instead of the opaque
  `ERR_CODE_NOT_FOUND`.
- Distinguish LOGIN_FAILED from an unreachable device when diagnosing
  time-sync login failures.
- Fix `time_sync_devices` parsing: bashio emits newline-delimited JSON, not a
  JSON array.

## 1.3.0

- Add daily remote time-sync: at `time_sync_at`, log in to each configured
  repeater/room-server and set its clock to the host's time via the firmware
  `time` CLI command.

## 1.2.1

- Fix a duplicate-reply loop on DM/channel triggers: dedupe re-delivered
  messages and funnel all sends through a single worker so replies never race
  the message-fetch loop on the serial link.

## 1.2.0

- Add path/hop-reporting commands (captured from `RX_LOG_DATA`) and support
  for direct-message triggers.

## 1.1.1

- Format channel replies with the `@[sender]` convention.
- Resolve the reply channel by name and exit with a clear error if it is not
  found.

## 1.1.0

- Fix serial device access: add `uart: true`, map `/dev/ttyACM0`, and default
  to the stable `/dev/serial/by-id` path.

## 1.0.2

- Fix the GHCR image name so installs pull the published packages.

## 1.0.1

- Fix arm builds (compile pycryptodome) and the pip install of the `meshcore`
  package.

## 1.0.0

- Initial release: monitor a MeshCore device over USB serial and auto-reply
  to channel messages.
