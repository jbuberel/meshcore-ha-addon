import asyncio
import logging
import os
import sys

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
# Text of a direct message that triggers a "path" reply (BlorkoBot-style).
DM_TRIGGER_TEXT = os.environ.get("DM_TRIGGER_TEXT", "test").lower()
DEVICE_NAME_OVERRIDE = os.environ.get("DEVICE_NAME", "")

# Channel commands. "!path" replies on the channel with the message path;
# "!dm" replies to the sender via a direct message with the same path.
CMD_PATH = "!path"
CMD_DM = "!dm"

# Fallback upper bound for the channel scan when the firmware does not report
# max_channels in its device info.
DEFAULT_MAX_CHANNELS = 32

# meshcore payload types (see PAYLOAD_TYPENAMES in meshcore_parser.py).
PAYLOAD_TYPE_TEXT_MSG = 2  # direct (1:1) text message
PAYLOAD_TYPE_GRP_TXT = 5  # channel / group text message

# The decoded CONTACT_MSG_RECV / CHANNEL_MSG_RECV events do not carry the path
# the packet travelled, so we capture it from the RX_LOG_DATA event that the
# firmware emits immediately before each decoded message. We keep the direct
# and channel state separate (keyed by payload type) so a DM reply uses the
# DM's path and a channel reply uses the channel message's path.
#
# All values come straight from meshcore's own packet parser (path,
# path_hash_size, path_len), rather than re-parsing the raw packet header
# ourselves: the header packs path_hash_size into the top two bits of the path
# byte and may carry a 4-byte transport code before it, both of which a naive
# fixed-offset parse gets wrong. ``path_len`` is meshcore's hop count.
latest_dm_path: str = ""
latest_dm_hash_size: int = 1
latest_dm_hops: int = 0
latest_chan_path: str = ""
latest_chan_hash_size: int = 1
latest_chan_hops: int = 0


def format_path(path_hex: str, hash_size: int = 1) -> str:
    """Render a raw path hex string as ``P1 → P2 → P3``.

    The MeshCore path is a hex string with ``hash_size`` bytes per hop, where
    each hop is a prefix of a repeater's public key. We split it into per-hop
    chunks and uppercase them, matching the node-prefix notation used by the
    MeshCore apps and BlorkoBot. A zero-hop (directly received) packet has no
    path bytes.
    """
    if not path_hex:
        return "direct (0 hops)"
    chars_per_hop = max(hash_size, 1) * 2
    hops = [
        path_hex[i : i + chars_per_hop].upper()
        for i in range(0, len(path_hex), chars_per_hop)
    ]
    return " → ".join(hops)


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
        global latest_dm_path, latest_dm_hash_size, latest_dm_hops
        global latest_chan_path, latest_chan_hash_size, latest_chan_hops
        rx = event.payload or {}

        # meshcore has already parsed the packet for us; keep the most recent
        # path/hop-count per message type so the next decoded message can report
        # the route it took. path_len is meshcore's hop count (number of hops).
        payload_type = rx.get("payload_type")
        path = rx.get("path", "") or ""
        hash_size = rx.get("path_hash_size", 1) or 1
        hops = rx.get("path_len", 0) or 0
        if payload_type == PAYLOAD_TYPE_TEXT_MSG:
            latest_dm_path, latest_dm_hash_size, latest_dm_hops = path, hash_size, hops
            _LOGGER.debug("Updated DM path: %s (%d hops)", path or "(direct)", hops)
        elif payload_type == PAYLOAD_TYPE_GRP_TXT:
            latest_chan_path, latest_chan_hash_size, latest_chan_hops = path, hash_size, hops
            _LOGGER.debug("Updated channel path: %s (%d hops)", path or "(direct)", hops)

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

        body_lower = body.lower()

        # "!path" — reply on the channel with the path the message travelled.
        if body_lower == CMD_PATH:
            reply = f"path {format_path(latest_chan_path, latest_chan_hash_size)}"
            _LOGGER.info("Sending channel path reply: %s", reply)
            result = await mc.commands.send_chan_msg(channel_idx, reply)
            if result.type == EventType.ERROR:
                _LOGGER.error("Failed to send path reply: %s", result.payload)
            else:
                _LOGGER.info("Path reply sent successfully")
            return

        # "!dm" — direct-message the sender with the message's path. The channel
        # message only carries the sender's display name, so we resolve it to a
        # contact (and thus a public key) to address the reply.
        if body_lower == CMD_DM:
            await mc.ensure_contacts()
            contact = mc.get_contact_by_name(sender)
            if not contact:
                _LOGGER.error(
                    "!dm: no contact found for sender '%s'; cannot send DM", sender
                )
                return
            reply = f"path {format_path(latest_chan_path, latest_chan_hash_size)}"
            _LOGGER.info("Sending DM to %s: %s", sender, reply)
            result = await mc.commands.send_msg(contact, reply)
            if result.type == EventType.ERROR:
                _LOGGER.error("Failed to send DM: %s", result.payload)
            else:
                _LOGGER.info("DM sent successfully")
            return

        # Only respond when the message body starts with the trigger text
        # (case-insensitive). Matching the body — not the full "sender: text"
        # string — avoids false replies from sender names or mid-message hits.
        if not body_lower.startswith(TRIGGER_TEXT):
            return

        reply = f"@[{sender}] {latest_chan_hops} hops to {device_name}"
        _LOGGER.info("Sending reply: %s", reply)

        result = await mc.commands.send_chan_msg(channel_idx, reply)
        if result.type == EventType.ERROR:
            _LOGGER.error("Failed to send reply: %s", result.payload)
        else:
            _LOGGER.info("Reply sent successfully")

    async def handle_contact_message(event):
        msg = event.payload or {}
        text = (msg.get("text") or "").strip()
        pubkey_prefix = msg.get("pubkey_prefix", "")
        _LOGGER.info("DM from %s: %s", pubkey_prefix, text)

        # Reply with the path only for the configured DM trigger text.
        if text.lower() != DM_TRIGGER_TEXT:
            return

        reply = f"path {format_path(latest_dm_path, latest_dm_hash_size)}"

        # send_msg accepts a contact dict or a public-key hex prefix. Prefer the
        # full contact when we have it (it carries the 32-byte key); otherwise
        # fall back to the 6-byte prefix from the received message.
        await mc.ensure_contacts()
        dest = mc.get_contact_by_key_prefix(pubkey_prefix) or pubkey_prefix
        _LOGGER.info("Sending DM reply to %s: %s", pubkey_prefix, reply)
        result = await mc.commands.send_msg(dest, reply)
        if result.type == EventType.ERROR:
            _LOGGER.error("Failed to send DM reply: %s", result.payload)
        else:
            _LOGGER.info("DM reply sent successfully")

    mc.subscribe(
        EventType.CHANNEL_MSG_RECV,
        handle_channel_message,
        attribute_filters={"channel_idx": channel_idx},
    )
    mc.subscribe(EventType.CONTACT_MSG_RECV, handle_contact_message)
    mc.subscribe(EventType.RX_LOG_DATA, handle_rx_log)

    _LOGGER.info(
        "Listening on channel %d for '%s'; channel commands '%s'/'%s'; "
        "DM trigger '%s'",
        channel_idx,
        TRIGGER_TEXT,
        CMD_PATH,
        CMD_DM,
        DM_TRIGGER_TEXT,
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
