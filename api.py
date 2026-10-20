"""REST API for the Mauritian news archive.

Serves the same archive as the MCP server (both use news_store.py) as JSON.

Run:  python api.py
Docs: http://127.0.0.1:8000/docs (interactive), /openapi.json (OpenAPI contract)
"""

from typing import Literal, get_args

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

import news_store as store

app = FastAPI(
    title="Mauritius News API",
    version="0.1.0",
    description=(
        "Articles from Mauritian news outlets, archived from their RSS feeds. "
        "Read routes refresh any feed last fetched more than 30 minutes ago before answering."
    ),
)

# Valid source keys, so bad values are rejected with 422 and /docs shows a dropdown.
SourceKey = Literal["defimedia", "newsmoris", "ionnews", "mauritiusnews", "4minit"]
if set(get_args(SourceKey)) != set(store.SOURCES):
    raise RuntimeError("SourceKey must list the same keys as news_store.SOURCES")


class Source(BaseModel):
    key: str
    name: str
    language: str
    feed: str
    articles: int
    last_refreshed: str | None
    last_status: str | None


class ArticleSummary(BaseModel):
    id: int
    source: str
    source_name: str
    url: str
    title: str
    summary: str
    author: str
    categories: list[str]
    published: str | None
    fetched: str


class Article(ArticleSummary):
    content: str


class SearchResult(ArticleSummary):
    snippet: str


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/docs")


@app.get("/api/sources", tags=["sources"], response_model=list[Source])
def list_sources():
    """List the news sources with their archived article counts and last refresh."""
    return store.list_sources()


@app.post("/api/refresh", tags=["sources"])
async def refresh(source: SourceKey | None = None) -> dict[str, str]:
    """Fetch the latest articles from the feeds now. Returns a status line per source."""
    return await store.refresh(store.resolve_sources(source))


@app.get("/api/articles", tags=["articles"], response_model=list[ArticleSummary])
async def latest_articles(
    source: SourceKey | None = None,
    limit: int = Query(10, ge=1, le=store.MAX_RESULTS),
):
    """The most recent articles, newest first."""
    return await store.latest_articles(source, limit)


# Declared before /api/articles/{article_id} so "search" is not read as an id.
@app.get("/api/articles/search", tags=["articles"], response_model=list[SearchResult])
async def search_articles(
    q: str = Query(..., min_length=1, description='Keywords. Supports OR, AND, NOT, "phrases" and prefix*'),
    source: SourceKey | None = None,
    days: int | None = Query(None, ge=1, description="Only articles published in the last N days"),
    limit: int = Query(10, ge=1, le=store.MAX_RESULTS),
):
    """Full-text search, best matches first. Accents are optional; matches are **bold** in the snippet."""
    try:
        return await store.search_articles(q, source, days, limit)
    except store.EmptyQueryError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get(
    "/api/articles/{article_id}",
    tags=["articles"],
    response_model=Article,
    responses={404: {"description": "No such article"}},
)
def get_article(article_id: int):
    """One article, with its full stored text."""
    article = store.get_article(article_id)
    if article is None:
        raise HTTPException(status_code=404, detail=f"No article with id {article_id}")
    return article


# --- Notes: your own data (create, read, update, delete) ----------------------


class NoteIn(BaseModel):
    text: str = Field(min_length=1)
    article_id: int | None = None
    tags: list[str] = []


class NoteUpdate(BaseModel):
    """Fields to change; omitted fields are kept. tags replaces all existing tags."""

    text: str | None = Field(None, min_length=1)
    article_id: int | None = None
    tags: list[str] | None = None


class Note(BaseModel):
    id: int
    text: str
    tags: list[str]
    article_id: int | None
    article_title: str | None
    article_url: str | None
    created: str
    updated: str


NOT_FOUND = {404: {"description": "No such note, or no such article to link"}}


def _note_errors(fn, *args):
    """Run a news_store notes call, mapping its errors to HTTP status codes."""
    try:
        return fn(*args)
    except store.NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/api/notes", tags=["notes"], response_model=list[Note])
def list_notes(
    tag: str | None = None,
    article_id: int | None = None,
    q: str | None = Query(None, description="Only notes whose text contains this"),
    limit: int = Query(store.MAX_RESULTS, ge=1, le=100),
):
    """Your notes, most recently updated first. Filters are optional and combine."""
    return store.list_notes(tag, article_id, q, limit)


@app.post("/api/notes", tags=["notes"], response_model=Note, status_code=201, responses=NOT_FOUND)
def create_note(note: NoteIn):
    """Save a note, optionally about an article."""
    return _note_errors(store.create_note, note.text, note.article_id, note.tags)


@app.get("/api/notes/{note_id}", tags=["notes"], response_model=Note, responses=NOT_FOUND)
def get_note(note_id: int):
    """One note."""
    return _note_errors(store.get_note, note_id)


@app.patch("/api/notes/{note_id}", tags=["notes"], response_model=Note, responses=NOT_FOUND)
def update_note(note_id: int, changes: NoteUpdate):
    """Change a note's text, tags or linked article."""
    return _note_errors(store.update_note, note_id, changes.text, changes.tags, changes.article_id)


@app.delete("/api/notes/{note_id}", tags=["notes"], response_model=Note, responses=NOT_FOUND)
def delete_note(note_id: int):
    """Delete a note. Returns the note as it was."""
    return _note_errors(store.delete_note, note_id)


if __name__ == "__main__":
    uvicorn.run("api:app", host="127.0.0.1", port=8000)
