# Mauritius News MCP

Ask questions about Mauritian news in plain English, or query the news archive over a REST API.

| File | Role |
| --- | --- |
| `news_store.py` | Data layer: fetches the RSS feeds, archives articles in SQLite (`news.db`), and runs the queries. Shared by the two servers below |
| `schema.sql` | The database schema: articles, full-text index, your notes, refresh log |
| `news_server.py` | MCP server: exposes the archive and your notes as tools for an AI model |
| `api.py` | REST API (FastAPI): exposes the archive as JSON routes |
| `Mcp_client.py` | Chat client: launches the MCP server, hands its tools to a model served by OpenCode, and answers your questions. Answers are always in English: French headlines and articles are translated, with the original French headline kept in parentheses |

## Sources

| Key | Outlet | Language |
| --- | --- | --- |
| `defimedia` | [Défimedia](https://defimedia.info) | French (headlines only in the feed) |
| `newsmoris` | [Newsmoris](https://www.newsmoris.com) | English |
| `ionnews` | [ION News](https://ionnews.mu) | French |
| `mauritiusnews` | [MauritiusNews](https://mauritius-news.com) | English (headline and summary in the feed) |
| `4minit` | [4minit](https://4minit.xyz) | English. Each item is a whole daily newsletter; its full text is downloaded once from the edition's page |

Only outlets with public RSS feeds that their `robots.txt` allows are included. Others were left out: Le Mauricien's `robots.txt` disallows its feed, L'Express publishes no feed, and the government GIS feed is no longer updated. To add an outlet, add its feed to `SOURCES` in `news_store.py` and its key to `SourceKey` in `api.py`; set `"fetch_pages": True` if its feed carries only a teaser and the full text is on the linked page.

## Tools

| Tool | What it does |
| --- | --- |
| `latest_news(source?, limit?)` | Most recent headlines, newest first |
| `search_news(query, source?, days?, limit?)` | Full-text search over the archive. Accents are optional (`emeutes` matches `Émeutes`); supports `OR`, `NOT`, `"phrases"` and `prefix*` |
| `get_article(article_id)` | Full stored text of one article |
| `list_sources()` | Sources, archived article counts and last refresh |
| `refresh_news(source?)` | Force a feed refresh |
| `create_note(text, article_id?, tags?)` | Save a note, optionally about an article |
| `list_notes(tag?, article_id?, query?, limit?)` | List your notes, newest first |
| `update_note(note_id, text?, tags?, article_id?)` | Edit a note; omitted fields are kept |
| `delete_note(note_id)` | Delete a note (the client asks you to confirm first) |

## Notes from the chat (CRUD)

Besides the news, the database holds your own notes in a separate `notes` table, so feed refreshes never touch them. Manage them by talking to the model:

- "Save the latest 4minit newsletter with the note 'use in my report', tagged report and politics" → `create_note`
- "Show my notes tagged report" → `list_notes`
- "Add the tag curfew to note 4" → `update_note`
- "Delete note 4" → `delete_note`

Creates and edits run straight away. **Deletes always ask first:**

```text
⚠️  The model wants to run delete_note with {'note_id': 4}. Proceed? [y/N]
```

Anything but `y` cancels, and the model is told you declined. The client decides which tools need this from the tools' MCP annotations (`destructive_hint`), not from their names, so it also protects you when connected to other MCP servers. A tool without annotations is treated as destructive, as the MCP spec says.

## REST API

Start it with:

```bash
python -m uv run api.py
```

Then open <http://127.0.0.1:8000/docs> to try every route in the browser. The OpenAPI contract is at <http://127.0.0.1:8000/openapi.json>, which Postman, Insomnia and code generators can import.

| Method | Route | Returns |
| --- | --- | --- |
| `GET` | `/api/sources` | Sources with article counts and last refresh |
| `GET` | `/api/articles?source=&limit=` | Latest articles, newest first (without full text) |
| `GET` | `/api/articles/search?q=&source=&days=&limit=` | Full-text search results, best first, each with a `snippet` |
| `GET` | `/api/articles/{id}` | One article with its full text (`content`) |
| `POST` | `/api/refresh?source=` | Fetches the feeds now; a status line per source |
| `GET` | `/api/notes?tag=&article_id=&q=&limit=` | Your notes, most recently updated first |
| `POST` | `/api/notes` | Creates a note from `{"text", "article_id"?, "tags"?}`; `201` with the note |
| `GET` | `/api/notes/{id}` | One note |
| `PATCH` | `/api/notes/{id}` | Changes the fields given (`text`, `tags`, `article_id`); returns the note |
| `DELETE` | `/api/notes/{id}` | Deletes a note; returns it as it was |

`source` is one of `defimedia`, `newsmoris`, `ionnews`, `mauritiusnews`, `4minit`; omit it for all sources. Article `limit` is 1–25 (default 10). Errors: unknown source, out-of-range parameter or empty note text → `422`; query with no searchable words → `400`; unknown article or note id → `404`.

Example:

```bash
curl "http://127.0.0.1:8000/api/articles/search?q=curfew&days=7&limit=5"
```

## The database

The archive is a SQLite file, `news.db`, created next to the code on first run. SQLite is built into Python, so there's no database server to install. Its structure is defined in `schema.sql`, which is applied automatically on start.

`news.db` is not committed to git (see `.gitignore`): it is data, and anyone can rebuild it from the feeds. To create or update it:

```bash
python -m uv run news_server.py --refresh
```

Feeds only list the latest 10–100 articles, so the archive keeps every article it sees, and older articles stay searchable after they leave the feeds. The read tools and routes refresh any feed last fetched more than 30 minutes ago, so the archive grows as you use it. To keep it growing when you aren't using it, schedule the refresh command above, e.g. hourly with Windows Task Scheduler.

To browse the database directly, open `news.db` in [DB Browser for SQLite](https://sqlitebrowser.org/).

## Setup

- Python 3.11+
- An [OpenCode API key](https://opencode.ai/zen) (from the OpenCode Console, with credit for a paid model)

```bash
python -m pip install uv
python -m uv sync
copy .env.example .env      # then put your OPENCODE_API_KEY in .env
```

## Run

```bash
python -m uv run Mcp_client.py news_server.py
```

If the project's virtual environment is already activated, `python Mcp_client.py news_server.py` works too.

Try:

- "What are the latest headlines in Mauritius?"
- "What has been reported about the riots this week?"
- "Translate the latest ION News article into English."

Type `quit` to exit. Without an `OPENCODE_API_KEY`, the client connects, prints the server's tools, and exits, which is useful for checking the MCP wiring.

## Model and quotas

The client talks to OpenCode's OpenAI-compatible endpoint (`https://opencode.ai/inference/go/openai/v1`, for an OpenCode Go subscription) and uses `deepseek-v4.1-flash` by default. Set `OPENCODE_MODEL` in `.env` to use another chat-completions model, e.g. `kimi-k3` or `glm-5.3`, and `OPENCODE_BASE_URL` to change the endpoint.

Use an API key from a workspace subscribed to OpenCode Go. For pay-as-you-go credit instead, set `OPENCODE_BASE_URL=https://opencode.ai/inference/openai/v1`. Free models only work from the OpenCode app itself. Each question takes 2–3 requests, one per tool round trip.

## How the client works

MCP tool input schemas are passed to the model unchanged as JSON Schema, as OpenAI-style function tools. The client runs the tool loop itself, calling the tools over MCP and returning each result as a `tool` message. The model's turns are sent back unchanged so any reasoning fields the provider adds are kept.

`Client(server_params, mode="auto")` launches the server from its `StdioServerParameters`, then probes `server/discover` and falls back to the `2025-11-25` handshake; `client.protocol_version` reports which era you got. See [Protocol versions](https://py.sdk.modelcontextprotocol.io/v2/protocol-versions/).
