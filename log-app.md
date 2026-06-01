type:: project
status:: active
tags:: #toc-app #toc #field-log #cyberdeck
updated:: 2026-06-01

# TOC-app

> Project name: TOC-app. In-app name: Field Log. Standalone Flask TOC/log app sharing OM's `toc_log` database. Log entries outside of OM context, with map, GPS, structured templates, and mission/folder management.
> Auto-synced to Logseq · managed by Claude/Codex · source: Projects/log-app/log-app.md

## State

| **Label** | Value |
|-----------|-------|
| Status | Active — running on CD; Codex style/bug/update pass applied 2026-06-01 |
| Project name | TOC-app |
| In-app name | Field Log |
| Port | 5400 |
| Host | Cyberdeck (Rock 5B, 100.97.104.107) |
| Database | Shared — `~/overmesh/overmesh_prefs.db` (table: `toc_log`) |
| Venv | `~/Projects/log-app/venv/` |
| Service | `log-app.service` (current deployed compatibility name; project name is TOC-app) |

## Access

| | |
|--|--|
| UI | `http://localhost:5400` (via Launcher tile) |
| SSH | `ssh slofi@100.97.104.107` |
| Service | `systemctl --user start/stop/restart log-app` |

## Quick Commands

Start manually:
```bash
cd ~/Projects/log-app && venv/bin/python app.py
```

Check logs:
```bash
journalctl --user -u log-app -f
```

Install (first time on CD):
```bash
cd ~/Projects/log-app
python3 -m venv venv
venv/bin/pip install -r requirements.txt
mkdir -p ~/.config/systemd/user
cp log-app.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable log-app.service
systemctl --user start log-app.service
```

## Key Paths

| | |
|--|--|
| App | `~/Projects/log-app/app.py` |
| Template | `~/Projects/log-app/templates/index.html` |
| CSS | `~/Projects/log-app/static/style.css` |
| Service | `~/Projects/log-app/log-app.service` |
| GitHub | `https://github.com/Slofi/TOC-app.git` |
| Shared DB | `~/overmesh/overmesh_prefs.db` |
| MBTiles | `~/maps/mbtiles/` (same as Map-App) |

## Pending

- Add link in OM's Log tab header pointing to :5400
- Optional later cleanup: rename folder/service from `log-app` to `toc-app` during a maintenance window. Do not do this while relying on the current launcher/service path.

## Changelog

- **2026-06-01** — Codex pass: project renamed in notes/master list to **TOC-app** while keeping the visible in-app name **Field Log**. Restyled toward the current black/dark-grey/gold UI with accent `#e8b04f`. Added a dedicated Missions tab for Mission / Folder view/filter/rename/remove-tag management. Added OM-style in-app updater controls in the settings panel with Check/Update/Restart status flow. Fixed Mission/Mission Folder parsing compatibility, safer mission inline handlers, duplicate-entry action, DELETE 404 for missing entries, TXT import UTC handling, backend file-upload import support, and kept Restart/Shutdown targeting deployed `log-app.service`.
- **2026-06-01** — GitHub remote set for updater and initial app state pushed to `https://github.com/Slofi/TOC-app.git` (`main`, version `0.1.0`).
- **2026-06-01** — Bug sweep: added missing `closeBurger()` (ReferenceError on Restart/Shutdown/SetPos/PickPos), fixed `data-mission` attr on chips (mission highlight broken), removed duplicate `First Heard` field from CONTACT template, fixed 3× `var(--muted)` → `var(--text-dim)` in CSS, removed duplicate `display:none` in burger CSS, added try/except around `limit` param in `api_entries`
- **2026-06-01** — Initial build: Flask backend, dark/amber UI, LOG/MAP/EXPORT tabs, structured field templates matching OM, map with full layer set (matches OM), manual position, mission management (rename/delete), Restart + Shutdown, +GPS, Now button. Launcher tile added to CD dashboard.

---
---
# ////// FULL REFERENCE //////

## Architecture

- **Shared DB**: Both OM and TOC-app / Field Log open `overmesh_prefs.db` directly in WAL mode — no sync, no API dependency, works when OM is offline. Both read/write the same `toc_log` table.
- **No OM dependency**: App works standalone. GPS proxied from OM (`/api/settings/gps`), falls back gracefully if OM is offline.
- **Tile serving**: Log app reads MBTiles directly from `~/maps/mbtiles/` (same directory as Map-App) via its own `/tiles/<id>/<z>/<x>/<y>.png` route. No dependency on Map-App or mbtileserver.

## DB Schema

```sql
CREATE TABLE toc_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    category TEXT NOT NULL DEFAULT 'NOTE',
    body     TEXT NOT NULL
);
```

Body format: `**Key:** value` markdown. Mission and GPS embedded:
- `**Mission / Folder:** <name>` — first line if set (`**Mission:**` is still parsed for compatibility)
- `**GPS:** <lat>, <lon>` — appended when +GPS used

## Categories

NOTE, PLAN, SITREP, ALERT, ACTION, COMMS, CONTACT, POSITION, INTEL, WEATHER

WEATHER is TOC-app-only (OM doesn't have it). If edited in OM, OM normalizes it to NOTE.

## API Routes

| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Main UI |
| `/api/status` | GET | DB health + entry count |
| `/api/entries` | GET | List entries (filter: category, mission, search, limit) |
| `/api/entries` | POST | Add entry |
| `/api/entries/<id>` | PUT/PATCH | Update entry |
| `/api/entries/<id>` | DELETE | Delete entry |
| `/api/missions` | GET | List missions with counts, last timestamp, and category breakdown |
| `/api/missions/rename` | PUT | Rename mission across all entries |
| `/api/missions/delete` | POST | Remove mission tag from all entries |
| `/api/stats` | GET | Entry counts per category |
| `/api/gps` | GET | GPS proxy from OM |
| `/api/tile-layers` | GET | Available tile layers (local MBTiles + online) |
| `/tiles/<id>/<z>/<x>/<y>.png` | GET | Serve MBTiles tile |
| `/api/export` | GET | Export as JSON or TXT |
| `/api/import` | POST | Import JSON or TXT |
| `/api/system/restart` | POST | Restart via systemctl |
| `/api/system/stop` | POST | Stop via systemctl |
| `/api/settings/update/status` | GET | OM-style Git updater status/check |
| `/api/settings/update/run` | POST | OM-style Git updater run |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TOC_APP_DB` / `LOG_APP_DB` | `~/overmesh/overmesh_prefs.db` | Path to SQLite DB |
| `OM_BASE_URL` | `http://localhost:8082` | OM base URL for GPS proxy |
| `TOC_APP_PORT` / `LOG_APP_PORT` | `5400` | Listening port |
| `TOC_APP_MBTILES_DIR` / `LOG_APP_MBTILES_DIR` | `~/maps/mbtiles` | MBTiles directory |
