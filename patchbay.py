#!/usr/bin/env python3
"""Backend for the Audio Patchbay bar widget. No external Python dependencies.

Talks to PipeWire/WirePlumber through pw-dump, pw-link, pw-metadata and
pactl. All state-changing actions are reversible: rate/quantum overrides
clear back to "auto" via pw-metadata, and the bit-depth rule is a single
owned file removed by its own "auto" preset.
"""
import errno
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time

RATE_PRESETS = {
    'auto': (0, 0),
    'music': (44100, 1024),
    'studio': (48000, 256),
    'hires88': (88200, 512),
    'hires': (96000, 512),
    'ultra176': (176400, 1024),
    'ultra': (192000, 1024),
    'extreme352': (352800, 2048),
    'extreme': (384000, 2048),
    'max705': (705600, 4096),
    'max': (768000, 4096),
}

BIT_PRESETS = {
    'auto': None,
    '16': 'S16LE',
    '24': 'S24LE',
    '32': 'S32LE',
}

MARKER = '# Owned by devfrp.audio-patchbay\n'
AUDIO_MEDIA_CLASSES = (
    'Audio/Sink', 'Audio/Source', 'Stream/Output/Audio', 'Stream/Input/Audio',
)
BIT_CONFIG_NAME = '70-audio-patchbay-bitdepth.conf'

# Security-review hardening (github.com/omacom/omarchy-plugin-marketplace#6521):
# external tools are resolved once against a fixed, trusted directory list —
# never the inherited PATH, which a shadow executable earlier in PATH could
# hijack — and every subprocess runs with a closed-down environment, a real
# wall-clock deadline enforced across its whole process group, and a cap on
# how much stdout/stderr it may produce.
#
# TRUSTED_BIN_DIRS deliberately excludes /usr/local/bin: on most distros
# that tree is writable by local package/admin tooling rather than only by
# the base OS install, so trusting it would just relocate the shadow-
# executable risk one directory over. Every resolved binary and every
# directory in its path is also required to be root-owned and not
# group/other-writable (_verify_trusted_ancestry) — matching the resolved
# name against a fixed directory *string* isn't enough on its own.
TRUSTED_BIN_DIRS = ('/usr/bin', '/bin')
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
RUN_TIMEOUT_SECONDS = 8
_TOOL_CACHE = {}


def _config_root():
    configured = os.environ.get('XDG_CONFIG_HOME')
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_absolute():
            return candidate
    return Path.home() / '.config'


def _is_root_owned_and_protected(path):
    st = os.stat(path)
    return st.st_uid == 0 and not (st.st_mode & (stat.S_IWGRP | stat.S_IWOTH))


def _verify_trusted_ancestry(resolved_path):
    """Every directory from / down to the resolved binary itself — not just
    the final directory name — must be root-owned and not writable by group
    or other, so nothing in the chain could have been swapped or written to
    by a non-root local user."""
    current = Path('/')
    for part in Path(resolved_path).parts[1:]:
        current = current / part
        if not _is_root_owned_and_protected(current):
            return False
    return True


def _trusted_tool(name):
    if name in _TOOL_CACHE:
        return _TOOL_CACHE[name]
    for directory in TRUSTED_BIN_DIRS:
        candidate = os.path.join(directory, name)
        if not (os.path.exists(candidate) and os.access(candidate, os.X_OK)):
            continue
        resolved = os.path.realpath(candidate)
        if os.path.dirname(resolved) not in TRUSTED_BIN_DIRS:
            continue
        if not _verify_trusted_ancestry(resolved):
            continue
        _TOOL_CACHE[name] = resolved
        return resolved
    raise RuntimeError(f'Required tool "{name}" was not found in a trusted location '
                        f'({", ".join(TRUSTED_BIN_DIRS)}).')


def _closed_env():
    keep = ('HOME', 'USER', 'XDG_RUNTIME_DIR', 'XDG_CONFIG_HOME',
            'DBUS_SESSION_BUS_ADDRESS', 'WAYLAND_DISPLAY')
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env['PATH'] = ':'.join(TRUSTED_BIN_DIRS)
    env['LC_ALL'] = 'C'
    return env


def _kill_process_tree(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=2)
    except Exception:
        pass


