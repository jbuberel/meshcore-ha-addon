import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from collections import deque
from datetime import datetime, timedelta

import aiomqtt
from aiohttp import web
from meshcore import MeshCore, EventType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
_LOGGER = logging.getLogger("meshcore_test_bot")


class _DropLoginNag(logging.Filter):
    """Drop meshcore's "please consider using send_login_sync" warning.

    We use send_login deliberately: send_login_sync only waits for
    LOGIN_SUCCESS, which makes a wrong password indistinguishable from an
    unreachable device (see wait_for_login_outcome). The library's nag would
    otherwise appear on every sync run and read like a problem.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "send_login_sync" not in record.getMessage()


logging.getLogger("meshcore").addFilter(_DropLoginNag())

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

# Must match `ingress_port:` in config.yaml. Ingress-only (no `ports:` entry),
# so this is reachable only via HA's authenticated ingress proxy, never
# directly on the LAN.
WEB_PORT = 8099

# MQTT broker credentials, exported by run.sh from the Supervisor services API
# when a broker add-on (e.g. Mosquitto) is installed. Empty MQTT_HOST means no
# broker — the dashboard button entity is simply not published.
MQTT_HOST = os.environ.get("MQTT_HOST", "").strip()
MQTT_PORT = int(os.environ.get("MQTT_PORT") or "1883")
MQTT_USER = os.environ.get("MQTT_USER") or None
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD") or None

# Topics for the "Sync Now" button entity, created via MQTT discovery (the
# same mechanism Zigbee2MQTT uses for its Restart button): a retained config
# on the discovery topic makes HA create a `button` entity that can be placed
# on any dashboard; pressing it publishes to the command topic.
MQTT_AVAILABILITY_TOPIC = "meshcore_test_bot/availability"
MQTT_COMMAND_TOPIC = "meshcore_test_bot/sync_now/press"
MQTT_DISCOVERY_TOPIC = "homeassistant/button/meshcore_test_bot/sync_now/config"


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

# txt_type of a CONTACT_MSG_RECV event (firmware TxtDataHelpers.h). Replies to
# firmware CLI commands (e.g. "clock", "time <epoch>") arrive as contact
# messages with TXT_TYPE_CLI_DATA, not as their own event type.
TXT_TYPE_CLI_DATA = 1

# Window (seconds) within which a device's clock counts as "already in sync".
# The firmware only ever moves a clock forward (`time` requires secs > curr,
# checked on arrival), and the epoch we push is stale by the mesh transit
# time when it gets there — so a device that's already accurate ALWAYS
# refuses with "clock cannot go backwards". A refusal with measured skew
# inside this window is therefore the best possible outcome, not a failure.
TIME_SYNC_IN_SYNC_TOLERANCE = 10

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


def format_skew(skew: int | None) -> str:
    """Render measured clock skew for humans.

    Always shows signed seconds (positive = device ahead of the host); once
    the magnitude stops being readable as seconds, appends an approximation
    in a larger unit — a repeater that sat powered off for months reads
    "-67514026 seconds (~781 days behind)" instead of a bare number.
    """
    if skew is None:
        return "unknown"
    text = f"{skew:+d} seconds"
    magnitude = abs(skew)
    if magnitude >= 172800:
        approx = f"~{magnitude / 86400:.0f} days"
    elif magnitude >= 7200:
        approx = f"~{magnitude / 3600:.0f} hours"
    elif magnitude >= 120:
        approx = f"~{magnitude / 60:.0f} minutes"
    else:
        approx = ""
    if approx:
        text += f" ({approx} {'ahead' if skew > 0 else 'behind'})"
    return text


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


async def wait_for_login_outcome(mc: MeshCore, timeout: float) -> tuple[bool, str]:
    """Wait for the remote login handshake to resolve, returning (ok, reason).

    meshcore's own ``send_login_sync`` only waits for ``LOGIN_SUCCESS``, so a
    wrong password — which the firmware answers with an explicit
    ``LOGIN_FAILED`` — is indistinguishable from an unreachable device; both
    just time out to ``None``. We race both event types so a failed sync can
    actually be diagnosed (bad password vs. no response at all).
    """
    success_wait = asyncio.ensure_future(
        mc.dispatcher.wait_for_event(EventType.LOGIN_SUCCESS, timeout=timeout)
    )
    failed_wait = asyncio.ensure_future(
        mc.dispatcher.wait_for_event(EventType.LOGIN_FAILED, timeout=timeout)
    )
    try:
        await asyncio.wait(
            {success_wait, failed_wait}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for pending in (success_wait, failed_wait):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(success_wait, failed_wait, return_exceptions=True)

    if not success_wait.cancelled() and success_wait.result() is not None:
        return True, ""
    if not failed_wait.cancelled() and failed_wait.result() is not None:
        return False, "device rejected the password (LOGIN_FAILED)"
    return False, f"no response within {timeout:.1f}s (device unreachable or busy)"


async def send_cli_and_wait_reply(
    mc: MeshCore, contact, pubkey: str, cmd: str
) -> tuple[dict | None, str]:
    """Send a firmware CLI command to a logged-in device and await its reply.

    Returns (reply_payload, error): exactly one is meaningful. The reply is
    the CONTACT_MSG_RECV payload of the device's CLI answer, matched on the
    sender's pubkey prefix and TXT_TYPE_CLI_DATA. Its ``sender_timestamp`` is
    the device's own clock (epoch seconds) at the moment it sent the reply,
    which is what makes clock-skew measurement possible without parsing the
    reply text (the "clock" command's text is only minute-resolution).
    """
    sent = await mc.commands.send_cmd(contact, cmd)
    if sent.type == EventType.ERROR:
        return None, f"could not send '{cmd}': {describe_error(sent.payload)}"
    # Same margin the login flow uses: suggested_timeout is in ms, /800
    # converts to seconds with 1.25x headroom for the reply leg.
    timeout = ((sent.payload or {}).get("suggested_timeout") or 4000) / 800
    reply = await mc.dispatcher.wait_for_event(
        EventType.CONTACT_MSG_RECV,
        attribute_filters={
            "pubkey_prefix": pubkey[:12].lower(),
            "txt_type": TXT_TYPE_CLI_DATA,
        },
        timeout=timeout,
    )
    if reply is None:
        return None, f"no reply to '{cmd}' within {timeout:.1f}s"
    return reply.payload or {}, ""


# Plain-English gloss for the device error codes we can actually act on.
# meshcore's reader already resolves a numeric error_code to this code_string
# (e.g. a companion-radio ERROR frame becomes {"error_code": 2, "code_string":
# "ERR_CODE_NOT_FOUND"}); we only need to translate the string, not the code.
_FRIENDLY_DEVICE_ERRORS = {
    "ERR_CODE_NOT_FOUND": "not a known contact on this companion radio",
    "ERR_CODE_TABLE_FULL": "companion radio's contact table is full",
    "ERR_CODE_BAD_STATE": "companion radio rejected the command (bad state)",
    "ERR_CODE_UNSUPPORTED_CMD": "companion radio does not support this command",
    "ERR_CODE_ILLEGAL_ARG": "companion radio rejected the command (illegal argument)",
    "ERR_CODE_FILE_IO_ERROR": "companion radio file I/O error",
}


def describe_error(payload: dict | None) -> str:
    """Render a command-error Event payload as a short, readable string.

    Device-originated errors carry a ``code_string`` (see above); client-side
    synthesized errors — a local send timeout, no response at all — carry
    ``reason`` instead (e.g. {"reason": "timeout"}), which is already
    readable as-is.
    """
    payload = payload or {}
    code_string = payload.get("code_string")
    if code_string:
        friendly = _FRIENDLY_DEVICE_ERRORS.get(code_string)
        return f"{code_string} ({friendly})" if friendly else code_string
    if "reason" in payload:
        return payload["reason"]
    if "error" in payload:
        return str(payload["error"])
    return str(payload)


def render_status_html(status: dict, devices_configured: bool) -> str:
    """Render the ingress "Sync Now" page. Vanilla HTML/JS, no build step or
    external assets, so it works regardless of the ingress path prefix HA
    assigns — everything is fetched relative to the current page URL."""
    if not devices_configured:
        body = "<p>No <code>time_sync_devices</code> are configured.</p>"
    else:
        button_disabled = "disabled" if status["running"] else ""
        body = f"""
        <button id="sync-btn" {button_disabled} onclick="triggerSync()">Sync Now</button>
        <p id="sync-msg"></p>
        <div id="results"></div>
        """
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>MeshCore Time Sync</title>
<style>
  body {{ font-family: sans-serif; margin: 2em; }}
  button {{ font-size: 1em; padding: 0.5em 1em; }}
  table {{ border-collapse: collapse; margin-top: 1em; }}
  td, th {{ border: 1px solid #ccc; padding: 0.4em 0.8em; text-align: left; }}
  .ok {{ color: green; }}
  .fail {{ color: #b00; }}
</style>
</head>
<body>
<h2>MeshCore Remote Time Sync</h2>
{body}
<script>
function renderStatus(s) {{
  document.getElementById('sync-btn').disabled = s.running;
  var msg = s.running ? 'Sync in progress...' : (s.finished_at ? 'Last run: ' + s.finished_at : '');
  document.getElementById('sync-msg').textContent = msg;
  var rows = s.results.map(function(r) {{
    return '<tr><td>' + r.name + '</td><td class="' + (r.ok ? 'ok' : 'fail') + '">' +
           (r.ok ? 'OK' : 'FAILED') + '</td><td>' + r.detail + '</td></tr>';
  }}).join('');
  document.getElementById('results').innerHTML = rows ?
    '<table><tr><th>Device</th><th>Result</th><th>Detail</th></tr>' + rows + '</table>' : '';
  if (s.running) {{ setTimeout(poll, 2000); }}
}}
function poll() {{
  fetch('status').then(function(r) {{ return r.json(); }}).then(renderStatus);
}}
function triggerSync() {{
  document.getElementById('sync-btn').disabled = true;
  document.getElementById('sync-msg').textContent = 'Starting...';
  fetch('trigger', {{ method: 'POST' }})
    .then(function(r) {{ return r.json(); }})
    .then(function(r) {{
      if (!r.ok) {{ document.getElementById('sync-msg').textContent = 'Error: ' + r.error; }}
      poll();
    }});
}}
poll();
</script>
</body>
</html>"""


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

    # Shared state read by the ingress web UI so a manual "Sync Now" click (or
    # the daily scheduler) has somewhere to report progress and per-device
    # results, since the actual work happens later on the reply worker.
    time_sync_status = {
        "running": False,
        "started_at": None,
        "finished_at": None,
        "results": [],
    }

    def enqueue_time_sync(source: str) -> bool:
        """Queue a timesync job unless one is already queued/running.

        Returns False (and does nothing) if a sync is already in flight, so a
        double-click on the web UI or an unlucky overlap with the daily
        schedule can't queue two runs back to back.
        """
        if time_sync_status["running"]:
            _LOGGER.info(
                "Time sync: %s trigger ignored, a sync is already running", source
            )
            return False
        time_sync_status["running"] = True
        time_sync_status["started_at"] = datetime.now().isoformat(timespec="seconds")
        time_sync_status["finished_at"] = None
        time_sync_status["results"] = []
        reply_queue.put_nowait(("timesync",))
        _LOGGER.info("Time sync: %s trigger queued", source)
        return True

    async def run_time_sync():
        """Log in to each configured device and set its clock to host time.

        Runs on the single reply worker so it is serialized against message
        replies — only one coroutine ever drives the serial link at a time,
        which is the rule the whole bot follows (see CLAUDE.md). Per-device
        failures are logged and skipped; one bad device never aborts the rest.
        """
        try:
            _LOGGER.info(
                "Time sync: starting for %d device(s)", len(TIME_SYNC_DEVICES)
            )

            # The contact list was loaded once at startup and is otherwise
            # only marked dirty (never refetched) when an advertisement or
            # path update comes in — see meshcore's _contact_change handler.
            # Force a refresh here so a device that started advertising after
            # the bot booted isn't permanently invisible to this daily job.
            await mc.ensure_contacts(follow=True)

            for dev in TIME_SYNC_DEVICES:
                pubkey = (dev.get("pubkey") or "").strip()
                password = dev.get("password") or ""
                label = dev.get("name") or (pubkey[:12] if pubkey else "(no pubkey)")
                if not pubkey:
                    _LOGGER.error(
                        "Time sync: device entry missing 'pubkey'; skipping"
                    )
                    time_sync_status["results"].append(
                        {"name": label, "ok": False, "detail": "missing 'pubkey'"}
                    )
                    continue

                # Login requires the full public key, and the companion radio
                # rejects it with ERR_CODE_NOT_FOUND if the key isn't in its own
                # contact/routing table — it never even reaches the mesh. Check
                # this ourselves first so a misconfigured or not-yet-advertised
                # device gets an immediate, specific message instead of paying
                # for a login round trip only to get the same answer back.
                contact = mc.get_contact_by_key_prefix(pubkey)
                if contact is None:
                    detail = (
                        "not a known contact on this companion radio — it may "
                        "not have advertised yet, or the configured pubkey is "
                        "wrong"
                    )
                    _LOGGER.error("Time sync: %s for %s", detail, label)
                    time_sync_status["results"].append(
                        {"name": label, "ok": False, "detail": detail}
                    )
                    continue

                try:
                    # We drive the login handshake ourselves (rather than
                    # send_login_sync) because that helper only waits for
                    # LOGIN_SUCCESS: a wrong password (which the firmware
                    # answers with LOGIN_FAILED) and an unreachable device both
                    # just look like a timeout. Racing both event types lets us
                    # log which one actually happened.
                    sent = await mc.commands.send_login(pubkey, password)
                    if sent.type == EventType.ERROR:
                        detail = f"could not send login request: {describe_error(sent.payload)}"
                        _LOGGER.error(
                            "Time sync: login failed for %s: %s", label, detail
                        )
                        time_sync_status["results"].append(
                            {"name": label, "ok": False, "detail": detail}
                        )
                        continue

                    suggested_timeout = (sent.payload or {}).get(
                        "suggested_timeout", 4000
                    )
                    ok, reason = await wait_for_login_outcome(
                        mc, suggested_timeout / 800
                    )
                    if not ok:
                        _LOGGER.error(
                            "Time sync: login failed for %s: %s", label, reason
                        )
                        time_sync_status["results"].append(
                            {"name": label, "ok": False, "detail": reason}
                        )
                        continue

                    # Measure the device's clock skew before correcting it:
                    # ask "clock" and read the reply's sender_timestamp (the
                    # device's clock when it sent the reply). Positive skew =
                    # device ahead of the host; accuracy is within the mesh
                    # transit time of the reply. Skew being unknown doesn't
                    # abort the sync — we still set the time.
                    skew = None
                    clock_reply, err = await send_cli_and_wait_reply(
                        mc, contact, pubkey, "clock"
                    )
                    if clock_reply is not None:
                        skew = clock_reply.get("sender_timestamp", 0) - int(
                            time.time()
                        )
                    else:
                        _LOGGER.warning(
                            "Time sync: %s: %s; skew unknown", label, err
                        )
                    skew_str = format_skew(skew)

                    epoch = int(time.time())
                    set_time_str = datetime.fromtimestamp(epoch).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    set_reply, err = await send_cli_and_wait_reply(
                        mc, contact, pubkey, f"time {epoch}"
                    )
                    if set_reply is None:
                        # CLI replies are sent once, unacknowledged, so the
                        # confirmation can get lost in the mesh even though
                        # the command itself was applied. Before declaring
                        # the outcome unknown, ask "clock" again: a residual
                        # skew near zero proves the clock is now right.
                        verify_reply, _ = await send_cli_and_wait_reply(
                            mc, contact, pubkey, "clock"
                        )
                        residual = (
                            verify_reply.get("sender_timestamp", 0)
                            - int(time.time())
                            if verify_reply is not None
                            else None
                        )
                        if residual is not None and abs(residual) <= 30:
                            ok = True
                            detail = (
                                f"Skew: {skew_str}, set time: {set_time_str} "
                                "(confirmation lost; verified via clock query)"
                            )
                        elif residual is not None:
                            ok = False
                            detail = (
                                f"{err}; follow-up clock query shows the "
                                f"clock is still off by {format_skew(residual)}"
                            )
                        else:
                            ok = False
                            detail = (
                                f"{err} — clock may or may not have been set "
                                f"(skew was {skew_str})"
                            )
                    else:
                        reply_text = set_reply.get("text", "")
                        if reply_text.startswith("OK"):
                            # "OK - clock set: HH:MM - D/M/YYYY UTC"
                            ok = True
                            detail = f"Skew: {skew_str}, set time: {set_time_str}"
                        elif "cannot go backwards" in reply_text:
                            # The firmware's monotonic guard (CommonCLI.cpp):
                            # `time` only succeeds when the pushed epoch is
                            # still in the device's future on arrival, so an
                            # already-accurate device always refuses. Judge
                            # the outcome by the measured skew instead.
                            if (
                                skew is not None
                                and abs(skew) < TIME_SYNC_IN_SYNC_TOLERANCE
                            ):
                                ok = True
                                detail = (
                                    "already in sync (firmware only moves "
                                    f"clocks forward; skew was {skew_str})"
                                )
                            elif (
                                skew is not None
                                and skew >= TIME_SYNC_IN_SYNC_TOLERANCE
                            ):
                                ok = False
                                detail = (
                                    "device clock is ahead and the firmware "
                                    "only moves clocks forward, so it cannot "
                                    "be corrected remotely (skew was "
                                    f"{skew_str})"
                                )
                            else:
                                # Skew unknown, or (contradictorily) the
                                # device looked behind yet still refused —
                                # report the raw refusal.
                                ok = False
                                detail = (
                                    f"device refused: {reply_text} "
                                    f"(skew was {skew_str})"
                                )
                        elif "ERR" in reply_text:
                            ok = False
                            detail = f"device refused: {reply_text} (skew was {skew_str})"
                        else:
                            ok = False
                            detail = (
                                f"unexpected reply: {reply_text!r} "
                                f"(skew was {skew_str})"
                            )
                    log = _LOGGER.info if ok else _LOGGER.error
                    log("Time sync: %s: %s", label, detail)
                    time_sync_status["results"].append(
                        {"name": label, "ok": ok, "detail": detail}
                    )

                    await mc.commands.send_logout(pubkey)
                except Exception as exc:
                    _LOGGER.exception("Time sync: error syncing %s", label)
                    time_sync_status["results"].append(
                        {"name": label, "ok": False, "detail": str(exc)}
                    )
                # Let the mesh settle before the next device.
                await asyncio.sleep(2)
            _LOGGER.info("Time sync: done")
        finally:
            time_sync_status["running"] = False
            time_sync_status["finished_at"] = datetime.now().isoformat(
                timespec="seconds"
            )

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
            enqueue_time_sync("scheduled")
            # Guard against re-firing within the same minute after a short sleep.
            await asyncio.sleep(60)

    async def mqtt_button_task():
        """Expose the manual sync trigger as a HA ``button`` entity over MQTT.

        Publishes a retained discovery config so Home Assistant auto-creates
        the entity (no user YAML), marks it available, and listens on the
        command topic. A press only calls enqueue_time_sync() — the actual
        device I/O still happens on the reply worker, so a dashboard press
        can't race the scheduler, the web UI, or message replies.

        Runs as its own reconnect loop: broker restarts (e.g. Mosquitto
        add-on updates) just cause a logged retry, never affect the bot.
        """
        discovery_payload = json.dumps(
            {
                "name": "Sync Now",
                "unique_id": "meshcore_test_bot_sync_now",
                "command_topic": MQTT_COMMAND_TOPIC,
                "payload_press": "PRESS",
                "availability_topic": MQTT_AVAILABILITY_TOPIC,
                "icon": "mdi:clock-check-outline",
                "device": {
                    "identifiers": ["meshcore_test_bot"],
                    "name": "MeshCore Test Bot",
                    "manufacturer": "meshcore-ha-addon",
                },
            }
        )
        while True:
            try:
                async with aiomqtt.Client(
                    MQTT_HOST,
                    port=MQTT_PORT,
                    username=MQTT_USER,
                    password=MQTT_PASSWORD,
                    # Last-will marks the button unavailable if the add-on
                    # dies without a clean disconnect (crash, SIGKILL).
                    will=aiomqtt.Will(
                        MQTT_AVAILABILITY_TOPIC, "offline", retain=True
                    ),
                ) as client:
                    if not TIME_SYNC_DEVICES:
                        # Clear a retained discovery config left over from a
                        # previous config that did have devices, so no dead
                        # button lingers on dashboards, then bow out.
                        await client.publish(MQTT_DISCOVERY_TOPIC, "", retain=True)
                        _LOGGER.info(
                            "MQTT: no time_sync_devices configured; "
                            "'Sync Now' button not published"
                        )
                        return
                    await client.publish(
                        MQTT_DISCOVERY_TOPIC, discovery_payload, retain=True
                    )
                    await client.publish(
                        MQTT_AVAILABILITY_TOPIC, "online", retain=True
                    )
                    await client.subscribe(MQTT_COMMAND_TOPIC)
                    _LOGGER.info(
                        "MQTT: 'Sync Now' button published (command topic %s)",
                        MQTT_COMMAND_TOPIC,
                    )
                    try:
                        async for _message in client.messages:
                            _LOGGER.info("MQTT: 'Sync Now' button pressed")
                            enqueue_time_sync("MQTT button")
                    except asyncio.CancelledError:
                        # Graceful shutdown: the last-will only fires on an
                        # unclean disconnect, so mark the button unavailable
                        # ourselves before the clean disconnect below.
                        with contextlib.suppress(Exception):
                            await client.publish(
                                MQTT_AVAILABILITY_TOPIC, "offline", retain=True
                            )
                        raise
            except aiomqtt.MqttError as exc:
                _LOGGER.warning(
                    "MQTT: connection failed (%s); retrying in 30s", exc
                )
                await asyncio.sleep(30)

    async def handle_web_index(request):
        return web.Response(
            text=render_status_html(time_sync_status, bool(TIME_SYNC_DEVICES)),
            content_type="text/html",
        )

    async def handle_web_status(request):
        return web.json_response(time_sync_status)

    async def handle_web_trigger(request):
        if not TIME_SYNC_DEVICES:
            return web.json_response(
                {"ok": False, "error": "No time_sync_devices configured."},
                status=400,
            )
        if not enqueue_time_sync("manual"):
            return web.json_response(
                {"ok": False, "error": "A sync is already running."}, status=409
            )
        return web.json_response({"ok": True})

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

        # CLI replies (e.g. to the time-sync "clock"/"time" commands) arrive
        # as contact messages too; they're consumed by run_time_sync's
        # wait_for_event and are not conversation, so don't log or reply.
        if msg.get("txt_type") == TXT_TYPE_CLI_DATA:
            return

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

    # Ingress-only web UI (see config.yaml: ingress/ingress_port, no `ports:`
    # mapping) with a manual "Sync Now" button. Started unconditionally since
    # the ingress panel is always registered, regardless of whether the daily
    # schedule is enabled — the manual trigger is also how you'd test the
    # feature before turning the schedule on.
    web_app = web.Application()
    web_app.router.add_get("/", handle_web_index)
    web_app.router.add_get("/status", handle_web_status)
    web_app.router.add_post("/trigger", handle_web_trigger)
    web_runner = web.AppRunner(web_app)
    await web_runner.setup()
    web_site = web.TCPSite(web_runner, "0.0.0.0", WEB_PORT)
    await web_site.start()
    _LOGGER.info("Time sync web UI listening on port %d", WEB_PORT)

    mqtt_task = None
    if MQTT_HOST:
        mqtt_task = asyncio.create_task(mqtt_button_task())
    else:
        _LOGGER.info("MQTT: no broker configured; 'Sync Now' dashboard button disabled")

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
        if mqtt_task is not None:
            # Await it so the availability "offline" publish (see
            # mqtt_button_task's CancelledError handler) actually completes.
            mqtt_task.cancel()
            await asyncio.gather(mqtt_task, return_exceptions=True)
        await web_runner.cleanup()
        await mc.stop_auto_message_fetching()
        await mc.disconnect()
        _LOGGER.info("Disconnected")


if __name__ == "__main__":
    asyncio.run(main())
