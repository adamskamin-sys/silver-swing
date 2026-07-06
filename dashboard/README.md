# silver-swing dashboard

Read-only status view for the silver-swing bot (spec §10 + §12 step 7).

## What it does

- Polls the shared `StateStore` (JSON file for local dev, KV/Postgres in prod)
  every 5s and renders per-instrument cards.
- Shows the current leg, swing size, core floor, exit mode, sell/buy targets,
  abort bracket, cycle count, realized P&L, live order id, last heartbeat.
- Shows the tail of the trade log (halts, arm/fill events, kill-switch pauses)
  color-coded by priority.
- Session-based password auth on `/api/*`. If `DASHBOARD_PASSWORD` isn't set,
  runs in dev mode with no auth — do NOT deploy without it.

## What it does NOT do

- Never touches Coinbase. Never holds an API key.
- Never places orders.
- Never edits config (spec §12 step 7: watch before you change).
  Editable config + validation is step 8, follow-up work.

## Local dev

```bash
cd dashboard
npm install
DASHBOARD_PASSWORD=changeme SESSION_SECRET=$(openssl rand -hex 32) npm start
# open http://localhost:3000
```

Set `SWING_STORE_PATH` if the store lives somewhere other than `../data/store.json`.

## Deployment to Render (matches spec §10)

- One Render web service running this app on `dash.smearthequeer.com` (CNAME
  the subdomain at your DNS provider to the Render service URL).
- The bot process runs as a **separate** Render background worker. It's the
  only holder of Coinbase API keys and the only thing placing orders.
- **Shared store**: swap `JsonFileStateStore` for the KV/Postgres backend so
  bot and dashboard read/write the same source of truth. The `StateStore`
  Protocol is designed for this drop-in.
- HTTPS is Render's default. Set `cookie.secure: true` in `server.js` before
  going live.

### Env vars

| var | required | notes |
| --- | --- | --- |
| `DASHBOARD_PASSWORD` | yes (prod) | leave unset for dev-only mode |
| `SESSION_SECRET` | yes (prod) | 32+ random bytes; rotate periodically |
| `SWING_STORE_PATH` | no | defaults to `../data/store.json` locally |
| `SWING_TRADE_LOG_PATH` | no | defaults to `../data/trades.jsonl` locally |
| `PORT` | no | Render sets this; defaults to 3000 locally |
