ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install Python, pip, and build tools.
# build-essential/libffi-dev are needed to compile pycryptodome from source on
# arm (armv7/armhf), which have no prebuilt wheels on PyPI.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-dev \
    build-essential \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies (Debian Bookworm+ requires --break-system-packages per PEP 668)
COPY requirements.txt /tmp/
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

# Copy application
COPY bot.py /app/bot.py
COPY run.sh /

RUN chmod +x /run.sh

CMD [ "/run.sh" ]
