"""Local CR Touch calibration panel for an Ender-3 S1. No print commands."""
import argparse
import copy
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import queue
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time

import serial
from serial.tools import list_ports

ROOT = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(32)
BOUNDS = {"xmin": 3.0, "xmax": 198.2, "ymin": 3.0, "ymax": 179.5}
FIXED = {'M115', 'M27', 'M105', 'M503', 'M420 V', 'M420 S0', 'M420 S1',
         'M140 S60', 'M140 S0', 'M84 S600', 'M84', 'G28', 'G90', 'G21',
         'G1 Z10 F300', 'M400', 'M114', 'G29 V4', 'M500'}


def load_settings():
    """Device-specific values stay in an ignored, machine-local file."""
    path = ROOT / 'printer.local.json'
    if not path.exists():
        return {}
    settings = json.loads(path.read_text())
    if not isinstance(settings, dict) or set(settings) - {'expected_uuid', 'serial_port'}:
        raise ValueError('printer.local.json must contain only expected_uuid and optional serial_port.')
    if not isinstance(settings.get('expected_uuid', ''), str):
        raise ValueError('expected_uuid must be a string in printer.local.json.')
    if settings.get('serial_port') is not None and not isinstance(settings['serial_port'], str):
        raise ValueError('serial_port must be a string or null in printer.local.json.')
    return settings


def verify_identity(identity, expected_uuid):
    match = re.search(r'\bUUID:([^\s]+)', identity)
    machine = re.search(r'\bMACHINE_TYPE:(.*?)(?=\s+[A-Z_]+:|$)', identity)
    if (not match or match.group(1).lower() != expected_uuid.lower() or not machine or
            machine.group(1).strip() not in {'Ender-3 S1', 'Ender-3 S1 Pro'} or
            'Cap:AUTOLEVEL:1' not in identity or 'Cap:Z_PROBE:1' not in identity):
        raise RuntimeError('Printer identity or probe capabilities do not match this panel configuration.')


def point(x, y):
    if isinstance(x, bool) or isinstance(y, bool):
        raise ValueError('Enter numeric X and Y coordinates.')
    x, y = float(x), float(y)
    if not (math.isfinite(x) and math.isfinite(y) and
            BOUNDS['xmin'] <= x <= BOUNDS['xmax'] and
            BOUNDS['ymin'] <= y <= BOUNDS['ymax']):
        raise ValueError('Choose a point inside the marked probe area.')
    return round(x, 2), round(y, 2)


def allowed(code):
    if code in FIXED:
        return True
    match = re.fullmatch(r'G30 X([\d.]+) Y([\d.]+)', code)
    if not match:
        return False
    try:
        point(*match.groups())
        return True
    except ValueError:
        return False


def probe_result(text, x, y):
    match = re.search(r'Bed X:\s*([-+\d.]+)\s+Y:\s*([-+\d.]+)\s+Z:\s*([-+\d.]+)', text)
    if not match:
        raise RuntimeError('Printer returned no single-point reading; G30 may be unavailable.')
    rx, ry, z = map(float, match.groups())
    if not all(map(math.isfinite, (rx, ry, z))) or abs(rx-x) > .03 or abs(ry-y) > .03 or abs(z) > 5:
        raise RuntimeError('Unexpected probe result; session stopped for inspection.')
    return z


def mesh_result(text):
    if 'Bilinear Leveling Grid:' not in text or 'Bed Leveling ON' not in text:
        raise RuntimeError('The printer did not return an enabled bilinear mesh.')
    section = text.split('Bilinear Leveling Grid:', 1)[1].split('Subdivided', 1)[0]
    rows = {}
    for line in section.splitlines():
        match = re.fullmatch(r'\s*([0-3])((?:\s+[+-]\d+\.\d+){4})\s*', line)
        if match:
            index = int(match.group(1))
            if index in rows:
                raise RuntimeError('Duplicate row in the returned mesh.')
            rows[index] = list(map(float, match.group(2).split()))
    if set(rows) != {0, 1, 2, 3}:
        raise RuntimeError('Expected a complete 4 × 4 mesh; the previous map is retained.')
    grid = [rows[i] for i in range(4)]
    if any(not math.isfinite(z) or abs(z) > 5 for row in grid for z in row):
        raise RuntimeError('Unexpected mesh heights; the previous map is retained.')
    return grid


