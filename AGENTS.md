# Work Manager — Agent Documentation

## Project Overview

Order management system for a stone countertop fabrication business. Receives orders from multiple channels (Telegram, Email, YouGile, MAX messenger), parses them with LLM, and creates Obsidian notes + working folders on YandexDisk.

**Tech stack**: Python 3.12, FastAPI, aiogram 3, pymax, aiosqlite, Jinja2, Bootstrap 5, instructor/OpenAI.

---

## Architecture

```
run.py                    — Entry point: starts web server + TG bot + MAX bridge
app/
  config.py               — Pydantic Settings from .env
  database.py             — SQLite CRUD for orders + history
  models.py               — Pydantic models (ParsedOrder, OrderRecord, etc.)
  parser.py               — LLM-based message parser (instructor + OpenAI)
  processor.py             — Core pipeline: parse → moderate → create notes
  obsidian.py              — Obsidian vault writer (notes, frontmatter, attachments)
  scanner.py              — Scan services (email, YouGile, Telegram)
  scheduler.py            — Auto-mode periodic scanner
  settings_db.py           — Key-value settings in SQLite (web UI configurable)
  log_stream.py            — SSE log streaming to browser
  yougile.py               — YouGile API client
  channels/
    telegram_bot.py        — aiogram 3 bot (long polling)
    email_checker.py       — IMAP email checker
    max_bridge.py          — MAX → Telegram bridge (pymax WebClient)
  web/
    routes.py              — FastAPI routes (pages + API)
    static/style.css       — CSS
    templates/             — Jinja2 HTML templates
```

---

## Key Concepts

### Order Flow
1. **Ingest**: Message arrives via TG bot / email scan / YouGile scan / MAX bridge / manual entry
2. **Parse**: LLM extracts `order_code`, `owner`, `description`, `thickness`, etc.
3. **Custom fields**: Per-client parameters parsed from text (e.g., "борт" → `bort: "да"`)
4. **Pending**: Order created in DB with status `pending` (moderation queue)
5. **Confirm**: User confirms → Obsidian note + YandexDisk working folder created
6. **Track**: Status progresses: `pending → in_progress → ready → done`

### Client System
Clients are configured in Settings → Clients. Each client has:
- `code` — short identifier (KK, AM, KH, YG, БЕЗ)
- `prefixes` — order code prefixes that map to this client (ЧМ→KK, АР→AM)
- `params` — per-client custom fields with parse rules and frontmatter keys

**Owner mapping**: `prefix → client_code` derived automatically from clients config.

### Custom Fields & Frontmatter
Each client param has a `frontmatter_key` that maps to Obsidian YAML frontmatter. If multiple params map to the same key, values are comma-joined.

Example: Two params `bort_shrapik` and `bort_galtel` both with `frontmatter_key: bort` → `bort: "штапик, галтель"`.

### MAX Bridge
- Uses `pymax` WebClient with browser token auth (no SMS)
- Auth: extract `__oneme_auth.token` from browser LocalStorage on max.ru
- Session persists in `max_sessions/max_bridge.db` — token only needed once
- Filters: chat whitelist + user blacklist (own messages skipped)
- Media: photos/files/videos downloaded from MAX and forwarded to TG

---

## Environment Variables (.env)

| Variable | Required | Description |
|----------|----------|-------------|
| `TG_BOT_TOKEN` | Yes | Telegram bot token |
| `TG_ADMIN_ID` | Yes | Admin user ID for private chat auth |
| `IMAP_HOST` | Yes | IMAP server (default: imap.mail.ru) |
| `IMAP_USER` | Yes | Email address |
| `IMAP_PASSWORD` | Yes | Email password |
| `YOUGILE_API_KEY` | Yes | YouGile API key |
| `YOUGILE_COMPANY_ID` | Yes | YouGile company ID |
| `OBSIDIAN_VAULT_PATH` | Yes | Path to Obsidian vault |
| `WORKING_BASE_PATH` | No | YandexDisk base path |
| `WEB_HOST` | No | Server host (default: 127.0.0.1) |
| `WEB_PORT` | No | Server port (default: 8080) |

