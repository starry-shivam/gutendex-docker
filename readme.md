Gutendex Next
=============

A rewrite of the [Gutendex](https://github.com/garethbjohnson/gutendex) API in **FastAPI + SQLite**, built for lower resource usage and faster response times.

Self-hosted [web API](https://en.wikipedia.org/wiki/Web_API) for serving book catalog data from
[Project Gutenberg](https://www.gutenberg.org/wiki/Main_Page). Gutendex Next is a **100% drop-in
replacement** — same endpoints, query parameters, and response format, so existing clients require
no changes.


Quick Start
-----------

**Requirements:** Docker + Docker Compose

```bash
# 1. Clone and enter the project
git clone https://github.com/starry-shivam/gutendex-next.git
cd gutendex-docker

# 2. Create data directories
mkdir -p data

# 3. Build and start
docker compose up -d

# 4. Import the Gutenberg catalog (one-time, takes 20-30 min)
docker compose exec gutendex-next python catalog/updatecatalog.py

# 5. Test it
curl http://localhost:5073
```

The API is now live at `http://localhost:5073`.

To update the catalog in the future, re-run step 4.


How does it work?
-----------------

Gutendex uses [FastAPI](https://fastapi.tiangolo.com) to serve book catalog data in a simple
[JSON](http://json.org) [REST](https://en.wikipedia.org/wiki/Representational_state_transfer) API.

Project Gutenberg publishes nightly archives of complex XML files. Gutendex downloads these, stores the
data in a local SQLite database, and exposes it in a clean format.


API
---

The home page is at the root URL (`http://localhost:5073`).


### Lists of Books

`GET /books`

Returns paginated book data:

```json
{
  "count": 70000,
  "next": "/books?page=2",
  "previous": null,
  "results": [...]
}
```

`results` contains up to 32 books by default, ordered by download count. Use `page` and
`page_size` to paginate.

#### Query Parameters

| Parameter | Description |
|-----------|-------------|
| `author_year_start` | Books with an author alive on or after this year |
| `author_year_end` | Books with an author alive on or before this year |
| `copyright` | `true`, `false`, or `null` — combinable with commas |
| `ids` | Comma-separated Project Gutenberg IDs, e.g. `ids=11,12,13` |
| `languages` | Comma-separated language codes, e.g. `languages=en,fr` |
| `mime_type` | Formats starting with this MIME type, e.g. `mime_type=text/plain` |
| `search` | Space-separated words to search in titles and author names |
| `sort` | `ascending` or `descending` by ID; default is by download count |
| `topic` | Case-insensitive search in bookshelves and subjects |
| `page` | Page number (default: `1`) |
| `page_size` | Results per page (default: `32`, max: `100`) |


### Individual Books

`GET /books/<id>`

Returns a single book by its Project Gutenberg ID.


### API Objects

#### Book

```json
{
  "id": 11,
  "title": "Alice's Adventures in Wonderland",
  "authors": [{"name": "Carroll, Lewis", "birth_year": 1832, "death_year": 1898}],
  "summaries": [],
  "editors": [],
  "translators": [],
  "subjects": ["Fantasy fiction"],
  "bookshelves": ["Children's Literature"],
  "languages": ["en"],
  "copyright": false,
  "media_type": "Text",
  "formats": {"text/html": "https://...", "application/epub+zip": "https://..."},
  "download_count": 35000
}
```

#### Person

```json
{"birth_year": 1832, "death_year": 1898, "name": "Carroll, Lewis"}
```

