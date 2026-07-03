import asyncio
import json
import logging
import os
import sys
import time
from collections import deque
from datetime import datetime, timedelta

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

# Daily remote time-sync. When enabled, once per day at TIME_SYNC_AT (local
# HH:MM) the bot logs in to each configured repeater/room-server and sets its
# clock to the HA host's time. Devices are a JSON list of
# {pubkey, password, name?} (pubkey is the full 64-hex-char public key).
TIME_SYNC_ENABLED = os.environ.get("TIME_SYNC_ENABLED", "false").lower() == "true"
TIME_SYNC_AT = os.environ.get("TIME_SYNC_AT", "03:30").strip()


def _parse_time_sync_devices() -> list[dict]:
    """Parse TIME_SYNC_DEVICES, which bashio::config emits as one compact JSON
    object per line for a list-of-objects option (not a single JSON array)."""
    raw = os.environ.get("TIME_SYNC_DEVICES", "").strip()
    if not raw:
        return []
    devices: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            _LOGGER.error("TIME_SYNC_DEVICES has an invalid entry; skipping: %r", line)
            continue
        if isinstance(entry, list):
            devices.extend(entry)
        else:
            devices.append(entry)
    return devices


TIME_SYNC_DEVICES = _parse_time_sync_devices()

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


# Idempotency guard. MeshCore delivery is at-least-once: the firmware can
# re-deliver the same message, and we observed that replying from inside the
# receive handler (which runs concurrently with the library's auto-fetch drain
# loop) provokes the firmware to re-deliver the just-received message in a tight
# loop — producing endless duplicate replies. Tracking a stable per-message
# identity makes handling idempotent: the first re-delivery is ignored instead
# of triggering another send, which also breaks that feedback loop.
_SEEN_MAX = 256
_seen_keys: set = set()
_seen_order: deque = deque()


def already_handled(key: tuple) -> bool:
    """Return True if ``key`` was seen before; otherwise record it and return False.

    Keeps at most ``_SEEN_MAX`` recent keys so memory stays bounded on a
    long-running bot.
    """
    if key in _seen_keys:
        return True
    _seen_keys.add(key)
    _seen_order.append(key)
    if len(_seen_order) > _SEEN_MAX:
        _seen_keys.discard(_seen_order.popleft())
    return False


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


