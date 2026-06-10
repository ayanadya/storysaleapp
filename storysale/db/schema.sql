-- One row per scraped IG item (post or story) that passed the filter gate.
CREATE TABLE IF NOT EXISTS content (
    ig_id           TEXT PRIMARY KEY,
    account         TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('post', 'story')),
    posted_at       INTEGER NOT NULL,   -- unix seconds
    scraped_at      INTEGER NOT NULL,
    caption         TEXT NOT NULL DEFAULT '',
    ocr_text        TEXT NOT NULL DEFAULT '',
    prices_json     TEXT NOT NULL DEFAULT '[]',
    clothing_json   TEXT NOT NULL DEFAULT '[]',
    brand           TEXT,               -- first canonical brand, NULL if none
    brands_json     TEXT NOT NULL DEFAULT '[]',
    ig_url          TEXT NOT NULL,
    thumbnail_path  TEXT,               -- relative path under data/thumbs/
    needs_review    INTEGER NOT NULL DEFAULT 0,   -- 1 if any OCR token < 0.5
    feedback        INTEGER NOT NULL DEFAULT 0    -- -1 / 0 / +1; survives retention if non-zero
);

CREATE INDEX IF NOT EXISTS idx_content_account     ON content(account);
CREATE INDEX IF NOT EXISTS idx_content_kind_posted ON content(kind, posted_at);
CREATE INDEX IF NOT EXISTS idx_content_brand       ON content(brand);

-- FTS5 mirror for caption + ocr_text.
CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5(
    caption, ocr_text,
    content='content', content_rowid='rowid',
    tokenize = "unicode61 remove_diacritics 2"
);

-- Keep FTS in sync. SQLite needs explicit triggers for external-content tables.
CREATE TRIGGER IF NOT EXISTS content_ai AFTER INSERT ON content BEGIN
    INSERT INTO content_fts(rowid, caption, ocr_text)
    VALUES (new.rowid, new.caption, new.ocr_text);
END;

CREATE TRIGGER IF NOT EXISTS content_ad AFTER DELETE ON content BEGIN
    INSERT INTO content_fts(content_fts, rowid, caption, ocr_text)
    VALUES ('delete', old.rowid, old.caption, old.ocr_text);
END;

CREATE TRIGGER IF NOT EXISTS content_au AFTER UPDATE ON content BEGIN
    INSERT INTO content_fts(content_fts, rowid, caption, ocr_text)
    VALUES ('delete', old.rowid, old.caption, old.ocr_text);
    INSERT INTO content_fts(rowid, caption, ocr_text)
    VALUES (new.rowid, new.caption, new.ocr_text);
END;

-- Audit log: one row per scraper invocation.
CREATE TABLE IF NOT EXISTS scrape_run (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      INTEGER NOT NULL,
    finished_at     INTEGER,
    items_seen      INTEGER NOT NULL DEFAULT 0,
    items_stored    INTEGER NOT NULL DEFAULT 0,
    items_rejected  INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'running',  -- running|ok|partial|failed|auth_expired
    error           TEXT
);

CREATE TABLE IF NOT EXISTS account (
    username        TEXT PRIMARY KEY,
    added_at        INTEGER NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    -- 0 = never scraped; updated by pipeline after a successful per-account run.
    -- Used by list_accounts_for_scrape() to pick least-recently-scraped first
    -- so a small per-run batch fairly cycles through the full followee list.
    last_scraped_at INTEGER NOT NULL DEFAULT 0
);

-- idx_account_last_scraped is created in repo._migrate so it lands AFTER
-- ALTER TABLE adds the column on pre-existing DBs.

-- One row per tracked account: latest verdict of the story-sale check.
-- Upserted on every story-scrape run; old values overwritten.
CREATE TABLE IF NOT EXISTS account_signal (
    account             TEXT PRIMARY KEY,
    checked_at          INTEGER NOT NULL,
    story_count_24h     INTEGER NOT NULL DEFAULT 0,
    active_sale_count   INTEGER NOT NULL DEFAULT 0,
    offers_only_count   INTEGER NOT NULL DEFAULT 0,
    sold_count          INTEGER NOT NULL DEFAULT 0,
    state               TEXT NOT NULL DEFAULT 'none'
        CHECK (state IN ('active_sale', 'offers_only', 'sold', 'none')),
    snippets_json       TEXT NOT NULL DEFAULT '[]',
    prices_json         TEXT NOT NULL DEFAULT '[]',
    needs_review        INTEGER NOT NULL DEFAULT 0,
    -- Posted-at of the newest / oldest active-sale story seen at this check.
    -- 0 = no active-sale stories observed. UI uses newest+24h to compute
    -- "expires in N hours" and to hide signals whose stories are already gone
    -- from IG.
    newest_story_at     INTEGER NOT NULL DEFAULT 0,
    oldest_story_at     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_account_signal_state ON account_signal(state);
-- idx_account_signal_newest is created in repo._migrate (depends on the
-- newest_story_at column which we add via ALTER on pre-existing DBs).
