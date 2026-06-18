import asyncio
import logging
import os
import sys
from typing import Any

from meshcore import MeshCore, EventType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
_LOGGER = logging.getLogger("meshcore_test_bot")

SERIAL_PORT = os.environ.get("SERIAL_PORT", "/dev/ttyUSB0")
BAUDRATE = int(os.environ.get("BAUDRATE", "115200"))
CHANNEL_IDX = int(os.environ.get("CHANNEL_IDX", "1"))
CHANNEL_NAME = os.environ.get("CHANNEL_NAME", "#test").strip()
TRIGGER_TEXT = os.environ.get("TRIGGER_TEXT", "test").lower()
DEVICE_NAME_OVERRIDE = os.environ.get("DEVICE_NAME", "")

# Fallback upper bound for the channel scan when the firmware does not report
# max_channels in its device info.
DEFAULT_MAX_CHANNELS = 32

latest_hop_count: int | None = None


def parse_rx_log_data(payload: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        hex_str = None
        if isinstance(payload, dict):
            hex_str = payload.get("payload") or payload.get("raw_hex")
        elif isinstance(payload, (str, bytes)):
            hex_str = payload

        if not hex_str:
            return result

        if isinstance(hex_str, bytes):
            hex_str = hex_str.hex()

        hex_str = str(hex_str).lower().replace(" ", "").replace("\n", "").replace("\r", "")

        if len(hex_str) < 4:
            return result

        result["header"] = hex_str[0:2]
        try:
            path_len = int(hex_str[2:4], 16)
            result["path_len"] = path_len
        except ValueError:
            return {}

    except Exception as ex:
        _LOGGER.debug("Error parsing RX_LOG data: %s", ex)

    return result


async def resolve_channel_index(mc: MeshCore, name: str) -> int | None:
    """Find the index of the channel named ``name`` by querying the device.

    MeshCore has no "list channels" command, so we walk the channel slots and
    read each one's name via ``get_channel``. Returns the matching index, or
    ``None`` if no channel with that name exists, after logging an error that
    lists every channel name that was found.
    """
    target = name.strip().lower()
    info = mc.self_info or {}
    max_channels = info.get("max_channels") or DEFAULT_MAX_CHANNELS

    found: list[tuple[int, str]] = []

    _LOGGER.info("Scanning up to %d channel(s) for '%s'", max_channels, name)
    for idx in range(max_channels):
        result = await mc.commands.get_channel(idx)
        if result.type == EventType.ERROR:
            # Unused/unsupported slot — skip it.
            continue

        payload = result.payload or {}
        chan_name = (payload.get("channel_name") or "").strip()
        if not chan_name:
            continue

        _LOGGER.debug("Channel %d: %s", idx, chan_name)
        found.append((idx, chan_name))
        if chan_name.lower() == target:
            _LOGGER.info("Found channel '%s' at index %d", chan_name, idx)
            return idx

    if found:
        channel_list = ", ".join(f"[{idx}] {chan_name}" for idx, chan_name in found)
    else:
        channel_list = "(none)"
    _LOGGER.error(
        "No channel named '%s' found on the device. Channels found: %s",
        name,
        channel_list,
    )
    return None


async def main():
    global latest_hop_count

    _LOGGER.info("Connecting to MeshCore device on %s at %d baud", SERIAL_PORT, BAUDRATE)
    mc = await MeshCore.create_serial(SERIAL_PORT, BAUDRATE)
    _LOGGER.info("Connected")

    device_name = DEVICE_NAME_OVERRIDE
    if not device_name:
        info = mc.self_info or {}
        # self_info is a dict; common field names observed in meshcore firmware
        device_name = (
            info.get("adv_name")
            or info.get("name")
            or info.get("node_name")
            or "MeshCore"
        )
    _LOGGER.info("Device name: %s", device_name)

    # Resolve the channel by name when configured; otherwise use the static index.
    if CHANNEL_NAME:
        channel_idx = await resolve_channel_index(mc, CHANNEL_NAME)
        if channel_idx is None:
            # Named channel not found — shut down cleanly and exit with an error.
            await mc.disconnect()
            _LOGGER.info("Disconnected")
            sys.exit(1)
    else:
        channel_idx = CHANNEL_IDX

    await mc.start_auto_message_fetching()

    async def handle_rx_log(event):
        global latest_hop_count
        rx = event.payload or {}
        raw = rx.get("payload")
        if not raw:
            return
        parsed = parse_rx_log_data(raw)
        hop_count = parsed.get("path_len")
        if hop_count is not None:
            latest_hop_count = hop_count
            _LOGGER.debug("Updated hop count: %d", hop_count)

    async def handle_channel_message(event):
        msg = event.payload or {}
        text = msg.get("text", "")
        chan = msg.get("channel_idx")

        # Extract sender and body: meshcore channel messages are formatted
        # as "sender: text".
        if ":" in text:
            sender, body = text.split(":", 1)
            sender, body = sender.strip(), body.strip()
        else:
            sender, body = "unknown", text.strip()

        _LOGGER.info("Channel %s | %s: %s", chan, sender, text)

        # Only respond when the message body starts with the trigger text
        # (case-insensitive). Matching the body — not the full "sender: text"
        # string — avoids false replies from sender names or mid-message hits.
        if not body.lower().startswith(TRIGGER_TEXT):
            return

        hops = latest_hop_count if latest_hop_count is not None else 0
        reply = f"@[{sender}] {hops} hops to {device_name}"
        _LOGGER.info("Sending reply: %s", reply)

        result = await mc.commands.send_chan_msg(channel_idx, reply)
        if result.type == EventType.ERROR:
            _LOGGER.error("Failed to send reply: %s", result.payload)
        else:
            _LOGGER.info("Reply sent successfully")

    mc.subscribe(
        EventType.CHANNEL_MSG_RECV,
        handle_channel_message,
        attribute_filters={"channel_idx": channel_idx},
    )
    mc.subscribe(EventType.RX_LOG_DATA, handle_rx_log)

    _LOGGER.info(
        "Listening on channel %d for messages containing '%s'", channel_idx, TRIGGER_TEXT
    )

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        _LOGGER.info("Shutting down")
    finally:
        await mc.stop_auto_message_fetching()
        await mc.disconnect()
        _LOGGER.info("Disconnected")


if __name__ == "__main__":
    asyncio.run(main())
