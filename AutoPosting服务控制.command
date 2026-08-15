#!/bin/bash
set -e
cd "$(dirname "$0")"
exec /usr/bin/env python3 service_control.py "$@"
