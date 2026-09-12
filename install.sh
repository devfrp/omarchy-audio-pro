#!/usr/bin/bash
set -euo pipefail
export PATH=/usr/bin:/bin

# Security-review hardening (github.com/omacom/omarchy-plugin-marketplace#6521):
# PATH is locked to the base system directories for the whole script before
# anything else runs, and every externally-significant tool is additionally
# resolved to a real path whose full ancestry (/, /usr, /usr/bin, ... down to
# the binary itself) is root-owned and not group/other-writable. Internal
# plumbing (mkdir, readlink, mv, stat, dirname, rm) relies on the PATH lock
# alone — verifying them against themselves would be circular, and the lock
# already keeps them out of any writable directory.
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

for cmd in python3 pw-dump pw-link pw-metadata pactl systemctl omarchy omarchy-shell; do
  require_trusted "$cmd"
done

src=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
config=${XDG_CONFIG_HOME:-$HOME/.config}
target="$config/omarchy/plugins/devfrp.audio-patchbay"

omarchy plugin validate "$src"
mkdir -p -- "$(dirname -- "$target")"

if [[ -L "$target" ]]; then
  [[ "$(readlink -- "$target")" == "$src" ]] || { echo "Refusing to replace $target (points elsewhere)" >&2; exit 1; }
elif [[ -e "$target" ]]; then
  echo "Refusing to replace $target (not a symlink)" >&2
  exit 1
else
  tmp="$target.tmp.$$"
  trap 'rm -f -- "$tmp"' EXIT
  ln -s -- "$src" "$tmp"
  mv -Tn -- "$tmp" "$target" || true
  if [[ ! -L "$target" || "$(readlink -- "$target")" != "$src" ]]; then
    echo "Refusing to replace $target: it appeared unexpectedly during install." >&2
    exit 1
  fi
  trap - EXIT
fi

omarchy-shell shell rescanPlugins
omarchy plugin enable devfrp.audio-patchbay --section right
echo 'Installed. Click the ⇄ icon in the bar. No audio settings were changed.'
echo 'If QML is cached after an update, run: omarchy restart shell'