def run(args):
    """Run a trusted tool with a closed environment, a real deadline across
    its whole process group, and a hard cap on how much output it may
    produce — a subprocess that stalls, forks, or floods stdout can't hang
    or exhaust memory in the caller."""
    resolved = [_trusted_tool(args[0]), *args[1:]]
    proc = subprocess.Popen(resolved, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=_closed_env(), start_new_session=True)
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ, 'out')
    sel.register(proc.stderr, selectors.EVENT_READ, 'err')
    chunks = {'out': [], 'err': []}
    sizes = {'out': 0, 'err': 0}
    open_streams = 2
    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    try:
        while open_streams > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(resolved, RUN_TIMEOUT_SECONDS)
            for key, _mask in sel.select(timeout=remaining):
                stream = key.data
                data = os.read(key.fileobj.fileno(), 65536)
                if data == b'':
                    sel.unregister(key.fileobj)
                    open_streams -= 1
                    continue
                sizes[stream] += len(data)
                if sizes[stream] > MAX_OUTPUT_BYTES:
                    raise RuntimeError(
                        f'{args[0]} produced more than {MAX_OUTPUT_BYTES} bytes of output.')
                chunks[stream].append(data)
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    except (subprocess.TimeoutExpired, RuntimeError):
        _kill_process_tree(proc)
        raise
    finally:
        sel.close()
    stdout = b''.join(chunks['out']).decode('utf-8', 'replace')
    stderr = b''.join(chunks['err']).decode('utf-8', 'replace')
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, resolved, stdout, stderr)
    return stdout


# ---------------------------------------------------------------- graph ----

def _dump():
    return json.loads(run(['pw-dump']))


def graph_snapshot(objs):
    nodes = {}
    ports = {}
    links = []
    for obj in objs:
        obj_type = obj.get('type', '')
        info = obj.get('info') or {}
        props = info.get('props') or {}
        if obj_type.endswith('Node'):
            media_class = props.get('media.class', '')
            if media_class not in AUDIO_MEDIA_CLASSES:
                continue
            nodes[obj['id']] = {
                'id': obj['id'],
                'name': props.get('node.description') or props.get('node.nick')
                or props.get('node.name') or str(obj['id']),
                'mediaClass': media_class,
            }
        elif obj_type.endswith('Port'):
            if 'audio' not in props.get('format.dsp', '').lower():
                continue
            ports[obj['id']] = {
                'id': obj['id'],
                'nodeId': props.get('node.id'),
                'name': props.get('port.name', str(obj['id'])),
                'direction': props.get('port.direction', ''),
            }
        elif obj_type.endswith('Link'):
            links.append({
                'id': obj['id'],
                'outputPort': props.get('link.output.port'),
                'inputPort': props.get('link.input.port'),
            })

    def port_entry(port):
        node = nodes.get(port['nodeId'], {'name': '?'})
        return {
            'id': port['id'],
            'nodeId': port['nodeId'],
            'label': f"{node['name']} · {port['name']}",
        }

    outputs = [port_entry(p) for p in ports.values() if p['nodeId'] in nodes and p['direction'] == 'out']
    inputs = [port_entry(p) for p in ports.values() if p['nodeId'] in nodes and p['direction'] == 'in']
    outputs.sort(key=lambda p: p['label'])
    inputs.sort(key=lambda p: p['label'])

    live_links = []
    for link in links:
        if link['outputPort'] in ports and link['inputPort'] in ports:
            live_links.append({
                'id': link['id'],
                'outputPortId': link['outputPort'],
                'inputPortId': link['inputPort'],
                'outputLabel': port_entry(ports[link['outputPort']])['label'],
                'inputLabel': port_entry(ports[link['inputPort']])['label'],
            })
    live_links.sort(key=lambda l: (l['outputLabel'], l['inputLabel']))

    return {'outputs': outputs, 'inputs': inputs, 'links': live_links}


def connect(output_port, input_port):
    run(['pw-link', str(output_port), str(input_port)])


def disconnect(link_id):
    run(['pw-link', '-d', str(link_id)])


# ---------------------------------------------------------- capabilities ---

def _enum_field_values(field):
    """Normalize a pw-dump EnumFormat field (scalar, enum-choice dict with
    default/alt1/alt2/..., or range-choice dict with default/min/max) into
    either {'kind': 'range', 'min', 'max'} or {'kind': 'enum', 'values': [...]}.
    """
    if isinstance(field, dict):
        if 'min' in field and 'max' in field:
            return {'kind': 'range', 'min': field['min'], 'max': field['max']}
        values = []
        if 'default' in field:
            values.append(field['default'])
        i = 1
        while f'alt{i}' in field:
            values.append(field[f'alt{i}'])
            i += 1
        seen = []
        for v in values:
            if v not in seen:
                seen.append(v)
        return {'kind': 'enum', 'values': seen}
    return {'kind': 'enum', 'values': [field]}


