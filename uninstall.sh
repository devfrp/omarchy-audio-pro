#!/usr/bin/bash
# Thin trusted-PATH shim — see install.sh / uninstall.py
# (github.com/omacom/omarchy-plugin-marketplace#6521).
set -euo pipefail
export PATH=/usr/bin:/bin
dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec /usr/bin/python3 -I -- "$dir/uninstall.py" "$@"
