#!/usr/bin/env bash
# Closes active TCP sockets to the specified IP addresses (needs root / CAP_NET_ADMIN)
set -u
for ip in "$@"; do
    if [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        ss -K -t dst "$ip" >/dev/null 2>&1 || true
    fi
done