def node_capabilities(objs, node_name):
    """Supported sample rates / bit-depth formats for the ALSA/Bluetooth node
    named `node_name`, read from its live pw-dump EnumFormat params."""
    for obj in objs:
        if not obj.get('type', '').endswith('Node'):
            continue
        props = (obj.get('info') or {}).get('props') or {}
        if props.get('node.name') != node_name:
            continue
        enum_formats = ((obj.get('info') or {}).get('params') or {}).get('EnumFormat') or []
        rates = set()
        rate_ranges = []
        formats = set()
        for entry in enum_formats:
            rf = _enum_field_values(entry.get('rate'))
            if rf['kind'] == 'range':
                rate_ranges.append((rf['min'], rf['max']))
            else:
                rates.update(rf['values'])
            ff = _enum_field_values(entry.get('format'))
            formats.update(ff['values'])
        return {'rates': sorted(rates), 'rateRanges': [list(r) for r in rate_ranges], 'formats': sorted(formats)}
    return {'rates': [], 'rateRanges': [], 'formats': []}


def rate_supported(caps, rate):
    if rate == 0:
        return True
    if rate in caps.get('rates', []):
        return True
    return any(lo <= rate <= hi for lo, hi in caps.get('rateRanges', []))


def format_supported(caps, audio_format):
    if not audio_format:
        return True
    return audio_format in caps.get('formats', [])


# ------------------------------------------------------------- clock/rate --

def metadata():
    text = run(['pw-metadata', '-n', 'settings'])
    return dict(re.findall(r"key:'([^']+)' value:'([^']*)'", text))


def apply_rate(preset):
    if preset not in RATE_PRESETS:
        raise ValueError(f'Unknown sample-rate preset: {preset}')
    rate, buffer = RATE_PRESETS[preset]
    if rate:
        sink = default_sink_name()
        caps = node_capabilities(_dump(), sink)
        if not rate_supported(caps, rate):
            raise ValueError(f'{rate} Hz not supported by "{sink}".')
    old = metadata()
    try:
        for key, value in [('clock.force-rate', rate), ('clock.force-quantum', buffer)]:
            run(['pw-metadata', '-n', 'settings', '0', key, str(value)])
    except Exception as exc:
        errors = []
        for key in ('clock.force-rate', 'clock.force-quantum'):
            try:
                run(['pw-metadata', '-n', 'settings', '0', key, old.get(key, '0')])
            except Exception as rollback:
                errors.append(str(rollback))
        raise RuntimeError(f'Apply failed: {exc}. Rollback: {errors or "completed"}') from exc


# -------------------------------------------------------------- bit depth --
#
# Security-review hardening: every step below the config root opens relative
# to an already-open, already-verified directory descriptor (`dir_fd=`) with
# O_NOFOLLOW, so there is exactly one lookup per path component — nothing to
# race, because a symlink swapped in after the check simply isn't what gets
# opened. Each descriptor is also checked as owned by us and not group/other
# -writable before we trust it. The final write goes through a private temp
# file plus an atomic rename, both addressed by that same descriptor.

def _verify_owned_fd(fd, label):
    stat_result = os.fstat(fd)
    if stat_result.st_uid != os.getuid():
        os.close(fd)
        raise ValueError(f'Refusing to use "{label}": not owned by the current user.')
    if stat_result.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        os.close(fd)
        raise ValueError(f'Refusing to use "{label}": writable by group or others.')


def _open_dir_component(parent_fd, name, create):
    flags = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY | os.O_CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            return None
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        fd = os.open(name, flags, dir_fd=parent_fd)
    except NotADirectoryError as exc:
        raise ValueError(f'Refusing to use "{name}": not a directory.') from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError(f'Refusing to follow a symlink at "{name}".') from exc
        raise
    _verify_owned_fd(fd, name)
    return fd


def _config_dir_fd(create):
    """Descriptor for wireplumber/wireplumber.conf.d under the config root,
    walked one verified component at a time. Returns None (create=False
    only) if any component along the way doesn't exist yet."""
    root = _config_root()
    fd = os.open(str(root), os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY | os.O_CLOEXEC)
    _verify_owned_fd(fd, str(root))
    try:
        for part in ('wireplumber', 'wireplumber.conf.d'):
            next_fd = _open_dir_component(fd, part, create)
            os.close(fd)
            if next_fd is None:
                return None
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


MAX_MARKER_FILE_BYTES = 64 * 1024  # generous for a config this plugin writes itself


