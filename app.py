"""
TOC-app — standalone field TOC/log app, shares OM's toc_log database.
Port: 5400
"""

import gzip
import io
import json
import ipaddress
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import calendar
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, render_template, request, send_file

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
OM_PREFS_DB = os.environ.get(
    'TOC_APP_DB',
    os.environ.get(
        'LOG_APP_DB',
        os.path.expanduser('~/overmesh/overmesh_prefs.db')
    )
)
OM_BASE_URL = os.environ.get('OM_BASE_URL', 'http://localhost:8082')
PORT = int(os.environ.get('TOC_APP_PORT', os.environ.get('LOG_APP_PORT', 5400)))
MBTILES_DIR = Path(os.environ.get('TOC_APP_MBTILES_DIR',
                   os.environ.get('LOG_APP_MBTILES_DIR',
                   os.path.expanduser('~/maps/mbtiles'))))

VALID_CATEGORIES = {
    'NOTE', 'PLAN', 'SITREP', 'ALERT', 'ACTION',
    'COMMS', 'CONTACT', 'POSITION', 'INTEL', 'WEATHER',
}

_MISSION_RE = re.compile(r'\*\*(?:Mission|Mission / Folder):\*\*\s*(.+)', re.I)
_POS_RE     = re.compile(r'\*\*GPS:\*\*\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)', re.I)

_UPDATE_LOCK = threading.Lock()
_UPDATE_STATE = {
    'running': False,
    'ok': None,
    'message': '',
    'log': [],
    'updated_at': None,
}
_UPDATE_STATUS_IGNORED_PATHS = {'__pycache__'}


