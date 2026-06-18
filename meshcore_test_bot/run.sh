#!/usr/bin/env bashio

# Read config from HA options
export SERIAL_PORT=$(bashio::config 'serial_port')
export BAUDRATE=$(bashio::config 'baudrate')
export CHANNEL_NAME=$(bashio::config 'channel_name')
export CHANNEL_IDX=$(bashio::config 'channel_idx')
export TRIGGER_TEXT=$(bashio::config 'trigger_text')
export DEVICE_NAME=$(bashio::config 'device_name')

bashio::log.info "Starting MeshCore Test Bot on ${SERIAL_PORT}"
exec python3 /app/bot.py
