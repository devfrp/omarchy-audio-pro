#!/usr/bin/env bash
set -euo pipefail
src=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
target="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/devfrp.audio-patchbay"
if [[ ! -L "$target" || $(readlink -f "$target") != "$src" ]]; then
  echo "Refusing to remove a plugin link not owned by this checkout." >&2
  exit 1
fi
omarchy plugin disable devfrp.audio-patchbay
unlink "$target"
omarchy-shell shell rescanPlugins
echo 'Plugin removed. Source, saved audio defaults and current overrides are preserved.'
echo 'Reset sample rate and bit depth to Auto in the popup beforehand if you want a clean slate.'
