#!/bin/sh
set -eu

: "${QUANTDESK_USER:?QUANTDESK_USER is required}"
: "${QUANTDESK_PASSWORD:?QUANTDESK_PASSWORD is required}"

htpasswd -bc /etc/nginx/.htpasswd "$QUANTDESK_USER" "$QUANTDESK_PASSWORD" >/dev/null
exec "$@"
