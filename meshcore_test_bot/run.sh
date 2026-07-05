#!/usr/bin/with-contenv bashio
# with-contenv matters: the base image's s6-overlay v3 runs this script with a
# scrubbed environment, so container env vars the Supervisor injects (notably
# SUPERVISOR_TOKEN, which bashio needs for every Supervisor API call) are only
# visible when the script is launched through with-contenv. Without it, MQTT
# broker auto-discovery always fails with 401 (empty token).

# Read config from HA options
export SERIAL_PORT=$(bashio::config 'serial_port')
export BAUDRATE=$(bashio::config 'baudrate')
export CHANNEL_NAME=$(bashio::config 'channel_name')
export CHANNEL_IDX=$(bashio::config 'channel_idx')
export TRIGGER_TEXT=$(bashio::config 'trigger_text')
export DM_TRIGGER_TEXT=$(bashio::config 'dm_trigger_text')
export DEVICE_NAME=$(bashio::config 'device_name')
export TIME_SYNC_ENABLED=$(bashio::config 'time_sync_enabled')
export TIME_SYNC_AT=$(bashio::config 'time_sync_at')
# Complex (list-of-objects) option: pass the raw JSON for bot.py to parse.
export TIME_SYNC_DEVICES=$(bashio::config 'time_sync_devices')

# MQTT broker for the "Sync Now" dashboard button. Preference order:
# explicit mqtt_* options first (needed when the broker is anything other
# than the official Mosquitto add-on — the Supervisor services API is only
# populated by that add-on), then Supervisor auto-discovery (requires
# `services: mqtt:want` in config.yaml). With neither, bot.py sees an empty
# MQTT_HOST and skips the dashboard button.
if bashio::config.has_value 'mqtt_host'; then
    export MQTT_HOST=$(bashio::config 'mqtt_host')
    export MQTT_PORT=$(bashio::config 'mqtt_port')
    export MQTT_USER=$(bashio::config 'mqtt_user')
    export MQTT_PASSWORD=$(bashio::config 'mqtt_password')
    bashio::log.info "Using configured MQTT broker at ${MQTT_HOST}:${MQTT_PORT}"
elif bashio::services.available "mqtt"; then
    export MQTT_HOST=$(bashio::services mqtt "host")
    export MQTT_PORT=$(bashio::services mqtt "port")
    export MQTT_USER=$(bashio::services mqtt "username")
    export MQTT_PASSWORD=$(bashio::services mqtt "password")
    bashio::log.info "MQTT broker discovered via Supervisor at ${MQTT_HOST}:${MQTT_PORT}"
else
    bashio::log.warning "No MQTT broker found; 'Sync Now' dashboard button disabled"
    # Log the raw Supervisor answer so the add-on log shows *why* discovery
    # failed (e.g. "Service not enabled" = no add-on has registered as the
    # mqtt provider, which only the official Mosquitto add-on does).
    # ${VAR:-} guards: bashio scripts run with nounset.
    if [ -n "${SUPERVISOR_TOKEN:-}" ]; then
        bashio::log.warning "SUPERVISOR_TOKEN is present (${#SUPERVISOR_TOKEN} chars); the Supervisor rejected the request:"
    else
        bashio::log.warning "SUPERVISOR_TOKEN is not set in this environment (with-contenv problem?); the Supervisor will answer 401:"
    fi
    bashio::log.warning "Supervisor /services/mqtt said: $(curl -s -H "Authorization: Bearer ${SUPERVISOR_TOKEN:-}" http://supervisor/services/mqtt)"
    bashio::log.warning "Auto-discovery only finds the official Mosquitto broker add-on; for any other broker, set the mqtt_host/mqtt_port/mqtt_user/mqtt_password options"
fi

bashio::log.info "Starting MeshCore Test Bot on ${SERIAL_PORT}"
exec python3 /app/bot.py