def seconds_until(hhmm: str) -> float:
    """Seconds from now until the next local-time occurrence of ``HH:MM``.

    Uses the container's local clock (HA Supervisor passes the system timezone
    to add-ons), so the trigger fires at the wall-clock time the user
    configured. The epoch we later push to the device is timezone-independent.
    """
    hh, mm = (int(x) for x in hhmm.split(":"))
    now = datetime.now()
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


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

    # Preload the contact list once. get_contact_by_* are then pure local
    # lookups, so the receive callbacks never do device I/O to resolve a
    # contact. (ensure_contacts only refetches when the cache is empty, so the
    # old per-message calls were no-ops after the first one anyway.)
    await mc.ensure_contacts()

    await mc.start_auto_message_fetching()

    # Outbound replies are funneled through this queue and sent by a single
    # worker task. The message-received callbacks never touch the device
    # themselves — they only decide *what* to say and enqueue it. This mirrors
    # BlorkoBot's design (its plugin returns a string and the host process does
    # the send) and keeps device commands out of the receive callbacks, which
    # run concurrently with meshcore's auto-fetch drain loop. Issuing sends from
    # inside those callbacks was the root cause of the duplicate-reply loop; the
    # already_handled() dedupe is the backstop.
    reply_queue: asyncio.Queue = asyncio.Queue()

    async def run_time_sync():
        """Log in to each configured device and set its clock to host time.

        Runs on the single reply worker so it is serialized against message
        replies — only one coroutine ever drives the serial link at a time,
        which is the rule the whole bot follows (see CLAUDE.md). Per-device
        failures are logged and skipped; one bad device never aborts the rest.
        """
        _LOGGER.info("Time sync: starting for %d device(s)", len(TIME_SYNC_DEVICES))
        for dev in TIME_SYNC_DEVICES:
            pubkey = (dev.get("pubkey") or "").strip()
            password = dev.get("password") or ""
            label = dev.get("name") or (pubkey[:12] if pubkey else "(no pubkey)")
            if not pubkey:
                _LOGGER.error("Time sync: device entry missing 'pubkey'; skipping")
                continue
            try:
                # Login requires the full 32-byte public key.
                login = await mc.commands.send_login_sync(pubkey, password)
                if login is None or login.type == EventType.ERROR:
                    _LOGGER.error("Time sync: login failed for %s", label)
                    continue

                # Address the command via the known contact (uses its routing
                # path) when we have one; otherwise fall back to the raw key.
                dest = mc.get_contact_by_key_prefix(pubkey) or pubkey
                epoch = int(time.time())
                result = await mc.commands.send_cmd(dest, f"time {epoch}")
                if result.type == EventType.ERROR:
                    _LOGGER.error(
                        "Time sync: set-time failed for %s: %s", label, result.payload
                    )
                else:
                    _LOGGER.info("Time sync: set %s clock to %d", label, epoch)

                await mc.commands.send_logout(pubkey)
            except Exception:
                _LOGGER.exception("Time sync: error syncing %s", label)
            # Let the mesh settle before the next device.
            await asyncio.sleep(2)
        _LOGGER.info("Time sync: done")

    async def reply_worker():
        while True:
            job = await reply_queue.get()
            try:
                kind = job[0]
                if kind == "timesync":
                    await run_time_sync()
                    continue
                _, target, text = job
                if kind == "chan":
                    result = await mc.commands.send_chan_msg(target, text)
                else:
                    result = await mc.commands.send_msg(target, text)
                if result.type == EventType.ERROR:
                    _LOGGER.error(
                        "Failed to send %s reply '%s': %s", kind, text, result.payload
                    )
                else:
                    _LOGGER.info("Sent %s reply: %s", kind, text)
            except Exception:
                _LOGGER.exception("Error handling job %r", job)
            finally:
                reply_queue.task_done()

    async def time_sync_scheduler():
        """Enqueue a time-sync job once per day at the configured local time."""
        while True:
            delay = seconds_until(TIME_SYNC_AT)
            _LOGGER.info(
                "Time sync: next run at %s (in %.0f min)", TIME_SYNC_AT, delay / 60
            )
            await asyncio.sleep(delay)
            reply_queue.put_nowait(("timesync",))
            # Guard against re-firing within the same minute after a short sleep.
            await asyncio.sleep(60)

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

        # Drop re-deliveries of a message we've already handled (see
        # already_handled). sender_timestamp + channel + text uniquely identifies
        # the original message across re-delivery.
        if already_handled(("chan", chan, msg.get("sender_timestamp"), text)):
            _LOGGER.debug("Ignoring duplicate channel message on %s: %s", chan, text)
            return

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
            reply_queue.put_nowait(("chan", channel_idx, reply))
            return

        # "!dm" — direct-message the sender with the message's path. The channel
        # message only carries the sender's display name, so we resolve it to a
        # contact (and thus a public key) to address the reply. Contacts were
        # preloaded at startup, so this lookup does no device I/O.
        if body_lower == CMD_DM:
            contact = mc.get_contact_by_name(sender)
            if not contact:
                _LOGGER.error(
                    "!dm: no contact found for sender '%s'; cannot send DM", sender
                )
                return
            reply = f"path {format_path(latest_chan_path, latest_chan_hash_size)}"
            reply_queue.put_nowait(("dm", contact, reply))
            return

        # Only respond when the message body starts with the trigger text
        # (case-insensitive). Matching the body — not the full "sender: text"
        # string — avoids false replies from sender names or mid-message hits.
        if not body_lower.startswith(TRIGGER_TEXT):
            return

        reply = f"@[{sender}] {latest_chan_hops} hops to {device_name}"
        reply_queue.put_nowait(("chan", channel_idx, reply))

    async def handle_contact_message(event):
        msg = event.payload or {}
        text = (msg.get("text") or "").strip()
        pubkey_prefix = msg.get("pubkey_prefix", "")

        # Drop re-deliveries of a message we've already handled (see
        # already_handled). sender_timestamp + sender + text uniquely identifies
        # the original message across re-delivery.
        if already_handled(("dm", pubkey_prefix, msg.get("sender_timestamp"), text)):
            _LOGGER.debug("Ignoring duplicate DM from %s: %s", pubkey_prefix, text)
            return

        _LOGGER.info("DM from %s: %s", pubkey_prefix, text)

        # Reply with the path only for the configured DM trigger text.
        if text.lower() != DM_TRIGGER_TEXT:
            return

        reply = f"path {format_path(latest_dm_path, latest_dm_hash_size)}"

        # send_msg accepts a contact dict or a public-key hex prefix. Prefer the
        # full contact when we have it (it carries the 32-byte key); otherwise
        # fall back to the 6-byte prefix from the received message. Contacts were
        # preloaded at startup, so this is a local lookup with no device I/O.
        dest = mc.get_contact_by_key_prefix(pubkey_prefix) or pubkey_prefix
        reply_queue.put_nowait(("dm", dest, reply))

    mc.subscribe(
        EventType.CHANNEL_MSG_RECV,
        handle_channel_message,
        attribute_filters={"channel_idx": channel_idx},
    )
    mc.subscribe(EventType.CONTACT_MSG_RECV, handle_contact_message)
    mc.subscribe(EventType.RX_LOG_DATA, handle_rx_log)

    worker_task = asyncio.create_task(reply_worker())

    scheduler_task = None
    if TIME_SYNC_ENABLED and TIME_SYNC_DEVICES:
        scheduler_task = asyncio.create_task(time_sync_scheduler())
        _LOGGER.info(
            "Daily time sync enabled at %s for %d device(s)",
            TIME_SYNC_AT,
            len(TIME_SYNC_DEVICES),
        )
    elif TIME_SYNC_ENABLED:
        _LOGGER.warning("Time sync enabled but no devices configured; skipping")

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
        worker_task.cancel()
        if scheduler_task is not None:
            scheduler_task.cancel()
        await mc.stop_auto_message_fetching()
        await mc.disconnect()
        _LOGGER.info("Disconnected")


if __name__ == "__main__":
    asyncio.run(main())
