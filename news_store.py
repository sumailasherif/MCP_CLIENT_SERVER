"""Mauritian news archive: fetches RSS feeds into SQLite and queries them.

Shared by the MCP server (news_server.py) and the REST API (api.py). Query
functions return plain dicts; each caller formats them for its own audience.
The database schema is in schema.sql.
"""

import asyncio
import html
import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx2 as httpx

# The MCP server's stderr is shown in the client's terminal, so keep
# per-request logs out of it.
logging.getLogger("httpx2").setLevel(logging.WARNING)

HERE = Path(__file__).parent
DB_PATH = HERE / "news.db"
SCHEMA_PATH = HERE / "schema.sql"
USER_AGENT = "mauritius-news-mcp/0.1 (personal news archive)"
# Feeds are refreshed automatically when the last fetch is older than this.
REFRESH_AFTER = timedelta(minutes=30)
MAX_RESULTS = 25

# Only outlets whose feeds are public and allowed by robots.txt. With
# fetch_pages, each new item's web page is downloaded once to store its full
# text, for feeds that carry only a teaser.
SOURCES = {
    "defimedia": {"name": "Défimedia", "feed": "https://defimedia.info/rss.xml", "language": "fr"},
    "newsmoris": {"name": "Newsmoris", "feed": "https://www.newsmoris.com/feed/", "language": "en"},
    "ionnews": {"name": "ION News", "feed": "https://ionnews.mu/feed/", "language": "fr"},
    "mauritiusnews": {"name": "MauritiusNews", "feed": "https://mauritius-news.com/rss", "language": "en"},
    # One item per daily newsletter edition, each covering many stories.
    "4minit": {"name": "4minit", "feed": "https://4minit.xyz/feed.xml", "language": "en", "fetch_pages": True},
}
# Cap on pages downloaded per refresh, so a first run doesn't hammer a site.
MAX_PAGES_PER_REFRESH = 20

NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

# Défimedia dates its items in French ("mar, 20 Oct 2026 15:08:29 +0400").
FRENCH_MONTHS = {
    "janv": "Jan", "févr": "Feb", "fevr": "Feb", "mars": "Mar", "avr": "Apr", "mai": "May", "juin": "Jun",
    "juil": "Jul", "août": "Aug", "aout": "Aug", "sept": "Sep", "oct": "Oct", "nov": "Nov", "déc": "Dec",
    "dec": "Dec",
}  # fmt: skip


class UnknownSourceError(ValueError):
    """A source key that is not in SOURCES."""


class EmptyQueryError(ValueError):
    """A search query with no searchable words."""


class NotFoundError(LookupError):
    """No article or note with the given id."""


# --- Database -----------------------------------------------------------------


