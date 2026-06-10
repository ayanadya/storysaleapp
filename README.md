# StorySale

See what Instagram accounts are having a story sale, and search archival
clothing across tracked accounts.

Scrapes posts + stories from a list of accounts you maintain, filters down
to actual for-sale items, and indexes everything in a local SQLite DB you
search via Streamlit. Personal use only. Zero ongoing cost — no APIs, no LLM
calls.

## Architecture

```
ingest/  → fetch (instaloader) → OCR (easyocr, stories only)
         → extract (regex + dicts: prices, clothing, brands, sold)
         → gate   (keep iff clothing ≥1, price ≥1, not sold)
         → persist (SQLite + FTS5)
db/      → schema with retention rules baked in
ui/      → Streamlit app reads via repo.search
cli.py   → driver for login, scrape, sweep, search
```

## Setup

```powershell
# one-time
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# interactive login (creates secrets/session-burner pickle)
.venv\Scripts\python -m storysale.cli login --username YOUR_BURNER

# track an account
.venv\Scripts\python -m storysale.cli accounts add some_resale_acct
```

## Daily use

```powershell
# scrape (2–3× per day; jittered 4–8s between items)
.venv\Scripts\python -m storysale.cli scrape

# search from CLI
.venv\Scripts\python -m storysale.cli search "brandy"

# delete expired rows + orphan thumbnails
.venv\Scripts\python -m storysale.cli sweep

# launch the UI
.venv\Scripts\streamlit run storysale\ui\app.py
```

## Smoke test without IG

```powershell
.venv\Scripts\python -m storysale.cli scrape --dry-run
```

Inserts two canned demo rows so you can see the pipeline + UI end-to-end
without a real account.

## Tests

```powershell
.venv\Scripts\python -m pytest
```

Covers extraction, filter gate, OCR thresholding, repo+FTS5, retention,
pipeline integration with a fake source, CLI plumbing, and UI query layer.

## Retention policy

- Posts: row + thumbnail kept 30 days
- Stories: row + OCR text kept 30 days; image thumbnail purged at 24h
- Any row with feedback (👍/👎) survives indefinitely

## Data hygiene

- `secrets/` and `data/` are gitignored
- Session pickle = your IG cookies. Don't share, don't commit, rotate the
  burner if leaked
- Pickle files are unsafe to load from untrusted sources — only load files
  this app wrote

## Tuning

- `storysale/dicts/clothing.txt` — add resale-typical items as you see gaps
- `storysale/dicts/brands.yaml` — add canonical brand + aliases
- `storysale/dicts/sold_markers.txt` — add new sold markers (emojis, slang)
- `storysale/ingest/fetch.py` — bump `SLEEP_MIN`/`SLEEP_MAX` if you get 429s
- `storysale/ingest/ocr.py` — adjust `CONF_THRESHOLD` if review queue is noisy
