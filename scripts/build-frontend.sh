#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -d .tools/node-v22.22.0-linux-x64/bin ]; then
    export PATH="$PWD/.tools/node-v22.22.0-linux-x64/bin:$PATH"
fi
cd frontend
export NODE_OPTIONS="${NODE_OPTIONS:---max-old-space-size=640}"
npm ci
npm run build