def connect() -> sqlite3.Connection:
    """Open the archive, creating the schema on first use."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return db


# --- Feed parsing -------------------------------------------------------------


class _TextExtractor(HTMLParser):
    """Collect the visible text of an HTML fragment, one block per line."""

    BLOCKS = {"p", "br", "div", "li", "h1", "h2", "h3", "h4", "blockquote"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


# WordPress feeds end each item with "The post <title> appeared first on <site>."
FEED_FOOTER = re.compile(r"^The post .* appeared first on .*\.$")


def html_to_text(fragment: str) -> str:
    parser = _TextExtractor()
    parser.feed(fragment or "")
    text = html.unescape("".join(parser.parts))
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line and not FEED_FOOTER.match(line))


def parse_date(value: str | None) -> str | None:
    """Parse an RSS date (English or French) to ISO 8601 UTC."""
    if not value:
        return None
    value = value.strip()
    # Drop the weekday, whose name may be French, then translate the month.
    value = re.sub(r"^[^\d,]+,\s*", "", value)
    value = re.sub(
        r"[^\W\d_]+\.?", lambda m: FRENCH_MONTHS.get(m.group(0).rstrip(".").lower(), m.group(0)), value, count=1
    )
    try:
        return parsedate_to_datetime(value).astimezone(UTC).isoformat()
    except (TypeError, ValueError):
        return None


def parse_feed(source: str, xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    articles = []
    for item in root.iterfind("./channel/item"):
        url = (item.findtext("link") or "").strip()
        title = html.unescape((item.findtext("title") or "").strip())
        if not url or not title:
            continue
        articles.append(
            {
                "source": source,
                "url": url,
                "title": title,
                "summary": html_to_text(item.findtext("description") or ""),
                "content": html_to_text(item.findtext("content:encoded", namespaces=NS) or ""),
                "author": (item.findtext("dc:creator", namespaces=NS) or "").strip(),
                "categories": ", ".join(c.text.strip() for c in item.findall("category") if c.text),
                "published": parse_date(item.findtext("pubDate")),
            }
        )
    return articles


# --- Fetching -----------------------------------------------------------------


async def fetch_pages(client: httpx.AsyncClient, db: sqlite3.Connection, source: str) -> int:
    """Store the full page text of archived items that have none yet. Returns how many were stored."""
    rows = db.execute(
        "SELECT id, url FROM articles WHERE source = ? AND content = '' ORDER BY published DESC LIMIT ?",
        (source, MAX_PAGES_PER_REFRESH),
    ).fetchall()
    stored = 0
    # One page at a time, to be gentle on the site.
    for row in rows:
        try:
            response = await client.get(row["url"])
            response.raise_for_status()
        except Exception:
            continue  # Retried on the next refresh.
        # Skip the <head>, whose <title> would repeat the headline.
        page = re.sub(r"(?is)<head\b.*?</head>", "", response.text)
        text = html_to_text(page)
        if text:
            db.execute("UPDATE articles SET content = ? WHERE id = ?", (text, row["id"]))
            stored += 1
    return stored


async def refresh_source(client: httpx.AsyncClient, db: sqlite3.Connection, source: str) -> str:
    """Fetch one feed and archive its new articles. Returns a status line."""
    now = datetime.now(UTC).isoformat()
    try:
        response = await client.get(SOURCES[source]["feed"])
        response.raise_for_status()
        articles = parse_feed(source, response.content)
    except Exception as e:
        status = f"error: {type(e).__name__}: {e}"
    else:
        new = 0
        for a in articles:
            # Keep the first copy of an article; only fill in text a later feed adds.
            new += db.execute(
                """
                INSERT INTO articles (source, url, title, summary, content, author, categories, published, fetched)
                VALUES (:source, :url, :title, :summary, :content, :author, :categories, :published, :fetched)
                ON CONFLICT(url) DO UPDATE SET
                    summary = excluded.summary, content = excluded.content
                WHERE articles.content = '' AND excluded.content != ''
                """,
                {**a, "fetched": now},
            ).rowcount
        status = f"ok: {len(articles)} in feed, {new} new or updated"
        if SOURCES[source].get("fetch_pages"):
            status += f", {await fetch_pages(client, db, source)} full pages fetched"
    db.execute(
        "INSERT INTO fetches (source, fetched, status) VALUES (?, ?, ?) "
        "ON CONFLICT(source) DO UPDATE SET fetched = excluded.fetched, status = excluded.status",
        (source, now, status),
    )
    db.commit()
    return status


async def refresh(sources: list[str], only_if_stale: bool = False) -> dict[str, str]:
    """Refresh the given sources' feeds. Returns a status line per source refreshed."""
    db = connect()
    try:
        if only_if_stale:
            cutoff = (datetime.now(UTC) - REFRESH_AFTER).isoformat()
            fresh = {
                row["source"]
                for row in db.execute("SELECT source FROM fetches WHERE fetched > ? AND status LIKE 'ok%'", (cutoff,))
            }
            sources = [s for s in sources if s not in fresh]
        if not sources:
            return {}
        headers = {"User-Agent": USER_AGENT}
        async with httpx.AsyncClient(headers=headers, timeout=30.0, follow_redirects=True) as client:
            statuses = await asyncio.gather(*(refresh_source(client, db, s) for s in sources))
        return dict(zip(sources, statuses, strict=True))
    finally:
        db.close()


