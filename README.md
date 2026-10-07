# EML Archive

Enterprise mail-fact service: it parses **EML** files into structured,
searchable records. FastAPI + Python's **`email` standard library** for
parsing, **PostgreSQL** for headers/relationships/body facts, and a
**controlled local directory** for raw messages and attachment bytes.

There is **no frontend** — JSON HTTP API only.

## Security model

| Concern | How it is handled |
|---|---|
| Attachment path escape | Storage root is resolved once; sender-supplied names are reduced to one safe basename (`app/storage.py::safe_basename`) and bytes are written to **content-addressed** shard names. Every path is re-validated with `Path.relative_to(root)`. Downloads re-validate the stored path and serve a sanitized `Content-Disposition` filename. |
| HTML / scripts / remote resources | HTML is stored only as: sanitized allow-list HTML (`safe_html`), fully escaped HTML (`escaped_html`), and extracted plain text. The stdlib allow-list sanitizer (`app/parser/html_sanitizer.py`) removes `<script>`, `<style>`, `<iframe>`, event handlers, `style=`/`class=`/`data=`, and **every** loading URL except inline `cid:` resources. Remote `http(s)`, protocol-relative, `data:`, `vbscript:` and `javascript:` loaders are stripped. |
| Inline resources | `multipart/related` / `Content-ID` parts are parsed as binary attachments with `content_id`; HTML parts record `referenced_cids` — links are facts, nothing is fetched. |
| Attachment bytes in logs | Only metadata is logged (content type, size, sha256, relative path). A test asserts payload markers never appear in log records. |
| Upload size | Hard streaming cap (`EMLARCH_MAX_UPLOAD_BYTES`) before persistence. |
| SQL | psycopg3 parameterized statements throughout; no string-built DML. |
| File modes | Stored files default to `0600`. |

## Parsing behavior

* **Multi-layer MIME**: a full structural tree (`multipart/mixed → alternative → related`, `message/rfc822` forwarded messages recursed) with stable MIME paths (`1.2`, `1.3.1`, `2.m1`).
* **Charsets**: declared charset is used when valid; on failure a recorded
  fallback ladder runs (`utf-8 → gb18030 → big5 → shift_jis → iso-8859-1`).
  Every substitution is a defect with its MIME stage.
* **Attachment names**: RFC2047/RFC2231 decoded display name kept **and** the
  raw encoded header preserved for forensics.
* **Defects are locatable**: boundary mismatches, unknown charsets, invalid
  dates, malformed Message-IDs, bad CTEs are collected per stage rather than
  raised. Results are `ok` / `defective` / `failed`; even `failed` uploads get
  an ingest row with the raw digest and defects.
* **Provenance**: each ingest stores the raw EML sha256, size, on-disk path,
  the parsed result references the same digest, and both record the
  `PARSER_VERSION` (`app/parser/eml_parser.py`) that produced the facts.

## Reparse comparison previews

After a parser upgrade the archivist can see how an *old* message would be
re-interpreted **without re-ingesting it**. Implemented in `app/preview.py`:

* `POST /messages/{id}/reparse-preview` reads the **saved original bytes** from
  controlled raw storage, runs the *current* parser, and diffs the fresh result
  against the archived facts in five sections: **headers, bodies, attachments,
  defects, thread references**.
* Each preview records the **current parser version**, the **archived parser
  version**, and an **original-text summary** (archived/on-disk sha256 + size,
  digest match, availability).
* It is **not** an ingest: no ingest/message rows are created, no attachment is
  written, stored download paths are unchanged, and no `thread_key` is
  reassigned. The thread section only *projects* the merge/split the fresh
  `Message-ID`/`References`/`In-Reply-To` tokens would cause (membership diff
  against the archived thread).
* **Stable**: previews are idempotent on
  `(message, archived raw sha256, parser version)` — repeating a preview for
  the same original returns the same row (same id/created-at) and a byte-stable
  diff.
* When the saved original is missing, the preview is marked `raw_missing` with
  `diff = null` and a reason; the old metadata (digest, size, path, facts)
  remains fully in place and searchable. Restoring the bytes recovers the
  preview on the same row.