class Controller:
    def __init__(self, data_dir=None):
        self.data_dir = Path(data_dir) if data_dir is not None else ROOT / '.local-data'
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.writes = threading.Lock()
        self.jobs = queue.Queue()
        self.ser = None
        self.halted = threading.Event()
        self.alive = True
        self.owns_heat = False
        self.restore_leveling = False
        self.configured = False
        self.last_action = time.monotonic()
        self.last_view = time.monotonic()
        self.last_temperature = 0
        self.state = dict(connected=False, ready=False, busy=False, phase='Start a session to probe',
                          error=None, bed=None, nozzle=None, target=0, session=0, readings=[],
                          front_left=None, session_mesh=None, bounds=BOUNDS, log=[], port=None, scan_progress=None)
        meshes = sorted(self.data_dir.glob('fresh-mesh-*.json'))
        self.state['mesh'] = json.loads(meshes[-1].read_text()) if meshes else None
        self.mesh_path = meshes[-1] if meshes else None
        self.logfile = (self.data_dir / 'printer.log').open('a', buffering=1)

    def update(self, **kw):
        with self.lock:
            self.state.update(kw)

    def log(self, text):
        text = re.sub(r'\bUUID:[^\s]+', 'UUID:[redacted]', text)
        line = dt.datetime.now().astimezone().isoformat(timespec='seconds') + ' ' + text
        self.logfile.write(line + '\n')
        with self.lock:
            self.state['log'] = (self.state['log'] + [line])[-45:]

    def snapshot(self):
        with self.lock:
            self.last_view = time.monotonic()
            meshes = sorted(self.data_dir.glob('fresh-mesh-*.json'))
            if meshes and meshes[-1] != self.mesh_path:
                try:
                    self.state['mesh'] = json.loads(meshes[-1].read_text())
                    self.mesh_path = meshes[-1]
                except (OSError, ValueError):
                    pass  # A mesh file may still be being written.
            return copy.deepcopy(self.state)

    def submit(self, action, data):
        with self.lock:
            if action not in ('prepare', 'probe', 'full_scan', 'end'):
                raise ValueError('Unknown action.')
            if self.state['busy']:
                raise ValueError('A printer operation is already running.')
            if action == 'prepare' and self.state['connected']:
                raise ValueError('End this session before starting a new one.')
            if action == 'full_scan' and self.state['connected'] and not self.state['ready']:
                raise ValueError('Wait for preparation to finish or start a fresh session.')
            if action == 'probe':
                if not self.state['ready']:
                    raise ValueError('Prepare the printer first.')
                data = dict(zip(('x', 'y'), point(data.get('x'), data.get('y'))))
            self.state.update(busy=True, error=None, phase='Starting…')
            self.last_action = time.monotonic()
            self.jobs.put((action, data))

    def emergency(self):
        self.halted.set()
        self.update(ready=False, error='Stop requested. If motion continues, use the printer power switch. Restart the printer before reconnecting.',
                    phase='Emergency stop requested')
        if self.ser and self.ser.is_open:
            with self.writes:
                self.ser.write(b'M112\n')
                self.ser.flush()
            self.log('SEND M112 (firmware has no emergency parser; physical power switch may be needed)')

    def command(self, code, timeout=20, on_line=None):
        if not allowed(code):
            raise ValueError('Command is outside the panel command set.')
        if self.halted.is_set():
            raise RuntimeError('Emergency stop requested; restart the printer.')
        self.log('SEND ' + code)
        with self.writes:
            self.ser.write((code+'\n').encode('ascii'))
            self.ser.flush()
        start = time.monotonic()
        deadline, ceiling = start + timeout, start + max(timeout, 120)
        lines = []
        while time.monotonic() < deadline:
            if self.halted.is_set():
                raise RuntimeError('Emergency stop requested; restart the printer.')
            line = self.ser.readline().decode('utf8', 'replace').strip()
            if not line:
                continue
            lines.append(line)
            self.log(line)
            if line.startswith('echo:busy:'):
                deadline = max(deadline, min(ceiling, time.monotonic()+20))
            if re.search(r'^Error:|^!!|homing failed|probing failed|printer halted|kill\(|Unknown command|^start$', line, re.I):
                raise RuntimeError(line)
            if on_line:
                on_line(line)
            if re.match(r'^ok\b', line):
                return '\n'.join(lines)
        raise TimeoutError('Printer did not complete ' + code)

    def temperature(self):
        text = self.command('M105')
        bed = re.search(r'\bB:\s*([\d.]+)\s*/\s*([\d.]+)', text)
        nozzle = re.search(r'\bT(?:0)?:\s*([\d.]+)\s*/\s*([\d.]+)', text)
        if not bed or not nozzle:
            raise RuntimeError('Cannot read heater status.')
        b, target, n, nt = map(float, bed.groups()+nozzle.groups())
        self.update(bed=b, target=target, nozzle=n)
        self.last_temperature = time.monotonic()
        if b > 70 or nt > 0 or (self.owns_heat and target != 60):
            raise RuntimeError('Heater state changed unexpectedly. Session stopped.')
        return b

    def prepare(self):
        self.halted.clear()
        settings = load_settings()
        expected_uuid = settings.get('expected_uuid', '').strip()
        if not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', expected_uuid):
            raise RuntimeError('Set your printer UUID in printer.local.json before connecting; see README.')
        devices = [p.device for p in list_ports.comports() if p.vid == 6790 and p.pid == 29987]
        requested_port = settings.get('serial_port')
        if requested_port:
            devices = [p for p in devices if p == requested_port]
        if len(devices) != 1:
            raise RuntimeError('Connect the Ender by USB. Exactly one matching USB device is required.')
        port = devices[0]
        if shutil.which('lsof'):
            held = subprocess.run(['lsof', port, port.replace('/cu.', '/tty.')], capture_output=True, text=True)
            if held.stdout.strip():
                raise RuntimeError('Printer USB is busy. Close Cura, Pronterface or the other serial connection.')
        self.ser = serial.Serial(port=None, baudrate=115200, timeout=.25, write_timeout=2, exclusive=True)
        self.ser.dtr = self.ser.rts = False
        self.ser.port = port
        self.ser.open()
        end = time.monotonic()+2
        while time.monotonic() < end:
            line = self.ser.readline().decode('utf8', 'replace').strip()
            if line:
                self.log('BOOT ' + line)
        identity = self.command('M115')
        verify_identity(identity, expected_uuid)
        self.check_idle()
        self.temperature()
        if self.state['target'] > 65:
            raise RuntimeError('Unexpected active bed target. Leave the printer idle before connecting.')
        settings = self.command('M503')
        if not re.search(r'M851 X-31\.80 Y-40\.50 Z', settings):
            raise RuntimeError('Probe offsets differ from the verified configuration. Review travel limits first.')
        mesh = self.command('M420 V')
        self.restore_leveling = 'Bed Leveling ON' in mesh
        self.configured = True
        self.update(connected=True, port=port, phase='Warming bed to 60°C')
        self.owns_heat = True
        self.command('M140 S60')
        stable = None
        deadline = time.monotonic()+600
        while time.monotonic() < deadline:
            b = self.temperature()
            now = time.monotonic()
            if now-self.last_view > 60:
                self.end()
                return
            stable = (stable or now) if 59 <= b <= 61 else None
            seconds = int(now-stable) if stable else 0
            self.update(phase=f'Settling at 60°C · {min(seconds,90)}/90 sec' if stable else 'Warming bed to 60°C')
            if seconds >= 90:
                break
            if self.halted.wait(2):
                raise RuntimeError('Emergency stop requested.')
        else:
            raise TimeoutError('Bed did not stabilize within 10 minutes.')
        self.update(phase='Homing once for this session')
        self.command('G21')
        self.command('G90')
        self.command('M84 S600')
        self.command('G28', 120)
        self.command('M420 S0')
        self.lift()
        with self.lock:
            self.state.update(ready=True, session=self.state['session']+1, front_left=None, session_mesh=None,
                              phase='Ready · select a point to test')

    def check_idle(self):
        if 'Not SD printing' not in self.command('M27'):
            raise RuntimeError('An SD print is active. The panel will not move the printer.')

    def lift(self):
        self.command('G90')
        self.command('G1 Z10 F300')
        self.command('M400', 30)

    def probe(self, data):
        if not self.state['ready']:
            raise ValueError('Prepare a fresh session before probing.')
        x, y = point(data['x'], data['y'])
        self.check_idle()
        b = self.temperature()
        if not 59 <= b <= 61:
            raise RuntimeError('Bed is outside 59–61°C. Start a fresh session.')
        self.update(phase=f'Probing X {x:.2f} · Y {y:.2f}')
        self.command('M420 S0')
        self.lift()
        result = self.command(f'G30 X{x:.2f} Y{y:.2f}', 90)
        z = probe_result(result, x, y)
        with self.lock:
            readings = self.state['readings']
            previous = next((r for r in reversed(readings) if r['session'] == self.state['session'] and
                             r['x'] == x and r['y'] == y), None)
            reading = dict(id=len(readings)+1, session=self.state['session'], x=x, y=y, z=z,
                           bed=b, time=dt.datetime.now().astimezone().isoformat(timespec='seconds'),
                           delta=round(z-previous['z'], 4) if previous else None)
            readings.append(reading)
            if x == BOUNDS['xmin'] and y == BOUNDS['ymin']:
                self.state['front_left'] = dict(reading, source='point')
            self.state['phase'] = 'Reading received · lifting nozzle'
        with (self.data_dir/'readings.jsonl').open('a') as f:
            f.write(json.dumps(reading)+'\n')
        self.lift()
        self.update(phase='Ready · adjust the wheel, then test again')

    def full_scan(self):
        if not self.state['connected']:
            self.prepare()
            if not self.state['ready']:
                return  # Preparation was cancelled when the page closed.
        else:
            if not self.state['ready']:
                raise ValueError('Prepare a fresh session before scanning.')
            self.check_idle()
            if not 59 <= self.temperature() <= 61:
                raise RuntimeError('Bed is outside 59–61°C. Start a fresh session.')
            with self.lock:
                self.state.update(ready=False, session=self.state['session']+1, front_left=None, session_mesh=None,
                                  phase='Homing for the full bed scan')
            self.command('G28', 120)
        self.update(ready=False, front_left=None, session_mesh=None, scan_progress=0, phase='Full bed scan · starting 16 points')

        def progress(line):
            match = re.search(r'Probing mesh point (\d+)/16\.', line)
            if match and 1 <= int(match.group(1)) <= 16:
                number = int(match.group(1))
                self.update(scan_progress=number, phase=f'Full bed scan · probing {number}/16')

        self.command('G29 V4', 240, on_line=progress)
        grid = mesh_result(self.command('M420 V'))
        self.update(phase='Full bed scan · saving mesh')
        self.command('M500')
        self.restore_leveling = True
        self.lift()
        values = [z for row in grid for z in row]
        stamp = dt.datetime.now(dt.timezone.utc)
        summary = dict(mesh_rows_as_reported=grid, min_mm=min(values), max_mm=max(values),
                       range_mm=round(max(values)-min(values), 4), bed_target_c=60,
                       saved_to_eeprom=True, measured_at=stamp.isoformat(timespec='seconds'),
                       source='Point Lab full scan', log='printer.log')
        path = self.data_dir / ('fresh-mesh-'+stamp.strftime('%Y%m%d-%H%M%S-%f')+'.json')
        temporary = path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(summary, indent=2)+'\n')
        temporary.replace(path)
        with self.lock:
            self.state['mesh'] = summary
            self.mesh_path = path
            self.state['session_mesh'] = dict(summary, session=self.state['session'])
            self.state['front_left'] = dict(x=BOUNDS['xmin'], y=BOUNDS['ymin'], z=grid[0][0],
                                           session=self.state['session'], time=summary['measured_at'],
                                           source='full_scan')
        # Point tests use raw heights. End session restores the new mesh's enabled state.
        self.command('M420 S0')
        self.update(ready=True, scan_progress=None,
                    phase=f'Ready · full mesh saved · {summary["range_mm"]:.3f} mm range')

    def end(self):
        heat_off = not self.owns_heat
        try:
            if self.ser and self.ser.is_open:
                if not self.halted.is_set():
                    if self.owns_heat:
                        self.command('M140 S0')
                        self.owns_heat = False
                        heat_off = True
                    if self.configured:
                        self.command('M420 S1' if self.restore_leveling else 'M420 S0')
                        self.command('M84')
        finally:
            if self.ser and self.ser.is_open:
                self.ser.close()
            self.ser = None
            self.owns_heat = self.configured = False
            self.update(connected=False, ready=False, front_left=None, session_mesh=None, scan_progress=None,
                        phase='Session ended · bed heating off' if heat_off and not self.halted.is_set()
                        else 'Stopped · check printer and restart')
            if heat_off:
                self.update(target=0)

    def run(self):
        while self.alive:
            try:
                action, data = self.jobs.get(timeout=.25)
            except queue.Empty:
                with self.lock:
                    if not self.state['connected'] or self.state['busy']:
                        continue
                    now = time.monotonic()
                    if now-self.last_action > 300 or now-self.last_view > 60:
                        action, data = 'end', {}
                        self.update(busy=True)
                    elif now-self.last_temperature > 5:
                        action, data = 'temperature', {}
                    else:
                        continue
            try:
                if action == 'probe':
                    self.probe(data)
                else:
                    getattr(self, action)()
                if action != 'temperature':
                    self.last_action = time.monotonic()
            except Exception as exc:
                self.log('ERROR ' + str(exc))
                self.update(ready=False, error=str(exc))
                # No emergency stop for validation/connection errors before heater or motion control.
                if self.configured:
                    try:
                        self.emergency()
                    except Exception as stop_error:
                        self.log('STOP FAILED ' + str(stop_error))
                try:
                    self.end()
                except Exception as close_error:
                    self.log('CLOSE FAILED ' + str(close_error))
                self.update(error=str(exc) + (' — restart the printer before reconnecting.' if self.halted.is_set() else ''))
                while True:
                    try:
                        self.jobs.get_nowait()
                    except queue.Empty:
                        break
                self.update(busy=False)
            finally:
                if action != 'temperature':
                    self.update(busy=False)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def valid_host(self):
        return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

    def reply(self, status, value, kind='application/json'):
        body = json.dumps(value).encode() if kind == 'application/json' else value.encode()
        self.send_response(status)
        self.send_header('Content-Type', kind + '; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.valid_host():
            return self.reply(403, {'error':'Local access only.'})
        if self.path == '/':
            return self.reply(200, (ROOT/'index.html').read_text().replace('__TOKEN__', TOKEN), 'text/html')
        if self.path == '/api/state':
            return self.reply(200, self.server.controller.snapshot())
        if self.path == '/measurements.js':
            return self.reply(200, (ROOT/'measurements.js').read_text(), 'text/javascript')
        return self.reply(404, {'error':'Not found.'})

    def do_POST(self):
        origin = self.headers.get('Origin')
        valid_origins = (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}')
        if (not self.valid_host() or (origin and origin not in valid_origins) or
            not secrets.compare_digest(self.headers.get('X-Panel-Token', ''), TOKEN) or
            self.headers.get('Content-Type') != 'application/json'):
            return self.reply(403, {'error':'Open the local panel to control the printer.'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size < 0 or size > 4096:
                raise ValueError('Invalid request size.')
            data = json.loads(self.rfile.read(size) or b'{}')
            if not isinstance(data, dict):
                raise ValueError('Expected an object.')
            action = self.path.removeprefix('/api/')
            if action == 'stop':
                self.server.controller.emergency()
            else:
                self.server.controller.submit(action, data)
            self.reply(202, {'ok':True})
        except (ValueError, TypeError) as exc:
            self.reply(400, {'error':str(exc)})
        except Exception as exc:
            self.reply(500, {'error':str(exc)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.controller = Controller()
    worker = threading.Thread(target=server.controller.run, daemon=True)
    worker.start()
    def shutdown(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    print(f'Ender Point Panel: http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    finally:
        c = server.controller
        if c.state['busy']:
            c.emergency()
        c.alive = False
        worker.join(timeout=3)
        c.end()
        server.server_close()


if __name__ == '__main__':
    main()