# --- Queries ------------------------------------------------------------------


def resolve_sources(source: str | None) -> list[str]:
    """Turn an optional source key into the list of keys to query."""
    if source is None:
        return list(SOURCES)
    if source not in SOURCES:
        raise UnknownSourceError(f"Unknown source {source!r}. Valid sources: {', '.join(SOURCES)}")
    return [source]


def clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_RESULTS))


def article_dict(row: sqlite3.Row) -> dict[str, Any]:
    article = {key: row[key] for key in row.keys() if key != "snip"}
    article["source_name"] = SOURCES[row["source"]]["name"]
    article["categories"] = [c for c in row["categories"].split(", ") if c]
    return article


def list_sources() -> list[dict[str, Any]]:
    """Sources with their archived article counts and last refresh."""
    db = connect()
    try:
        result = []
        for key, info in SOURCES.items():
            count = db.execute("SELECT COUNT(*) FROM articles WHERE source = ?", (key,)).fetchone()[0]
            fetch = db.execute("SELECT fetched, status FROM fetches WHERE source = ?", (key,)).fetchone()
            result.append(
                {
                    "key": key,
                    "name": info["name"],
                    "language": info["language"],
                    "feed": info["feed"],
                    "articles": count,
                    "last_refreshed": fetch["fetched"] if fetch else None,
                    "last_status": fetch["status"] if fetch else None,
                }
            )
        return result
    finally:
        db.close()


