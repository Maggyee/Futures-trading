#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
exec .venv/bin/python -m backend.cli serve