def _owned_marker_content(dir_fd, name):
    """Read `name`'s content if it's a plain file we own, carrying our
    marker — bounded to MAX_MARKER_FILE_BYTES so a status poll (this runs
    on every refresh, unlike the once-per-action write path) can't be made
    to read an unbounded amount of memory from an oversized owned file."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError(f'Refusing to touch a symlink: {name}') from exc
        raise
    with os.fdopen(fd, 'rb') as stream:
        stat_result = os.fstat(stream.fileno())
        if not stat.S_ISREG(stat_result.st_mode) or stat_result.st_uid != os.getuid():
            raise ValueError(f'Refusing to touch an unowned file: {name}')
        raw = stream.read(MAX_MARKER_FILE_BYTES + 1)
    if len(raw) > MAX_MARKER_FILE_BYTES:
        raise ValueError(f'Refusing to read "{name}": larger than {MAX_MARKER_FILE_BYTES} bytes.')
    content = raw.decode('utf-8')
    if not content.startswith(MARKER):
        raise ValueError(f'Refusing to touch an unowned file: {name}')
    return content


def default_sink_name():
    return run(['pactl', 'get-default-sink']).strip()


def apply_bitdepth(preset):
    if preset not in BIT_PRESETS:
        raise ValueError(f'Unknown bit-depth preset: {preset}')
    dir_fd = _config_dir_fd(create=True)
    try:
        _owned_marker_content(dir_fd, BIT_CONFIG_NAME)  # validates ownership before any write/removal
        audio_format = BIT_PRESETS[preset]
        if audio_format is None:
            try:
                os.unlink(BIT_CONFIG_NAME, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
        else:
            sink = default_sink_name()
            if not sink.startswith('alsa_output.'):
                raise ValueError(
                    f'Bit-depth override only supports local ALSA outputs; current default is "{sink}".'
                )
            caps = node_capabilities(_dump(), sink)
            if not format_supported(caps, audio_format):
                raise ValueError(f'{audio_format} not supported by "{sink}".')
            content = '\n'.join([
                MARKER.rstrip(),
                'monitor.alsa.rules = [',
                '  {',
                '    matches = [',
                f'      {{ node.name = "{sink}" }}',
                '    ]',
                '    actions = {',
                '      update-props = {',
                f'        audio.format = "{audio_format}"',
                '      }',
                '    }',
                '  }',
                ']',
                '',
            ])
            temp_name = f'.audio-patchbay-{os.getpid()}.tmp'
            fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                         dir_fd=dir_fd)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.rename(temp_name, BIT_CONFIG_NAME, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            except Exception:
                try:
                    os.unlink(temp_name, dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
                raise
    finally:
        os.close(dir_fd)
    run(['systemctl', '--user', 'restart', 'wireplumber'])


# ------------------------------------------------------------------ status -

def hardware():
    rows = []
    for path in sorted(Path('/proc/asound').glob('card*/stream*')):
        try:
            text = path.read_text()
        except OSError:
            continue
        if not text.strip():
            continue
        rates = sorted(set(re.findall(r'Momentary freq = (\d+) Hz', text)))
        bits = sorted(set(re.findall(r'Bits: (\d+)', text)))
        if rates or bits:
            rows.append({'device': text.splitlines()[0], 'rates': rates, 'bits': bits})
    return rows


def status(objs):
    settings = metadata()
    try:
        sink = default_sink_name()
    except Exception:
        sink = ''
    bit_rule = ''
    try:
        dir_fd = _config_dir_fd(create=False)
        if dir_fd is not None:
            try:
                content = _owned_marker_content(dir_fd, BIT_CONFIG_NAME)
            finally:
                os.close(dir_fd)
            if content:
                match = re.search(r'audio\.format\s*=\s*"([^"]+)"', content)
                if match:
                    bit_rule = match.group(1)
    except (ValueError, OSError):
        pass
    capabilities = node_capabilities(objs, sink) if sink else {'rates': [], 'rateRanges': [], 'formats': []}
    return {
        'settings': settings,
        'defaultSink': sink,
        'bitRule': bit_rule,
        'hardware': hardware(),
        'capabilities': capabilities,
    }


def snapshot():
    objs = _dump()
    return {'graph': graph_snapshot(objs), 'status': status(objs)}


# --------------------------------------------------------------------- cli -

def main():
    action = sys.argv[1] if len(sys.argv) > 1 else 'json'
    if action == 'json':
        print(json.dumps(snapshot()))
    elif action == 'connect':
        connect(sys.argv[2], sys.argv[3])
    elif action == 'disconnect':
        disconnect(sys.argv[2])
    elif action == 'rate':
        apply_rate(sys.argv[2])
    elif action == 'bitdepth':
        apply_bitdepth(sys.argv[2])
    else:
        raise ValueError('Use json, connect OUT IN, disconnect LINK, rate PRESET, or bitdepth PRESET')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
