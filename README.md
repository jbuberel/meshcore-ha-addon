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
| `serial_port` | `/dev/ttyUSB0` | Serial device path for the MeshCore USB connection |
| `baudrate` | `115200` | Serial baud rate |
| `channel_idx` | `1` | Channel index to monitor (0-based; find yours in the MeshCore app) |
| `trigger_text` | `test` | Text to match in incoming messages (case-insensitive) |
| `device_name` | *(empty)* | Override the device name used in replies. If empty, auto-detected from device. |

Example `options` in the add-on UI:

```yaml
serial_port: /dev/ttyUSB0
baudrate: 115200
channel_idx: 1
trigger_text: test
device_name: ""
```

## Finding your channel index

The `channel_idx` is zero-based and matches the order channels appear in the MeshCore app. Channel `#test` is typically index `1` if it's the second channel listed.

## USB serial device access

The add-on requests access to `/dev/ttyUSB0` by default. If your device appears at a different path (e.g. `/dev/ttyACM0`), update `serial_port` in the add-on config and restart.

To verify the device path in HA, check **Settings → System → Hardware** or use the SSH add-on to run `ls /dev/tty*`.

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
