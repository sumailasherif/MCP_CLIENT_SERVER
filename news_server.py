"""MCP server for Mauritian news.

Exposes the news archive (news_store.py) as MCP tools, formatted as text for
the model. The archive grows every time the feeds are refreshed, so older
articles stay searchable after they drop off the feeds.

Run `python news_server.py --refresh` to refresh the archive without starting
the server (e.g. from Windows Task Scheduler).
"""

import asyncio
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

import news_store as store

mcp = MCPServer("mauritius-news")

# Every tool says whether it changes data; the client asks before running destructive ones.
READ_ONLY = ToolAnnotations(read_only_hint=True)
ADDS_DATA = ToolAnnotations(read_only_hint=False, destructive_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)


def format_article(article: dict[str, Any], preview: str | None = None) -> str:
    published = article["published"][:16].replace("T", " ") + " UTC" if article["published"] else "unknown date"
    lines = [f"[{article['id']}] {article['title']}", f"    {article['source_name']} | {published} | {article['url']}"]
    if article["categories"]:
        lines.append(f"    Categories: {', '.join(article['categories'])}")
    preview = preview or article["summary"] or article["content"]
    if preview:
        lines.append("    " + preview.replace("\n", " ")[:300])
    return "\n".join(lines)


@mcp.tool(annotations=READ_ONLY)
async def list_sources() -> str:
    """List the Mauritian news sources in the archive, with article counts and last refresh time."""
    lines = []
    for s in store.list_sources():
        last = (
            f"last refreshed {s['last_refreshed'][:16]} UTC ({s['last_status']})"
            if s["last_refreshed"]
            else "never refreshed"
        )
        lines.append(f"- {s['key']}: {s['name']} [{s['language']}] - {s['articles']} articles archived, {last}")
    return "\n".join(lines)


@mcp.tool(annotations=ADDS_DATA)
async def refresh_news(source: str | None = None) -> str:
    """Fetch the latest articles from the news feeds into the archive.

    Other tools refresh stale feeds automatically, so this is only needed to force an update.

    Args:
        source: One source key (defimedia, newsmoris, ionnews, mauritiusnews, 4minit), or omit for all sources
    """
    try:
        statuses = await store.refresh(store.resolve_sources(source))
    except store.UnknownSourceError as e:
        raise ToolError(str(e)) from e
    return "\n".join(f"- {s}: {status}" for s, status in statuses.items())


@mcp.tool(annotations=READ_ONLY)
async def latest_news(source: str | None = None, limit: int = 10) -> str:
    """Get the most recent Mauritian news headlines, newest first.

    Args:
        source: One source key (defimedia, newsmoris, ionnews, mauritiusnews, 4minit), or omit for all sources
        limit: Number of articles to return (max 25)
    """
    try:
        articles = await store.latest_articles(source, limit)
    except store.UnknownSourceError as e:
        raise ToolError(str(e)) from e
    return "\n\n".join(format_article(a) for a in articles) or "No articles archived yet."


@mcp.tool(annotations=READ_ONLY)
async def search_news(query: str, source: str | None = None, days: int | None = None, limit: int = 10) -> str:
    """Search the archived Mauritian news by keywords, best matches first.

    Articles are in French (Défimedia, ION News) or English (Newsmoris, MauritiusNews, 4minit), so search
    with keywords in both languages when relevant, e.g. "grève OR strike". Accents are optional. Each 4minit
    article is a whole daily newsletter covering many stories.

    Args:
        query: Keywords. Supports OR, AND, NOT, "exact phrases" and prefix* matching
        source: One source key (defimedia, newsmoris, ionnews, mauritiusnews, 4minit), or omit for all sources
        days: Only include articles published in the last N days
        limit: Number of articles to return (max 25)
    """
    try:
        articles = await store.search_articles(query, source, days, limit)
    except (store.UnknownSourceError, store.EmptyQueryError) as e:
        raise ToolError(str(e)) from e
    return "\n\n".join(format_article(a, a["snippet"]) for a in articles) or f"No archived articles match {query!r}."


@mcp.tool(annotations=READ_ONLY)
async def get_article(article_id: int) -> str:
    """Get the full stored text of one archived article.

    Args:
        article_id: The number shown in [brackets] by latest_news or search_news
    """
    article = store.get_article(article_id)
    if article is None:
        raise ToolError(f"No article with id {article_id}.")
    body = article["content"] or article["summary"]
    if not body:
        body = "(This source only publishes headlines in its feed. Open the link to read the full article.)"
    header = format_article(article).split("\n")[:3]
    meta = [f"    Author: {article['author']}"] if article["author"] else []
    return "\n".join([*header, *meta, "", body])


# --- Notes: the user's own data ------------------------------------------------


def format_note(note: dict[str, Any]) -> str:
    lines = [f"Note #{note['id']} (updated {note['updated'][:16].replace('T', ' ')} UTC)"]
    if note["article_id"] is not None:
        lines.append(f"    About article [{note['article_id']}] {note['article_title']} | {note['article_url']}")
    if note["tags"]:
        lines.append(f"    Tags: {', '.join(note['tags'])}")
    lines.append(f"    {note['text']}")
    return "\n".join(lines)


@mcp.tool(annotations=ADDS_DATA)
async def create_note(text: str, article_id: int | None = None, tags: list[str] | None = None) -> str:
    """Save a note for the user, optionally about an archived article (a bookmark with a comment).

    Use this when the user asks to save, bookmark, remember or note something.

    Args:
        text: The note, in the user's words
        article_id: The article it is about: the number in [brackets] from latest_news or search_news
        tags: Labels to group notes by, e.g. ["report", "politics"]
    """
    try:
        return "Saved.\n" + format_note(store.create_note(text, article_id, tags))
    except (store.NotFoundError, ValueError) as e:
        raise ToolError(str(e)) from e


@mcp.tool(annotations=READ_ONLY)
async def list_notes(
    tag: str | None = None, article_id: int | None = None, query: str | None = None, limit: int = 25
) -> str:
    """List the user's saved notes, newest first. All filters are optional and combine.

    Args:
        tag: Only notes with this tag
        article_id: Only notes about this article
        query: Only notes whose text contains these words
        limit: Maximum number of notes to return
    """
    notes = store.list_notes(tag, article_id, query, limit)
    return "\n\n".join(format_note(n) for n in notes) or "No notes found."


@mcp.tool(annotations=ADDS_DATA)
async def update_note(
    note_id: int, text: str | None = None, tags: list[str] | None = None, article_id: int | None = None
) -> str:
    """Edit one of the user's notes. Only the fields given are changed.

    Args:
        note_id: The note's number, from list_notes or create_note
        text: New text, replacing the old text
        tags: New tags, replacing all old tags. To add a tag, pass the old tags plus the new one
        article_id: Link the note to this article instead
    """
    try:
        return "Updated.\n" + format_note(store.update_note(note_id, text, tags, article_id))
    except (store.NotFoundError, ValueError) as e:
        raise ToolError(str(e)) from e


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_note(note_id: int) -> str:
    """Permanently delete one of the user's notes. The client asks the user to confirm first.

    Args:
        note_id: The note's number, from list_notes
    """
    try:
        return "Deleted.\n" + format_note(store.delete_note(note_id))
    except store.NotFoundError as e:
        raise ToolError(str(e)) from e


if __name__ == "__main__":
    if "--refresh" in sys.argv:
        for source, status in asyncio.run(store.refresh(list(store.SOURCES))).items():
            print(f"{source}: {status}")
    else:
        mcp.run(transport="stdio")
