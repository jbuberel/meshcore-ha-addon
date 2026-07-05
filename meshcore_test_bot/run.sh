#!/usr/bin/env bashio

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

# MQTT broker credentials from the Supervisor services API (requires
# `services: mqtt:want` in config.yaml). Optional — when no broker add-on is
# installed, bot.py sees an empty MQTT_HOST and skips the dashboard button.
if bashio::services.available "mqtt"; then
    export MQTT_HOST=$(bashio::services mqtt "host")
    export MQTT_PORT=$(bashio::services mqtt "port")
    export MQTT_USER=$(bashio::services mqtt "username")
    export MQTT_PASSWORD=$(bashio::services mqtt "password")
    bashio::log.info "MQTT broker available at ${MQTT_HOST}:${MQTT_PORT}"
else
    bashio::log.info "No MQTT broker available; 'Sync Now' dashboard button disabled"
fi

bashio::log.info "Starting MeshCore Test Bot on ${SERIAL_PORT}"
exec python3 /app/bot.py
