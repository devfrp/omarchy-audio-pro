#!/usr/bin/bash
# Thin trusted-PATH shim — every security-sensitive operation (directory
# walking, symlink publication) lives in install.py, which can actually
# open paths O_NOFOLLOW and hold a directory descriptor across operations;
# bash has no way to do either (github.com/omacom/omarchy-plugin-marketplace#6521).
set -euo pipefail
export PATH=/usr/bin:/bin
dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec /usr/bin/python3 -I -- "$dir/install.py" "$@"
