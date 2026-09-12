#!/usr/bin/python3
"""Uninstaller for devfrp.audio-patchbay — see install.py for why this
lives in Python rather than bash (github.com/omacom/omarchy-plugin-
marketplace#6521, third review pass)."""
import errno
import os
import stat
import subprocess
import sys
from pathlib import Path

PLUGIN_ID = 'devfrp.audio-patchbay'
TRUSTED_BIN_DIRS = ('/usr/bin', '/bin')
REQUIRED_TOOLS = ('omarchy', 'omarchy-shell')


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


def main():
    for tool in REQUIRED_TOOLS:
        _trusted_tool(tool)

    src = str(Path(__file__).resolve().parent)

    try:
        dir_fd = plugins_dir_fd()
    except FileNotFoundError:
        sys.exit('Refusing to remove a plugin link not owned by this checkout (not installed).')

    try:
        try:
            existing = os.readlink(PLUGIN_ID, dir_fd=dir_fd)
        except (FileNotFoundError, OSError):
            sys.exit('Refusing to remove a plugin link not owned by this checkout (not found).')
        if existing != src:
            sys.exit('Refusing to remove a plugin link not owned by this checkout.')

        _run_trusted(['omarchy', 'plugin', 'disable', PLUGIN_ID])
        os.unlink(PLUGIN_ID, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)

    _run_trusted(['omarchy-shell', 'shell', 'rescanPlugins'])
    print('Plugin removed. Source, saved audio defaults and current overrides are preserved.')
    print('Reset sample rate and bit depth to Auto in the popup beforehand if you want a clean slate.')


if __name__ == '__main__':
    main()
