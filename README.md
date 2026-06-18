# MeshCore Test Bot — Home Assistant Add-on

A Home Assistant add-on that monitors a [MeshCore](https://meshcore.co.nz) device over USB serial for messages on a configurable channel. When it sees a message containing the trigger text (default: `"test"`), it auto-replies with:

```
@<sender>: <N> hops from <device-name>
```

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
| `trigger_text` | `test` | Text to match in incoming messages (case-insensitive) |
| `device_name` | *(empty)* | Override the device name used in replies. If empty, auto-detected from device. |

Example `options` in the add-on UI:

```yaml
serial_port: /dev/serial/by-id/usb-RAKwireless_WisCore_RAK4631_Board_XXXX-if00
baudrate: 115200
channel_name: "#test"
channel_idx: 1
trigger_text: test
device_name: ""
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
- [`meshcore_py`](https://github.com/meshcore-dev/meshcore_py)

### Running locally

```bash
pip install meshcore_py
SERIAL_PORT=/dev/ttyUSB0 CHANNEL_IDX=1 TRIGGER_TEXT=test python3 meshcore_test_bot/bot.py
```

### Building the Docker image locally

```bash
docker build \
  --build-arg BUILD_FROM=python:3.11-slim \
  -t meshcore-test-bot \
  ./meshcore_test_bot
```

### Releasing a new version

1. Update `version:` in [`meshcore_test_bot/config.yaml`](meshcore_test_bot/config.yaml).
2. Commit and push.
3. Create a git tag matching the version: `git tag v1.0.1 && git push origin v1.0.1`.
4. GitHub Actions will build all four architectures and publish to GHCR automatically.
