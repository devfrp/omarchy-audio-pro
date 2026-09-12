#!/usr/bin/bash
set -euo pipefail
export PATH=/usr/bin:/bin

# See install.sh for the require_trusted/verify_ancestry rationale
# (github.com/omacom/omarchy-plugin-marketplace#6521).
verify_ancestry() {
  local path="$1" current="" part owner mode mode_dec
  IFS='/' read -ra parts <<< "$path"
  for part in "${parts[@]}"; do
    [[ -z "$part" ]] && continue
    current="$current/$part"
    owner=$(stat -c '%u' -- "$current")
    mode=$(stat -c '%a' -- "$current")
    mode_dec=$(( 8#$mode ))
    if [[ "$owner" != "0" ]] || (( (mode_dec & 18) != 0 )); then
      return 1
    fi
  done
  return 0
}

require_trusted() {
  local cmd="$1" resolved
  resolved=$(command -v -- "$cmd") || { echo "Missing dependency: $cmd" >&2; exit 1; }
  resolved=$(readlink -f -- "$resolved")
  case "$resolved" in
    /usr/bin/*|/bin/*) ;;
    *) echo "Refusing to use untrusted $cmd resolved outside /usr/bin or /bin: $resolved" >&2; exit 1 ;;
  esac
  verify_ancestry "$resolved" \
    || { echo "Refusing to use $cmd: an ancestor of $resolved is not root-owned and protected" >&2; exit 1; }
}

for cmd in omarchy omarchy-shell; do
  require_trusted "$cmd"
done

src=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
target="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/devfrp.audio-patchbay"

if [[ ! -L "$target" || "$(readlink -- "$target")" != "$src" ]]; then
  echo "Refusing to remove a plugin link not owned by this checkout." >&2
  exit 1
fi

omarchy plugin disable devfrp.audio-patchbay
unlink -- "$target"
omarchy-shell shell rescanPlugins
echo 'Plugin removed. Source, saved audio defaults and current overrides are preserved.'
echo 'Reset sample rate and bit depth to Auto in the popup beforehand if you want a clean slate.'
