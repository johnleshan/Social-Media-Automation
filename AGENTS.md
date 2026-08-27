# Project Notes for AI Sessions

## Python environment (IMPORTANT)
- This project has a venv at `venv\` — ALWAYS use it explicitly for every
  command, test, or probe. Never rely on global `python`.
  - Run scripts/tests:  `venv\Scripts\python.exe <script>`
  - Compile checks:     `venv\Scripts\python.exe -m py_compile app/...`
  - The user often forgets to activate it; do not ask, just use the path.
- Global Python (Python312) also has the deps, but treat the venv as canonical.

## Running the app for the user (USER RULE — always follow)
- While coding/building: the app must be CLOSED. Kill `python.exe` running
  `main.py` before you start editing, and never assume it is still running
  afterwards.
- When handing work over (done coding): start exactly one fresh instance and
  verify it responds:
  1. Kill stale instances:
     `Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ? { $_.CommandLine -match 'main\.py' } | % { Stop-Process -Id $_.ProcessId -Force }`
  2. Start fresh (detached):
     `Start-Process -FilePath "venv\Scripts\python.exe" -ArgumentList "main.py" -WorkingDirectory <project root>`
  3. Verify http://127.0.0.1:8756/api/state responds before handing over.
- ONLY ONE app instance may run at a time (shared sqlite DB + profile locks).
- Testing exception: the app may be started temporarily to run automated UI
  tests; close or restart it cleanly when handing back.

## App layout
- Entry: `main.py` (web UI default; `--gui` legacy Tkinter; `--cli`)
- Web UI served from `web/` (index.html, app.js, style.css) on port 8756+
- Backend modules in `app/`: webserver.py (HTTP API), worker.py (job engine),
  browser.py (native Chrome + CDP via Playwright), discovery.py (group search),
  joiner.py (auto-join), approval.py (safe/skip classifier), poster.py,
  database.py (sqlite at data/bot.db), config.py (data/config.json)
- Facebook login works via native Chrome windows (app-bound cookie encryption
  makes profile copying useless); browser driven over CDP.

## Gotchas learned the hard way
- Chrome 151+ encrypts cookies (app_bound_encrypted_key) — copied profiles load
  0 cookies. Live CDP cookie reading is the only working approach.
- PowerShell inline `python -c "..."` with quotes gets mangled by
  Start-Process/-ArgumentList splitting — write temp .py files instead.
- Console is cp1252: set `$env:PYTHONIOENCODING='utf-8'` when printing emoji/
  unicode from tests.