MAX bridge settings are in the web UI (Settings → MAX мост), not .env.

---

## Database

**Orders DB**: `app/db/orders.db` — SQLite via aiosqlite
- Table `orders`: all order fields + `custom_fields` (JSON)
- Table `history`: change tracking
- Table `settings`: key-value overrides for DEFAULTS

**Settings**: `settings_db.py` — DEFAULTS dict is the source of truth. DB overrides merged on read. No migration needed — new keys just add to DEFAULTS.

---

## API Endpoints

### Pages
- `GET /` — Dashboard (pending queue + all orders)
- `GET /order/{id}` — Order detail/edit
- `GET /settings` — Settings page
- `GET /new` — Manual entry form

### Orders
- `POST /api/process` — Parse text → create pending order
- `GET /api/orders` — List orders
- `PUT /api/orders/{id}` — Update order
- `POST /api/orders/{id}/confirm` — Confirm → create note + folder
- `DELETE /api/orders/{id}` — Delete from DB

### Scanning
- `POST /api/scan/telegram|email|yougile` — Manual scan

### Settings
- `GET /api/settings` — All settings
- `PUT /api/settings/{key}` — Set one setting
- `POST /api/settings/batch` — Set multiple
- `DELETE /api/settings/{key}` — Reset to default
- `PUT /api/settings/clients/{code}` — Save client config
- `GET /api/clients/list` — Clients as list

### MAX Bridge
- `GET /api/max-bridge/status` — Running?
- `POST /api/max-bridge/start` — Start bridge
- `POST /api/max-bridge/stop` — Stop bridge

### YouGile
- `GET /api/yougile/boards` — List boards with columns
- `GET /api/yougile/stickers` — List sticker states
- `GET /api/yougile/users` — List users
- `GET /api/yougile/blacklist` — Get blacklist
- `POST /api/yougile/blacklist` — Add to blacklist
- `DELETE /api/yougile/blacklist/{id}` — Remove from blacklist

---

## Development

### Running locally
```bash
# From project root
python run.py
# Opens on http://127.0.0.1:8090
```

### Running in Docker
```bash
docker compose up --build
```

### Adding a new client
1. Settings → Clients → "+ Новый клиент"
2. Set code, name, prefixes, color
3. Add params with `frontmatter_key` for Obsidian mapping

### MAX bridge setup
1. Open max.ru in browser → F12 → Application → Local Storage → copy `__oneme_auth.token`
2. Settings → MAX мост → paste token → Save → Start
3. Session persists; token only needed on first run or if session expires

---

## File Conventions

- **Python**: async/await everywhere, aiosqlite for DB, aiogram for TG
- **Templates**: Jinja2 with `{% extends "base.html" %}`, Bootstrap 5
- **Settings**: Add new keys to `DEFAULTS` in `settings_db.py` — they auto-appear in UI
- **Frontmatter**: Use camelCase keys in Obsidian YAML (e.g., `orderCode`, `sinkType`)
- **Obsidian vault**: Mounted at `/vault` in Docker, configurable via `OBSIDIAN_VAULT_PATH`
- **Working folders**: YandexDisk mounted at `/yandexdisk`, structured as `Owner/OrderCode/`

---

## Important Patterns

- **Order code format**: `XXX-NNNN` (e.g., ЧМ-1102, СРБ-1046). Prefix determines owner.
- **Multi-order**: One message can contain multiple order codes → separate DB records, shared folder
- **Dedup**: MAX bridge deduplicates by message ID. TG bot handles attachments via pending queue.
- **Auto-mode**: Scheduler runs periodic scans (configurable intervals in settings)
- **Email auto-download**: If `email_reference=True`, confirm triggers IMAP attachment download

---

## Common Tasks

### Add a new settings key
1. Add to `DEFAULTS` in `app/settings_db.py`
2. It appears in `/api/settings` automatically
3. For UI: add input in `settings.html` + JS handler

### Add a new channel
1. Create `app/channels/new_channel.py`
2. Add start/stop functions
3. Register in `run.py` (auto-start if enabled)
4. Add API endpoints in `routes.py`
5. Add UI toggle in `index.html` dashboard
