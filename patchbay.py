#!/usr/bin/env python3
"""Backend for the Audio Patchbay bar widget. No external Python dependencies.

Talks to PipeWire/WirePlumber through pw-dump, pw-link, pw-metadata and
pactl. All state-changing actions are reversible: rate/quantum overrides
clear back to "auto" via pw-metadata, and the bit-depth rule is a single
owned file removed by its own "auto" preset.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

RATE_PRESETS = {
    'auto': (0, 0),
    'music': (44100, 1024),
    'studio': (48000, 256),
    'hires': (96000, 512),
    'ultra': (192000, 1024),
    'extreme': (384000, 2048),
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


def _config_root():
    configured = os.environ.get('XDG_CONFIG_HOME')
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_absolute():
            return candidate
    return Path.home() / '.config'


BIT_CONFIG = _config_root() / 'wireplumber/wireplumber.conf.d/70-audio-patchbay-bitdepth.conf'


def run(args):
    return subprocess.run(args, text=True, capture_output=True, check=True, timeout=8,
                           env={**os.environ, 'LC_ALL': 'C'}).stdout


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

def _owned_content(path):
    if path.is_symlink():
        raise ValueError(f'Refusing to touch an unowned file: {path}')
    try:
        content = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise ValueError(f'Refusing to touch an unowned file: {path}') from exc
    if not content.startswith(MARKER):
        raise ValueError(f'Refusing to touch an unowned file: {path}')
    return content


def default_sink_name():
    return run(['pactl', 'get-default-sink']).strip()


def apply_bitdepth(preset):
    if preset not in BIT_PRESETS:
        raise ValueError(f'Unknown bit-depth preset: {preset}')
    _owned_content(BIT_CONFIG)  # validates ownership before any write/removal
    audio_format = BIT_PRESETS[preset]
    if audio_format is None:
        BIT_CONFIG.unlink(missing_ok=True)
    else:
        sink = default_sink_name()
        if not sink.startswith('alsa_output.'):
            raise ValueError(
                f'Bit-depth override only supports local ALSA outputs; current default is "{sink}".'
            )
        caps = node_capabilities(_dump(), sink)
        if not format_supported(caps, audio_format):
            raise ValueError(f'{audio_format} not supported by "{sink}".')
        lines = [
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
        ]
        BIT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=BIT_CONFIG.parent, prefix='.audio-patchbay-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write('\n'.join(lines))
            os.replace(temp, BIT_CONFIG)
        finally:
            Path(temp).unlink(missing_ok=True)
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
        content = BIT_CONFIG.read_text(encoding='utf-8')
        if content.startswith(MARKER):
            match = re.search(r'audio\.format\s*=\s*"([^"]+)"', content)
            if match:
                bit_rule = match.group(1)
    except (FileNotFoundError, OSError, UnicodeError):
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
