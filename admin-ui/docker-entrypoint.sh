#!/bin/sh
set -e
ADMIN_URL="${PROXY_HOPPER_ADMIN_URL:-http://localhost:8081}"
echo "window.__PROXY_HOPPER_ADMIN_URL__ = '${ADMIN_URL}';" > /usr/share/nginx/html/config.js
exec nginx -g 'daemon off;'
