#!/usr/bin/python3
"""Installer for devfrp.audio-patchbay — publishes a symlink to this
checkout under the user's Omarchy plugin directory and enables the widget.

Rewritten from a pure-bash install.sh (github.com/omacom/omarchy-plugin-
marketplace#6521, third review pass): shell has no way to open a path with
O_NOFOLLOW or hold a directory descriptor across later operations, so a
script publishing a symlink under the user's config tree can only minimize,
never eliminate, the check-then-use gap between verifying a directory and
using it. Python's os module exposes O_NOFOLLOW and dir_fd= directly, the
same primitives already used for the WirePlumber config write in
patchbay.py — every directory component below $HOME is opened exactly
once, O_NOFOLLOW, relative to its own already-verified parent descriptor,
and the final symlink is published via an unpredictable exclusive name
linked into place (os.link refuses if the destination already exists,
unlike a plain rename) rather than a checked-then-used pathname.

install.sh is now a thin trusted-PATH shim that execs this file.
"""
import errno
import os
import secrets
import stat
import subprocess
import sys
from pathlib import Path

PLUGIN_ID = 'devfrp.audio-patchbay'
SECTION = 'right'
TRUSTED_BIN_DIRS = ('/usr/bin', '/bin')
REQUIRED_TOOLS = ('python3', 'pw-dump', 'pw-link', 'pw-metadata', 'pactl', 'systemctl',
                   'omarchy', 'omarchy-shell')


# ------------------------------------------------------- trusted tools ----

def _is_root_owned_and_protected(path):
    st = os.stat(path)
    return st.st_uid == 0 and not (st.st_mode & (stat.S_IWGRP | stat.S_IWOTH))


def _verify_trusted_ancestry(resolved_path):
    current = Path('/')
    for part in Path(resolved_path).parts[1:]:
        current = current / part
        if not _is_root_owned_and_protected(current):
            return False
    return True


def _trusted_tool(name):
    for directory in TRUSTED_BIN_DIRS:
        candidate = os.path.join(directory, name)
        if not (os.path.exists(candidate) and os.access(candidate, os.X_OK)):
            continue
        resolved = os.path.realpath(candidate)
        if os.path.dirname(resolved) not in TRUSTED_BIN_DIRS:
            continue
        if not _verify_trusted_ancestry(resolved):
            continue
        return resolved
    sys.exit(f'Missing or untrusted dependency: {name} '
              f'(must resolve under {" or ".join(TRUSTED_BIN_DIRS)}, root-owned, not group/other-writable)')


def _run_trusted(args):
    subprocess.run([_trusted_tool(args[0]), *args[1:]], check=True)


# --------------------------------------------- owned, no-follow dir walk --

def _verify_owned_fd(fd, label):
    st = os.fstat(fd)
    if st.st_uid != os.getuid():
        os.close(fd)
        sys.exit(f'Refusing to use "{label}": not owned by the current user.')
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        os.close(fd)
        sys.exit(f'Refusing to use "{label}": writable by group or other.')


def _open_dir_component(parent_fd, name):
    flags = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY | os.O_CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        fd = os.open(name, flags, dir_fd=parent_fd)
    except NotADirectoryError:
        sys.exit(f'Refusing to use "{name}": not a directory.')
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            sys.exit(f'Refusing to follow a symlink at "{name}".')
        raise
    _verify_owned_fd(fd, name)
    return fd


def plugins_dir_fd():
    home = str(Path.home())
    fd = os.open(home, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY | os.O_CLOEXEC)
    _verify_owned_fd(fd, home)
    try:
        for part in ('.config', 'omarchy', 'plugins'):
            next_fd = _open_dir_component(fd, part)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


# --------------------------------------------------------------- publish --

def publish_symlink(dir_fd, name, target):
    """Create `name` -> `target` under `dir_fd` without ever re-resolving
    the parent pathname: build the symlink under an unpredictable temp name
    (dir_fd-relative, so a component swap elsewhere can't redirect it),
    then link it to the final name — os.link refuses outright if that name
    already exists, so a race can't silently overwrite it the way a plain
    rename would."""
    temp_name = f'.{name}.tmp-{secrets.token_hex(8)}'
    os.symlink(target, temp_name, dir_fd=dir_fd)
    try:
        os.link(temp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
    except FileExistsError:
        sys.exit(f'Refusing to replace "{name}": it appeared unexpectedly during install.')
    finally:
        os.unlink(temp_name, dir_fd=dir_fd)


def main():
    for tool in REQUIRED_TOOLS:
        _trusted_tool(tool)

    src = str(Path(__file__).resolve().parent)
    _run_trusted(['omarchy', 'plugin', 'validate', src])

    dir_fd = plugins_dir_fd()
    try:
        try:
            existing = os.readlink(PLUGIN_ID, dir_fd=dir_fd)
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            if exc.errno == errno.EINVAL:
                sys.exit(f'Refusing to replace {PLUGIN_ID}: exists and is not a symlink.')
            raise

        if existing is not None:
            if existing != src:
                sys.exit(f'Refusing to replace {PLUGIN_ID}: points elsewhere ({existing}).')
        else:
            publish_symlink(dir_fd, PLUGIN_ID, src)
    finally:
        os.close(dir_fd)

    _run_trusted(['omarchy-shell', 'shell', 'rescanPlugins'])
    _run_trusted(['omarchy', 'plugin', 'enable', PLUGIN_ID, '--section', SECTION])
    print('Installed. Click the ⇄ icon in the bar. No audio settings were changed.')
    print('If QML is cached after an update, run: omarchy restart shell')


if __name__ == '__main__':
    main()