* Query via `GET /messages/{id}/reparse-preview` (latest),
  `GET /reparse-previews` (list), `GET /reparse-previews/{id}`.

## Threading / conversations

Implemented in `app/threads.py` (pure function, unit tested):

* Edges come **only** from `Message-ID`, `References`, `In-Reply-To`.
* Same/normalized subjects are **weak candidates** — surfaced as
  `weak_suggestions` (with a recency window), **never auto-merged**.
* **Duplicate** Message-IDs are retained as a conflict (`duplicate_ids`) —
  messages are not merged into one row; the `messages.message_id` column is
  deliberately not unique.
* **Missing** Message-ID rows are flagged `missing_id` and only join a thread
  through reference tokens.
* Reference cycles are detected with bounded three-color DFS (`cycles`);
  dangling references (`dangling_references`) are reported, not hidden.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/ingest` | multipart upload of one `.eml`; returns status, digest, parts, attachments, threading report |
| GET | `/messages` / `/messages/{id}` | list / full detail (tree, bodies, attachments, defects) |
| GET | `/messages/{id}/attachments/{aid}/download` | stream attachment bytes (path re-validated) |
| POST | `/messages/{id}/reparse-preview` | re-parse the saved original with the current parser and diff vs. archive (read-only, not an ingest) |
| GET | `/messages/{id}/reparse-preview` | latest preview for one message |
| GET | `/reparse-previews` / `/reparse-previews/{id}` | list / fetch stored previews |
| GET | `/search?q=` | substring over subject, Message-ID, all header values, body plain text |
| GET | `/threads` / `/threads/{key}` | thread summaries / ordered members with reference headers |
| POST | `/threads/rebuild` | recompute all threads; returns conflicts/cycles/dangling/weak hints |
| GET | `/ingests/{id}` | provenance: raw digest/path + every defect located by stage |
| GET | `/failures` | failed/defective ingests with their defect lists |
| GET | `/health` | backend and configured storage roots |

## Running

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt

export EMLARCH_DATABASE_DSN="postgresql://user:pass@localhost:5432/emlarch"
export EMLARCH_ATTACHMENT_DIR=/var/lib/emlarchive/attachments
export EMLARCH_RAW_DIR=/var/lib/emlarchive/raw
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080
```

The schema is applied automatically on startup (`app/sql/schema.sql`).
Without a DSN the service boots an in-memory repository (useful for demos).

### Generate boundary samples

```bash
.venv/bin/python scripts/generate_samples.py
ls samples/
# 01 multibyte/nested, 02 circular refs, 03 missing id + bogus charset,
# 04 duplicate ids, 05 same subject different threads, 06 corrupt boundary,
# 07 bad CTE, 08 path traversal attachment, 09 XSS/remote HTML
```

### Tests

```bash
.venv/bin/python -m pytest                       # 73 unit + API tests (memory backend)
EMLARCH_RUN_PG_TESTS=1 EMLARCH_TEST_DSN='postgresql://postgres@/postgres?host=/tmp/pgsock&port=55432' \
  .venv/bin/python -m pytest                     # + real PostgreSQL integration tests
```

### Quick manual check

```bash
curl -F "file=@samples/01_multibyte.eml" http://127.0.0.1:8080/ingest
curl "http://127.0.0.1:8080/search?q=GB18030"
```

## Layout

```
app/
  config.py            env-driven configuration (roots, cap, DSN)
  parser/
    eml_parser.py      stdlib email parsing, structural walk, charset ladder
    html_sanitizer.py  allow-list sanitizer + escaping + text extraction
    models.py          structured result dataclasses
  storage.py           ControlledStorage (path safety, 0600, metadata logs)
  threads.py           Message-ID graph + cycles + conflicts + weak subjects
  preview.py           reparse-comparison previews (five-section diff, read-only)
  pg_repository.py     PostgreSQL persistence (psycopg3)
  memory_repository.py same interface, in-memory (tests / demo)
  service.py           parse -> store -> persist -> thread orchestration
  main.py / schemas.py FastAPI app and response models
  sql/schema.sql       DDL (ids intentionally non-unique)
scripts/generate_samples.py
samples/               generated edge-case EML files
tests/                 unit, API and PostgreSQL integration tests
```
