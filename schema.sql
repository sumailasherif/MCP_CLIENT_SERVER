-- Schema of the news archive (news.db). news_store.connect() applies it on
-- every start; each statement is idempotent, so existing data is kept.

-- One row per article, keyed by its URL.
CREATE TABLE IF NOT EXISTS articles (
    id         INTEGER PRIMARY KEY,
    source     TEXT NOT NULL,              -- key in news_store.SOURCES
    url        TEXT NOT NULL UNIQUE,
    title      TEXT NOT NULL,
    summary    TEXT NOT NULL DEFAULT '',   -- plain text from the feed's <description>
    content    TEXT NOT NULL DEFAULT '',   -- full plain text, when the feed or page provides it
    author     TEXT NOT NULL DEFAULT '',
    categories TEXT NOT NULL DEFAULT '',   -- comma-separated
    published  TEXT,                       -- ISO 8601, UTC
    fetched    TEXT NOT NULL               -- ISO 8601, UTC
);
CREATE INDEX IF NOT EXISTS articles_published ON articles(published);

-- Full-text index over articles; remove_diacritics lets "emeutes" match "Émeutes".
CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    title, summary, content, categories,
    content='articles', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts(rowid, title, summary, content, categories)
    VALUES (new.id, new.title, new.summary, new.content, new.categories);
END;
CREATE TRIGGER IF NOT EXISTS articles_au AFTER UPDATE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, summary, content, categories)
    VALUES ('delete', old.id, old.title, old.summary, old.content, old.categories);
    INSERT INTO articles_fts(rowid, title, summary, content, categories)
    VALUES (new.id, new.title, new.summary, new.content, new.categories);
END;

-- Your own notes and bookmarks, created from the chat or the API. Kept apart
-- from the feed data, which refreshes never change.
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,  -- ids are never reused, so "note 3" stays unambiguous
    article_id INTEGER REFERENCES articles(id),   -- the article the note is about, if any
    text       TEXT NOT NULL,
    tags       TEXT NOT NULL DEFAULT '',          -- comma-separated, lowercase, e.g. "report,politics"
    created    TEXT NOT NULL,                     -- ISO 8601, UTC
    updated    TEXT NOT NULL                      -- ISO 8601, UTC
);
CREATE INDEX IF NOT EXISTS notes_article ON notes(article_id);

-- Last refresh of each source's feed.
CREATE TABLE IF NOT EXISTS fetches (
    source  TEXT PRIMARY KEY,
    fetched TEXT NOT NULL,   -- ISO 8601, UTC
    status  TEXT NOT NULL    -- "ok: ..." or "error: ..."
);