# ── DB ────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(OM_PREFS_DB)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    """Create toc_log if OM hasn't initialised the DB yet."""
    with get_db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS toc_log (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       INTEGER NOT NULL,
            category TEXT NOT NULL DEFAULT 'NOTE',
            body     TEXT NOT NULL
        )''')


def _norm_cat(v):
    c = (v or 'NOTE').strip().upper()
    return c if c in VALID_CATEGORIES else 'NOTE'


def _norm_ts(v):
    if v in (None, ''):
        return int(time.time())
    try:
        ts = int(float(v))
    except (TypeError, ValueError):
        return int(time.time())
    if ts > 10_000_000_000:
        ts = ts // 1000
    return max(0, ts)


def _row_to_dict(r):
    return {'id': r['id'], 'ts': r['ts'], 'category': r['category'], 'body': r['body']}


def _annotate(e):
    m = _MISSION_RE.search(e['body'] or '')
    e['mission'] = m.group(1).strip() if m else None
    p = _POS_RE.search(e['body'] or '')
    if p:
        e['lat'] = float(p.group(1))
        e['lon'] = float(p.group(2))
    return e


def _local_request():
    addr = request.remote_addr or ''
    if addr == 'localhost':
        return True
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    if ip.version == 4:
        trusted = (
            ipaddress.ip_network('10.0.0.0/8'),
            ipaddress.ip_network('172.16.0.0/12'),
            ipaddress.ip_network('192.168.0.0/16'),
            ipaddress.ip_network('100.64.0.0/10'),
        )
        return any(ip in net for net in trusted)
    return ip.is_private or ip.is_link_local


def _git_cmd(args, timeout=30, check=False):
    result = subprocess.run(
        ['git', *args],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    out = (result.stdout or '').strip()
    err = (result.stderr or '').strip()
    if check and result.returncode != 0:
        raise RuntimeError(err or out or f"git {' '.join(args)} failed")
    return result.returncode, out, err


def _git_status_path(line):
    if not line or len(line) < 4:
        return ''
    path = line[3:].strip()
    if ' -> ' in path:
        path = path.rsplit(' -> ', 1)[-1]
    if len(path) >= 2 and path[0] == path[-1] == '"':
        path = path[1:-1]
    return path


def _filter_update_status_lines(status):
    lines = status.splitlines() if status else []
    return [line for line in lines if _git_status_path(line) not in _UPDATE_STATUS_IGNORED_PATHS]


def _app_version():
    for name in ('VERSION', 'version.txt'):
        try:
            value = (BASE_DIR / name).read_text(encoding='utf-8').strip()
            if value:
                return value
        except OSError:
            pass
    return '0.0.0'


def _update_append(line):
    with _UPDATE_LOCK:
        _UPDATE_STATE['log'].append(line)
        _UPDATE_STATE['log'] = _UPDATE_STATE['log'][-80:]
        _UPDATE_STATE['updated_at'] = int(time.time())


def _git_info(fetch=False):
    if not (BASE_DIR / '.git').is_dir():
        return {'managed': False, 'error': 'This install is not a Git checkout.'}

    info = {'managed': True}
    _, branch, _ = _git_cmd(['rev-parse', '--abbrev-ref', 'HEAD'], timeout=10)
    _, commit, _ = _git_cmd(['rev-parse', '--short', 'HEAD'], timeout=10)
    _, full_commit, _ = _git_cmd(['rev-parse', 'HEAD'], timeout=10)
    _, remote, _ = _git_cmd(['config', '--get', 'remote.origin.url'], timeout=10)
    info.update({
        'version': _app_version(),
        'branch': branch or 'unknown',
        'commit': commit or 'unknown',
        'full_commit': full_commit or '',
        'remote': remote or '',
    })

    rc, status, _ = _git_cmd(['status', '--porcelain'], timeout=10)
    status_lines = _filter_update_status_lines(status) if rc == 0 else []
    info['dirty'] = bool(status_lines) if rc == 0 else True
    info['dirty_summary'] = status_lines[:12]

    if fetch:
        frc, fout, ferr = _git_cmd(['fetch', '--prune', 'origin'], timeout=45)
        info['fetch_ok'] = frc == 0
        if frc != 0:
            info['fetch_error'] = ferr or fout or 'Fetch failed.'

    upstream = 'origin/main'
    rc, remote_commit, _ = _git_cmd(['rev-parse', '--short', upstream], timeout=10)
    if rc == 0 and remote_commit:
        info['remote_commit'] = remote_commit
        rc, counts, _ = _git_cmd(['rev-list', '--left-right', '--count', f'HEAD...{upstream}'], timeout=10)
        if rc == 0 and counts:
            parts = counts.split()
            if len(parts) == 2:
                info['ahead'] = int(parts[0])
                info['behind'] = int(parts[1])
                info['update_available'] = info['behind'] > 0
    else:
        info['remote_commit'] = None
        info['update_available'] = False
    return info


def _run_update_job():
    with _UPDATE_LOCK:
        _UPDATE_STATE.update({
            'running': True,
            'ok': None,
            'message': 'Updating...',
            'log': [],
            'updated_at': int(time.time()),
        })
    try:
        _update_append('Checking repository state...')
        info = _git_info(fetch=True)
        if not info.get('managed'):
            raise RuntimeError(info.get('error') or 'Not a Git checkout.')
        if info.get('ahead', 0) > 0:
            raise RuntimeError('Local commits are ahead of origin. Push or reconcile before updating.')
        if not info.get('update_available'):
            with _UPDATE_LOCK:
                _UPDATE_STATE.update({'running': False, 'ok': True, 'message': 'Already up to date.'})
            _update_append('Already up to date.')
            return

        rc, changed, _ = _git_cmd(['diff', '--name-only', 'HEAD', 'origin/main'], timeout=15)
        changed_files = set(changed.splitlines()) if rc == 0 and changed else set()

        if info.get('dirty'):
            _update_append('Local changes detected - stashing before update...')
            _git_cmd(['stash', '--include-untracked'], timeout=15)

        _update_append('Resetting to origin/main...')
        _git_cmd(['reset', '--hard', 'origin/main'], timeout=60, check=True)

        if 'requirements.txt' in changed_files:
            _update_append('requirements.txt changed; installing Python dependencies...')
            pip_cmd = [sys.executable or 'python3', '-m', 'pip', 'install', '-r', 'requirements.txt']
            if sys.prefix == getattr(sys, 'base_prefix', sys.prefix):
                pip_cmd.append('--user')
            pip = subprocess.run(
                pip_cmd,
                cwd=BASE_DIR,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            if pip.returncode != 0 and 'externally-managed-environment' in (pip.stderr or ''):
                _update_append('System Python blocks user installs; retrying with --break-system-packages...')
                pip = subprocess.run(
                    pip_cmd + ['--break-system-packages'],
                    cwd=BASE_DIR,
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )
            if pip.returncode != 0:
                raise RuntimeError((pip.stderr or pip.stdout or 'pip install failed').strip())
            _update_append('Dependencies updated.')

        final = _git_info(fetch=False)
        with _UPDATE_LOCK:
            _UPDATE_STATE.update({
                'running': False,
                'ok': True,
                'message': f"Updated to {final.get('commit', 'latest')}. Restart required.",
            })
        _update_append('Update complete. Restart required.')
    except Exception as e:
        with _UPDATE_LOCK:
            _UPDATE_STATE.update({
                'running': False,
                'ok': False,
                'message': str(e),
                'updated_at': int(time.time()),
            })
        _update_append(f'Error: {e}')


# ── Routes ────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
def api_status():
    try:
        with get_db() as conn:
            n = conn.execute('SELECT COUNT(*) FROM toc_log').fetchone()[0]
        return jsonify({'ok': True, 'entries': n})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/entries')
def api_entries():
    cat    = request.args.get('category', '').upper()
    miss   = request.args.get('mission', '')
    search = request.args.get('search', '')
    try:
        limit = min(int(request.args.get('limit', 500)), 2000)
    except (ValueError, TypeError):
        limit = 500

    with get_db() as conn:
        rows = conn.execute(
            'SELECT id, ts, category, body FROM toc_log ORDER BY ts DESC LIMIT ?',
            (limit,)
        ).fetchall()

    entries = [_annotate(_row_to_dict(r)) for r in rows]

    if cat and cat != 'ALL':
        entries = [e for e in entries if e['category'] == cat]
    if miss:
        entries = [e for e in entries
                   if (e.get('mission') or '').lower() == miss.lower()]
    if search:
        s = search.lower()
        entries = [e for e in entries if s in e['body'].lower()]

    return jsonify(entries)


@app.route('/api/entries', methods=['POST'])
def api_entries_add():
    d    = request.get_json(silent=True) or {}
    body = (d.get('body') or '').strip()
    if not body:
        return jsonify({'error': 'Body required'}), 400
    cat = _norm_cat(d.get('category'))
    ts  = _norm_ts(d.get('ts'))
    with get_db() as conn:
        cur = conn.execute(
            'INSERT INTO toc_log (ts, category, body) VALUES (?, ?, ?)',
            (ts, cat, body)
        )
        eid = cur.lastrowid
    return jsonify({'ok': True, **_annotate({'id': eid, 'ts': ts, 'category': cat, 'body': body})})


@app.route('/api/entries/<int:eid>', methods=['PUT', 'PATCH'])
def api_entries_update(eid):
    d    = request.get_json(silent=True) or {}
    body = (d.get('body') or '').strip()
    if not body:
        return jsonify({'error': 'Body required'}), 400
    cat = _norm_cat(d.get('category'))
    ts  = _norm_ts(d.get('ts'))
    with get_db() as conn:
        cur = conn.execute(
            'UPDATE toc_log SET ts=?, category=?, body=? WHERE id=?',
            (ts, cat, body, eid)
        )
        if cur.rowcount == 0:
            return jsonify({'error': 'Not found'}), 404
    return jsonify({'ok': True, **_annotate({'id': eid, 'ts': ts, 'category': cat, 'body': body})})


@app.route('/api/entries/<int:eid>', methods=['DELETE'])
def api_entries_delete(eid):
    with get_db() as conn:
        cur = conn.execute('DELETE FROM toc_log WHERE id=?', (eid,))
        if cur.rowcount == 0:
            return jsonify({'error': 'Not found'}), 404
    return jsonify({'ok': True})


@app.route('/api/missions')
def api_missions():
    with get_db() as conn:
        rows = conn.execute('SELECT ts, category, body FROM toc_log').fetchall()
    missions = {}
    for r in rows:
        m = _MISSION_RE.search(r['body'] or '')
        if m:
            name = m.group(1).strip()
            key = name.lower()
            cur = missions.setdefault(key, {
                'name': name,
                'count': 0,
                'last_ts': 0,
                'categories': {},
            })
            cur['count'] += 1
            cur['last_ts'] = max(cur['last_ts'], int(r['ts'] or 0))
            cat = r['category'] or 'NOTE'
            cur['categories'][cat] = cur['categories'].get(cat, 0) + 1
    return jsonify(sorted(missions.values(), key=lambda x: (-x['last_ts'], x['name'].lower())))


@app.route('/api/missions/rename', methods=['PUT'])
def api_missions_rename():
    d = request.get_json(silent=True) or {}
    old = (d.get('old_name') or '').strip()
    new = (d.get('new_name') or '').strip()
    if not old or not new:
        return jsonify({'error': 'old_name and new_name required'}), 400
    with get_db() as conn:
        rows = conn.execute('SELECT id, body FROM toc_log').fetchall()
        updated = 0
        for r in rows:
            body = r['body'] or ''
            m = _MISSION_RE.search(body)
            if m and m.group(1).strip() == old:
                new_body = _MISSION_RE.sub(f'**Mission / Folder:** {new}', body, count=1)
                conn.execute('UPDATE toc_log SET body=? WHERE id=?', (new_body, r['id']))
                updated += 1
    return jsonify({'ok': True, 'updated': updated})


@app.route('/api/missions/delete', methods=['POST'])
def api_missions_delete():
    d = request.get_json(silent=True) or {}
    name = (d.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400
    with get_db() as conn:
        rows = conn.execute('SELECT id, body FROM toc_log').fetchall()
        updated = 0
        for r in rows:
            body = r['body'] or ''
            m = _MISSION_RE.search(body)
            if m and m.group(1).strip() == name:
                new_body = _MISSION_RE.sub('', body).lstrip('\n')
                conn.execute('UPDATE toc_log SET body=? WHERE id=?', (new_body, r['id']))
                updated += 1
    return jsonify({'ok': True, 'updated': updated})


@app.route('/api/stats')
def api_stats():
    with get_db() as conn:
        rows = conn.execute(
            'SELECT category, COUNT(*) as n FROM toc_log GROUP BY category ORDER BY n DESC'
        ).fetchall()
    return jsonify([{'category': r['category'], 'count': r['n']} for r in rows])


@app.route('/api/gps')
def api_gps():
    try:
        r = requests.get(f'{OM_BASE_URL}/api/settings/gps', timeout=2)
        d = r.json()
        return jsonify({
            'lat': d.get('lat'), 'lon': d.get('lon'),
            'alt': d.get('alt'), 'fix': d.get('fix', False),
            'sats': d.get('sats', 0), 'source': 'om',
        })
    except Exception:
        return jsonify({'fix': False, 'source': 'unavailable'})


@app.route('/api/export')
def api_export():
    fmt = request.args.get('fmt', 'txt')
    with get_db() as conn:
        rows = conn.execute(
            'SELECT id, ts, category, body FROM toc_log ORDER BY ts ASC'
        ).fetchall()
    entries = [_row_to_dict(r) for r in rows]
    if fmt == 'json':
        return Response(
            json.dumps(entries, indent=2),
            mimetype='application/json',
            headers={'Content-Disposition': 'attachment; filename="field_log.json"'}
        )
    lines = []
    for e in entries:
        dt = time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(e['ts']))
        lines.append(f"[{dt}] [{e['category']}]\n{e['body']}\n")
    return Response(
        '\n'.join(lines), mimetype='text/plain',
        headers={'Content-Disposition': 'attachment; filename="field_log.txt"'}
    )


@app.route('/api/import', methods=['POST'])
def api_import():
    if request.files:
        upload = request.files.get('file')
        raw = upload.read().decode('utf-8', errors='replace') if upload else ''
    else:
        d   = request.get_json(silent=True) or {}
        raw = d.get('data', '')
    entries = []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict) and item.get('body'):
                    entries.append({
                        'ts': _norm_ts(item.get('ts')),
                        'category': _norm_cat(item.get('category')),
                        'body': str(item.get('body', '')).strip(),
                    })
    except (json.JSONDecodeError, TypeError):
        _TXT_RE = re.compile(
            r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?Z?)\] \[([A-Z]+)\]\n(.*?)(?=\n\n\[|\Z)',
            re.S | re.M
        )
        for m in _TXT_RE.finditer(raw):
            try:
                dt_raw = m.group(1)
                struct = time.strptime(dt_raw.rstrip('Z')[:19], '%Y-%m-%d %H:%M:%S')
                ts = calendar.timegm(struct) if dt_raw.endswith('Z') else int(time.mktime(struct))
            except Exception:
                ts = int(time.time())
            entries.append({
                'ts': ts,
                'category': _norm_cat(m.group(2)),
                'body': m.group(3).strip(),
            })
    if not entries:
        return jsonify({'error': 'No importable entries found'}), 400
    with get_db() as conn:
        for e in entries:
            conn.execute('INSERT INTO toc_log (ts, category, body) VALUES (?,?,?)',
                         (e['ts'], e['category'], e['body']))
    return jsonify({'ok': True, 'imported': len(entries)})


# ── Tiles ─────────────────────────────────────────────────────────────
def _list_mbtiles():
    layers = []
    if not MBTILES_DIR.exists():
        return layers
    for path in sorted(MBTILES_DIR.glob('*.mbtiles')):
        layer_id = path.stem.replace(' ', '-').lower()
        name = path.stem.replace('_', ' ').replace('-', ' ').title()
        meta = {}
        try:
            with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as c:
                meta = {r[0]: r[1] for r in c.execute('SELECT name,value FROM metadata').fetchall()}
        except sqlite3.Error:
            pass
        layers.append({
            'id': layer_id,
            'name': meta.get('name') or name,
            'type': 'local',
            'url': f'/tiles/{layer_id}/{{z}}/{{x}}/{{y}}.png',
            'minzoom': int(meta.get('minzoom') or 0),
            'maxzoom': int(meta.get('maxzoom') or 18),
            'attribution': meta.get('attribution') or 'Local MBTiles',
        })
    return layers


@app.route('/api/tile-layers')
def api_tile_layers():
    online = [
        {'id': 'osm',              'name': 'OpenStreetMap',    'type': 'online', 'maxzoom': 19,
         'url': 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
         'attribution': '© OpenStreetMap contributors'},
        {'id': 'voyager',          'name': 'Voyager',          'type': 'online', 'maxzoom': 19,
         'url': 'https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png',
         'attribution': '© OpenStreetMap contributors © CARTO'},
        {'id': 'voyager_nolabels', 'name': 'Voyager No Labels','type': 'online', 'maxzoom': 19,
         'url': 'https://{s}.basemaps.cartocdn.com/rastertiles/voyager_nolabels/{z}/{x}/{y}{r}.png',
         'attribution': '© OpenStreetMap contributors © CARTO'},
        {'id': 'positron',         'name': 'Positron',         'type': 'online', 'maxzoom': 19,
         'url': 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
         'attribution': '© OpenStreetMap contributors © CARTO'},
        {'id': 'dark_matter',      'name': 'Dark Matter',      'type': 'online', 'maxzoom': 19,
         'url': 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
         'attribution': '© OpenStreetMap contributors © CARTO'},
        {'id': 'dark_nolabels',    'name': 'Dark No Labels',   'type': 'online', 'maxzoom': 19,
         'url': 'https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png',
         'attribution': '© OpenStreetMap contributors © CARTO'},
        {'id': 'esri_gray_dark',   'name': 'Esri Dark Gray',   'type': 'online', 'maxzoom': 16,
         'url': 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}',
         'attribution': '© Esri, HERE, Garmin, © OpenStreetMap contributors'},
        {'id': 'stamen_toner_lite','name': 'Toner Lite',       'type': 'online', 'maxzoom': 20,
         'url': 'https://tiles.stadiamaps.com/tiles/stamen_toner_lite/{z}/{x}/{y}{r}.png',
         'attribution': '© Stadia Maps © Stamen Design © OpenMapTiles © OpenStreetMap'},
        {'id': 'stamen_toner_dark','name': 'Toner Dark',       'type': 'online', 'maxzoom': 20,
         'url': 'https://tiles.stadiamaps.com/tiles/stamen_toner_dark/{z}/{x}/{y}{r}.png',
         'attribution': '© Stadia Maps © Stamen Design © OpenMapTiles © OpenStreetMap'},
        {'id': 'stamen_terrain',   'name': 'Stamen Terrain',   'type': 'online', 'maxzoom': 20,
         'url': 'https://tiles.stadiamaps.com/tiles/stamen_terrain/{z}/{x}/{y}{r}.png',
         'attribution': '© Stadia Maps © Stamen Design © OpenMapTiles © OpenStreetMap'},
        {'id': 'esri_sat',         'name': 'Esri Satellite',   'type': 'online', 'maxzoom': 18,
         'url': 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
         'attribution': '© Esri'},
        {'id': 'esri_streets',     'name': 'Esri Streets',     'type': 'online', 'maxzoom': 19,
         'url': 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',
         'attribution': '© Esri, DeLorme, NAVTEQ, USGS'},
        {'id': 'esri_topo',        'name': 'Esri Topo',        'type': 'online', 'maxzoom': 18,
         'url': 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}',
         'attribution': '© Esri'},
        {'id': 'stadia_outdoors',  'name': 'Stadia Outdoors',  'type': 'online', 'maxzoom': 20,
         'url': 'https://tiles.stadiamaps.com/tiles/outdoors/{z}/{x}/{y}{r}.png',
         'attribution': '© Stadia Maps © OpenMapTiles © OpenStreetMap'},
        {'id': 'esri_hillshade',   'name': 'Esri Hillshade',   'type': 'online', 'maxzoom': 16,
         'url': 'https://server.arcgisonline.com/ArcGIS/rest/services/Elevation/World_Hillshade/MapServer/tile/{z}/{y}/{x}',
         'attribution': '© Esri, Airbus DS, USGS, NGA, NASA'},
    ]
    return jsonify(_list_mbtiles() + online)


@app.route('/tiles/<layer_id>/<int:z>/<int:x>/<int:y>.png')
def serve_tile(layer_id, z, x, y):
    path = MBTILES_DIR / f'{layer_id}.mbtiles'
    if not path.exists():
        # try case-insensitive match
        matches = list(MBTILES_DIR.glob(f'*.mbtiles')) if MBTILES_DIR.exists() else []
        path = next((p for p in matches
                     if p.stem.replace(' ', '-').lower() == layer_id), None)
        if not path:
            return Response(status=204)
    y_tms = (2 ** z - 1) - y
    try:
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as c:
            row = c.execute(
                'SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?',
                (z, x, y_tms)
            ).fetchone()
        if not row:
            return Response(status=204)
        data = row[0]
        if data[:2] == b'\x1f\x8b':
            data = gzip.decompress(data)
        mime = 'image/webp' if data[:4] == b'RIFF' else 'image/png'
        return send_file(io.BytesIO(data), mimetype=mime)
    except sqlite3.Error:
        return Response(status=204)


@app.route('/api/settings/update/status')
@app.route('/api/system/update/status')
def api_update_status():
    if not _local_request():
        return jsonify({'error': 'Updater is only available from the local machine.'}), 403
    fetch = request.args.get('fetch') in ('1', 'true', 'yes')
    try:
        info = _git_info(fetch=fetch)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    with _UPDATE_LOCK:
        state = dict(_UPDATE_STATE)
    return jsonify({'info': info, 'state': state})


@app.route('/api/settings/update/run', methods=['POST'])
@app.route('/api/system/update/run', methods=['POST'])
def api_update_run():
    if not _local_request():
        return jsonify({'error': 'Updater is only available from the local machine.'}), 403
    with _UPDATE_LOCK:
        if _UPDATE_STATE.get('running'):
            return jsonify({'error': 'Update already running.', 'state': dict(_UPDATE_STATE)}), 409
        _UPDATE_STATE.update({
            'running': True,
            'ok': None,
            'message': 'Starting update...',
            'log': ['Starting update...'],
            'updated_at': int(time.time()),
        })
    threading.Thread(target=_run_update_job, daemon=True).start()
    return jsonify({'ok': True, 'state': dict(_UPDATE_STATE)})


def _systemctl_user(action):
    uid = os.getuid()
    env = {**os.environ, 'XDG_RUNTIME_DIR': f'/run/user/{uid}'}
    time.sleep(0.5)
    subprocess.run(['systemctl', '--user', action, 'log-app'], env=env)


@app.route('/api/system/restart', methods=['POST'])
def system_restart():
    threading.Thread(target=_systemctl_user, args=('restart',), daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/system/stop', methods=['POST'])
def system_stop():
    threading.Thread(target=_systemctl_user, args=('stop',), daemon=True).start()
    return jsonify({'ok': True})


if __name__ == '__main__':
    _init_db()
    app.run(host='0.0.0.0', port=PORT, debug=False)