async def latest_articles(source: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """Most recent articles, newest first. Refreshes stale feeds first."""
    sources = resolve_sources(source)
    await refresh(sources, only_if_stale=True)
    db = connect()
    try:
        rows = db.execute(
            f"SELECT * FROM articles WHERE source IN ({','.join('?' * len(sources))}) "
            "ORDER BY published DESC NULLS LAST LIMIT ?",
            (*sources, clamp_limit(limit)),
        ).fetchall()
        return [article_dict(r) for r in rows]
    finally:
        db.close()


async def search_articles(
    query: str, source: str | None = None, days: int | None = None, limit: int = 10
) -> list[dict[str, Any]]:
    """Full-text search, best matches first. Each result has a "snippet" with matches in **bold**."""
    sources = resolve_sources(source)
    await refresh(sources, only_if_stale=True)
    db = connect()
    try:
        sql = f"""
            SELECT a.*, snippet(articles_fts, -1, '**', '**', '…', 30) AS snip
            FROM articles_fts JOIN articles a ON a.id = articles_fts.rowid
            WHERE articles_fts MATCH ? AND a.source IN ({",".join("?" * len(sources))})
              AND (? = '' OR a.published >= ?)
            ORDER BY rank LIMIT ?
        """
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat() if days is not None else ""
        params = (*sources, cutoff, cutoff, clamp_limit(limit))
        try:
            rows = db.execute(sql, (query, *params)).fetchall()
        except sqlite3.OperationalError:
            # Not valid FTS syntax (e.g. stray punctuation): search the words as plain terms.
            words = re.findall(r"\w+", query)
            if not words:
                raise EmptyQueryError("The query has no searchable words.") from None
            rows = db.execute(sql, (" ".join(f'"{w}"' for w in words), *params)).fetchall()
        return [{**article_dict(r), "snippet": r["snip"]} for r in rows]
    finally:
        db.close()


def get_article(article_id: int) -> dict[str, Any] | None:
    """One archived article, or None if there is no such id."""
    db = connect()
    try:
        row = db.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
        return article_dict(row) if row else None
    finally:
        db.close()


# --- Notes (your own data: create, read, update, delete) ----------------------


def _clean_tags(tags: list[str] | None) -> str:
    """Store tags lowercase, trimmed, unique, comma-separated."""
    cleaned = dict.fromkeys(t.strip().lower() for t in tags or [] if t.strip())
    return ",".join(t.replace(",", " ") for t in cleaned)


def note_dict(row: sqlite3.Row) -> dict[str, Any]:
    note = dict(row)
    note["tags"] = [t for t in row["tags"].split(",") if t]
    return note


def _check_article(db: sqlite3.Connection, article_id: int | None) -> None:
    if article_id is not None and db.execute("SELECT 1 FROM articles WHERE id = ?", (article_id,)).fetchone() is None:
        raise NotFoundError(f"No article with id {article_id}")


_NOTE_SELECT = """
    SELECT n.*, a.title AS article_title, a.url AS article_url
    FROM notes n LEFT JOIN articles a ON a.id = n.article_id
"""


def _fetch_note(db: sqlite3.Connection, note_id: int) -> dict[str, Any]:
    row = db.execute(_NOTE_SELECT + " WHERE n.id = ?", (note_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"No note with id {note_id}")
    return note_dict(row)


def create_note(text: str, article_id: int | None = None, tags: list[str] | None = None) -> dict[str, Any]:
    """Save a note, optionally about an article. Returns the new note."""
    if not text.strip():
        raise ValueError("A note needs some text.")
    db = connect()
    try:
        _check_article(db, article_id)
        now = datetime.now(UTC).isoformat()
        note_id = db.execute(
            "INSERT INTO notes (article_id, text, tags, created, updated) VALUES (?, ?, ?, ?, ?)",
            (article_id, text.strip(), _clean_tags(tags), now, now),
        ).lastrowid
        db.commit()
        return _fetch_note(db, note_id)
    finally:
        db.close()


def list_notes(
    tag: str | None = None, article_id: int | None = None, query: str | None = None, limit: int = MAX_RESULTS
) -> list[dict[str, Any]]:
    """Notes, newest first, optionally filtered by tag, article or words in the text."""
    where, params = [], []
    if tag:
        where.append("(',' || n.tags || ',') LIKE ?")
        params.append(f"%,{tag.strip().lower()},%")
    if article_id is not None:
        where.append("n.article_id = ?")
        params.append(article_id)
    if query:
        where.append("n.text LIKE ?")
        params.append(f"%{query}%")
    sql = _NOTE_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY n.updated DESC LIMIT ?"
    db = connect()
    try:
        return [note_dict(r) for r in db.execute(sql, (*params, max(1, limit))).fetchall()]
    finally:
        db.close()


def get_note(note_id: int) -> dict[str, Any]:
    db = connect()
    try:
        return _fetch_note(db, note_id)
    finally:
        db.close()


def update_note(
    note_id: int,
    text: str | None = None,
    tags: list[str] | None = None,
    article_id: int | None = None,
) -> dict[str, Any]:
    """Change a note's text, tags (replaced as a whole) or article. Omitted fields are kept."""
    db = connect()
    try:
        _fetch_note(db, note_id)
        _check_article(db, article_id)
        changes = {}
        if text is not None:
            if not text.strip():
                raise ValueError("A note needs some text.")
            changes["text"] = text.strip()
        if tags is not None:
            changes["tags"] = _clean_tags(tags)
        if article_id is not None:
            changes["article_id"] = article_id
        if changes:
            changes["updated"] = datetime.now(UTC).isoformat()
            assignments = ", ".join(f"{column} = :{column}" for column in changes)
            db.execute(f"UPDATE notes SET {assignments} WHERE id = :id", {**changes, "id": note_id})
            db.commit()
        return _fetch_note(db, note_id)
    finally:
        db.close()


def delete_note(note_id: int) -> dict[str, Any]:
    """Delete a note. Returns the note as it was."""
    db = connect()
    try:
        note = _fetch_note(db, note_id)
        db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        db.commit()
        return note
    finally:
        db.close()
