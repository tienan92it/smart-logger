"""
Jira local cache: storage layer.

Pure SQLite I/O. No Jira SDK calls, no business logic. Other modules talk to
Jira / build reports on top of this.

Layout
------
~/.smart-logger/jira_cache.db is a single SQLite database with these tables:

- tickets       core ticket fields + raw JSON for forward-compat
- comments      one row per Jira comment
- links         issue relationships (blocks, relates to, ...)
- worklogs      per-entry time logs
- attachments   attachment metadata only (no binaries)
- sync_runs     audit log of sync executions
- tickets_fts   FTS5 virtual table over (key, summary, description, comments)

The store is intentionally additive: syncing a subset of tickets never deletes
unrelated rows. Comments / links / worklogs / attachments of *synced* tickets
are replaced (delete-then-insert) so edits and deletions inside Jira propagate.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional


# --- Paths ------------------------------------------------------------------

CACHE_DIR = Path.home() / ".smart-logger"
DB_PATH = CACHE_DIR / "jira_cache.db"


# --- Schema -----------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    key                  TEXT PRIMARY KEY,
    project              TEXT NOT NULL,
    summary              TEXT,
    description          TEXT,
    description_rendered TEXT,
    status               TEXT,
    status_category      TEXT,
    priority             TEXT,
    issue_type           TEXT,
    assignee             TEXT,
    assignee_email       TEXT,
    reporter             TEXT,
    reporter_email       TEXT,
    parent_key           TEXT,
    epic_key             TEXT,
    labels               TEXT,    -- JSON array
    components           TEXT,    -- JSON array
    fix_versions         TEXT,    -- JSON array
    sprint               TEXT,    -- JSON: latest sprint object
    story_points         REAL,
    time_estimate        INTEGER, -- seconds
    time_spent           INTEGER, -- seconds
    created              TEXT,
    updated              TEXT,
    resolved             TEXT,
    resolution           TEXT,
    raw_json             TEXT,
    last_synced          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tickets_project   ON tickets(project);
CREATE INDEX IF NOT EXISTS idx_tickets_status    ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_assignee  ON tickets(assignee);
CREATE INDEX IF NOT EXISTS idx_tickets_updated   ON tickets(updated);
CREATE INDEX IF NOT EXISTS idx_tickets_parent    ON tickets(parent_key);
CREATE INDEX IF NOT EXISTS idx_tickets_epic      ON tickets(epic_key);

CREATE TABLE IF NOT EXISTS comments (
    id            TEXT PRIMARY KEY,
    ticket_key    TEXT NOT NULL,
    author        TEXT,
    author_email  TEXT,
    body          TEXT,
    body_rendered TEXT,
    created       TEXT,
    updated       TEXT,
    FOREIGN KEY (ticket_key) REFERENCES tickets(key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_comments_ticket ON comments(ticket_key);

CREATE TABLE IF NOT EXISTS links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key  TEXT NOT NULL,
    target_key  TEXT NOT NULL,
    link_type   TEXT NOT NULL,    -- e.g. "Blocks", "Relates", "Duplicate"
    direction   TEXT NOT NULL,    -- "outward" | "inward"
    label       TEXT,             -- human-readable e.g. "is blocked by"
    UNIQUE(source_key, target_key, link_type, direction)
);
CREATE INDEX IF NOT EXISTS idx_links_source ON links(source_key);
CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_key);

CREATE TABLE IF NOT EXISTS worklogs (
    id                  TEXT PRIMARY KEY,
    ticket_key          TEXT NOT NULL,
    author              TEXT,
    author_email        TEXT,
    time_spent_seconds  INTEGER NOT NULL,
    started             TEXT,
    comment             TEXT,
    created             TEXT,
    updated             TEXT,
    FOREIGN KEY (ticket_key) REFERENCES tickets(key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_worklogs_ticket  ON worklogs(ticket_key);
CREATE INDEX IF NOT EXISTS idx_worklogs_started ON worklogs(started);

CREATE TABLE IF NOT EXISTS attachments (
    id          TEXT PRIMARY KEY,
    ticket_key  TEXT NOT NULL,
    filename    TEXT,
    mime_type   TEXT,
    size        INTEGER,
    author      TEXT,
    created     TEXT,
    url         TEXT,
    FOREIGN KEY (ticket_key) REFERENCES tickets(key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_attachments_ticket ON attachments(ticket_key);

CREATE TABLE IF NOT EXISTS sync_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    jql               TEXT,
    tickets_synced    INTEGER DEFAULT 0,
    comments_synced   INTEGER DEFAULT 0,
    links_synced      INTEGER DEFAULT 0,
    worklogs_synced   INTEGER DEFAULT 0,
    attachments_synced INTEGER DEFAULT 0,
    error             TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS tickets_fts USING fts5(
    key, summary, description, comments_concat,
    tokenize='unicode61'
);
"""


# --- Connection management --------------------------------------------------

def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Return a sqlite3 connection with row dicts and foreign keys enabled."""
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(db_path: Optional[Path] = None) -> Path:
    """Create the database file and apply the schema. Idempotent."""
    path = db_path or DB_PATH
    with _connect(path) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()
    return path


@contextmanager
def open_db(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Context manager that ensures schema exists and commits on success."""
    init_db(db_path)
    conn = _connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- Helpers ----------------------------------------------------------------

def _json_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --- Writes -----------------------------------------------------------------

def upsert_ticket(conn: sqlite3.Connection, ticket: dict) -> None:
    """
    Insert or update a single ticket row.

    `ticket` is a dict shaped by jira_sync.normalize_issue() — see that module
    for the canonical field list. Unknown keys are ignored.
    """
    cols = [
        "key", "project", "summary", "description", "description_rendered",
        "status", "status_category", "priority", "issue_type",
        "assignee", "assignee_email", "reporter", "reporter_email",
        "parent_key", "epic_key",
        "labels", "components", "fix_versions", "sprint",
        "story_points", "time_estimate", "time_spent",
        "created", "updated", "resolved", "resolution",
        "raw_json", "last_synced",
    ]
    row = {c: ticket.get(c) for c in cols}
    row["last_synced"] = row.get("last_synced") or _now()
    # Coerce JSON columns
    for c in ("labels", "components", "fix_versions", "sprint", "raw_json"):
        v = row.get(c)
        if v is not None and not isinstance(v, str):
            row[c] = _json_or_none(v)

    placeholders = ", ".join([f":{c}" for c in cols])
    assignments = ", ".join([f"{c} = excluded.{c}" for c in cols if c != "key"])
    sql = (
        f"INSERT INTO tickets ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(key) DO UPDATE SET {assignments}"
    )
    conn.execute(sql, row)


def replace_comments(conn: sqlite3.Connection, ticket_key: str, comments: Iterable[dict]) -> int:
    """Delete existing comments for the ticket, then insert fresh rows."""
    conn.execute("DELETE FROM comments WHERE ticket_key = ?", (ticket_key,))
    count = 0
    for c in comments:
        conn.execute(
            """
            INSERT INTO comments (id, ticket_key, author, author_email, body, body_rendered, created, updated)
            VALUES (:id, :ticket_key, :author, :author_email, :body, :body_rendered, :created, :updated)
            """,
            {
                "id": c.get("id"),
                "ticket_key": ticket_key,
                "author": c.get("author"),
                "author_email": c.get("author_email"),
                "body": c.get("body"),
                "body_rendered": c.get("body_rendered"),
                "created": c.get("created"),
                "updated": c.get("updated"),
            },
        )
        count += 1
    return count


def replace_links(conn: sqlite3.Connection, ticket_key: str, links: Iterable[dict]) -> int:
    """Delete outward+inward links anchored at ticket_key, then insert."""
    conn.execute(
        "DELETE FROM links WHERE source_key = ?",
        (ticket_key,),
    )
    count = 0
    for link in links:
        conn.execute(
            """
            INSERT OR IGNORE INTO links (source_key, target_key, link_type, direction, label)
            VALUES (:source_key, :target_key, :link_type, :direction, :label)
            """,
            {
                "source_key": ticket_key,
                "target_key": link.get("target_key"),
                "link_type": link.get("link_type"),
                "direction": link.get("direction"),
                "label": link.get("label"),
            },
        )
        count += 1
    return count


def replace_worklogs(conn: sqlite3.Connection, ticket_key: str, worklogs: Iterable[dict]) -> int:
    conn.execute("DELETE FROM worklogs WHERE ticket_key = ?", (ticket_key,))
    count = 0
    for w in worklogs:
        conn.execute(
            """
            INSERT INTO worklogs (id, ticket_key, author, author_email, time_spent_seconds, started, comment, created, updated)
            VALUES (:id, :ticket_key, :author, :author_email, :time_spent_seconds, :started, :comment, :created, :updated)
            """,
            {
                "id": w.get("id"),
                "ticket_key": ticket_key,
                "author": w.get("author"),
                "author_email": w.get("author_email"),
                "time_spent_seconds": w.get("time_spent_seconds") or 0,
                "started": w.get("started"),
                "comment": w.get("comment"),
                "created": w.get("created"),
                "updated": w.get("updated"),
            },
        )
        count += 1
    return count


def replace_attachments(conn: sqlite3.Connection, ticket_key: str, attachments: Iterable[dict]) -> int:
    conn.execute("DELETE FROM attachments WHERE ticket_key = ?", (ticket_key,))
    count = 0
    for a in attachments:
        conn.execute(
            """
            INSERT OR REPLACE INTO attachments (id, ticket_key, filename, mime_type, size, author, created, url)
            VALUES (:id, :ticket_key, :filename, :mime_type, :size, :author, :created, :url)
            """,
            {
                "id": a.get("id"),
                "ticket_key": ticket_key,
                "filename": a.get("filename"),
                "mime_type": a.get("mime_type"),
                "size": a.get("size"),
                "author": a.get("author"),
                "created": a.get("created"),
                "url": a.get("url"),
            },
        )
        count += 1
    return count


def refresh_fts(conn: sqlite3.Connection, ticket_key: str) -> None:
    """Rebuild the FTS row for one ticket from the current tickets/comments state."""
    conn.execute("DELETE FROM tickets_fts WHERE key = ?", (ticket_key,))
    row = conn.execute(
        "SELECT key, summary, description FROM tickets WHERE key = ?",
        (ticket_key,),
    ).fetchone()
    if not row:
        return
    comments_concat = " ".join(
        (c["body"] or "")
        for c in conn.execute(
            "SELECT body FROM comments WHERE ticket_key = ?", (ticket_key,)
        ).fetchall()
    )
    conn.execute(
        "INSERT INTO tickets_fts (key, summary, description, comments_concat) VALUES (?, ?, ?, ?)",
        (row["key"], row["summary"] or "", row["description"] or "", comments_concat),
    )


def record_sync_start(conn: sqlite3.Connection, jql: str) -> int:
    cur = conn.execute(
        "INSERT INTO sync_runs (started_at, jql) VALUES (?, ?)",
        (_now(), jql),
    )
    return cur.lastrowid


def record_sync_finish(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    tickets: int = 0,
    comments: int = 0,
    links: int = 0,
    worklogs: int = 0,
    attachments: int = 0,
    error: Optional[str] = None,
) -> None:
    conn.execute(
        """
        UPDATE sync_runs
        SET finished_at = ?, tickets_synced = ?, comments_synced = ?,
            links_synced = ?, worklogs_synced = ?, attachments_synced = ?, error = ?
        WHERE id = ?
        """,
        (_now(), tickets, comments, links, worklogs, attachments, error, run_id),
    )


# --- Reads ------------------------------------------------------------------

def get_ticket(conn: sqlite3.Connection, key: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM tickets WHERE key = ?", (key.upper(),)).fetchone()
    if not row:
        return None
    ticket = dict(row)
    for c in ("labels", "components", "fix_versions", "sprint"):
        v = ticket.get(c)
        if v:
            try:
                ticket[c] = json.loads(v)
            except json.JSONDecodeError:
                pass
    return ticket


def get_comments(conn: sqlite3.Connection, key: str) -> list[dict]:
    return [
        dict(r) for r in conn.execute(
            "SELECT * FROM comments WHERE ticket_key = ? ORDER BY created ASC",
            (key.upper(),),
        ).fetchall()
    ]


def get_links(conn: sqlite3.Connection, key: str) -> list[dict]:
    """Links anchored at this ticket (outward + inward both stored as source=key)."""
    return [
        dict(r) for r in conn.execute(
            "SELECT * FROM links WHERE source_key = ? ORDER BY direction, link_type",
            (key.upper(),),
        ).fetchall()
    ]


def get_worklogs(conn: sqlite3.Connection, key: str) -> list[dict]:
    return [
        dict(r) for r in conn.execute(
            "SELECT * FROM worklogs WHERE ticket_key = ? ORDER BY started ASC",
            (key.upper(),),
        ).fetchall()
    ]


def get_attachments(conn: sqlite3.Connection, key: str) -> list[dict]:
    return [
        dict(r) for r in conn.execute(
            "SELECT * FROM attachments WHERE ticket_key = ? ORDER BY created ASC",
            (key.upper(),),
        ).fetchall()
    ]


_FTS_OPERATOR_PATTERN = re.compile(r'"|\bAND\b|\bOR\b|\bNOT\b|\bNEAR\b|[()*^]')


def _sanitize_fts_query(query: str) -> str:
    """
    Make user input safe for FTS5 MATCH.

    If the query already uses FTS5 syntax (quotes, AND/OR/NOT/NEAR, parens,
    `*`, `^`), pass it through verbatim. Otherwise quote each whitespace-
    separated token so punctuation like `-` doesn't trigger NOT semantics.
    """
    q = query.strip()
    if not q:
        return q
    if _FTS_OPERATOR_PATTERN.search(q):
        return q
    tokens = [t for t in q.split() if t]
    return " ".join(f'"{t.replace(chr(34), chr(34) * 2)}"' for t in tokens)


def search_tickets(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
) -> list[dict]:
    """
    Full-text search across summary + description + comments.

    Plain-word queries are automatically quoted so punctuation (`-`, `:`, etc.)
    does not trigger FTS5 NOT/operator semantics. To use power-user syntax
    (NEAR/N, AND/OR, prefix*, phrase quoting), include any FTS5 operator in
    the query and it will be passed through.

    Returns ticket rows with an extra `snippet` column.
    """
    sanitized = _sanitize_fts_query(query)
    sql = """
        SELECT t.*, snippet(tickets_fts, -1, '[', ']', '...', 8) AS snippet
        FROM tickets_fts
        JOIN tickets t ON t.key = tickets_fts.key
        WHERE tickets_fts MATCH ?
        ORDER BY bm25(tickets_fts)
        LIMIT ?
    """
    return [dict(r) for r in conn.execute(sql, (sanitized, limit)).fetchall()]


_RELATIVE_RE = re.compile(r"^-(\d+)([dwmy])$", re.IGNORECASE)


def resolve_relative_date(value: Optional[str]) -> Optional[str]:
    """
    Normalize a JQL-style or absolute date into ISO `YYYY-MM-DD` (date-only).

    Accepts:
    - `None` / "" → returns None
    - `today`, `yesterday`
    - `-Nd`, `-Nw`, `-Nm`, `-Ny` (days / weeks / months / years)
    - `YYYY-MM-DD` (passed through after validation)

    Anything else is returned unchanged so callers can still pass full timestamps.
    """
    if not value:
        return None
    v = value.strip().lower()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if v == "today":
        return today.strftime("%Y-%m-%d")
    if v == "yesterday":
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")
    m = _RELATIVE_RE.match(v)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = {"d": timedelta(days=n), "w": timedelta(weeks=n), "m": timedelta(days=30 * n), "y": timedelta(days=365 * n)}[unit]
        return (today - delta).strftime("%Y-%m-%d")
    try:
        datetime.strptime(value[:10], "%Y-%m-%d")
        return value[:10]
    except ValueError:
        return value


def query_tickets(
    conn: sqlite3.Connection,
    *,
    project: Optional[str] = None,
    projects: Optional[list[str]] = None,
    status: Optional[list[str]] = None,
    status_category: Optional[list[str]] = None,
    priority: Optional[list[str]] = None,
    issue_type: Optional[list[str]] = None,
    assignee: Optional[str] = None,
    assignee_email: Optional[str] = None,
    reporter_email: Optional[str] = None,
    updated_after: Optional[str] = None,
    updated_before: Optional[str] = None,
    created_after: Optional[str] = None,
    labels: Optional[list[str]] = None,
    text_search: Optional[str] = None,
    open_only: bool = False,
    order_by: str = "updated",
    order_dir: str = "DESC",
    limit: int = 50,
) -> list[dict]:
    """
    Flexible filter-based query against the local cache.

    All filters are AND-combined. List filters become SQL `IN (...)`. `labels`
    requires *all* listed labels to be present on the ticket (AND match).
    `text_search` runs the FTS5 index and restricts the result set to its keys.

    Designed to be the single read path for the AI orchestrator — every leak
    to Jira's live API for *reads* should funnel through here.
    """
    conditions: list[str] = []
    params: list[Any] = []

    def in_clause(col: str, values: list[str]) -> None:
        placeholders = ",".join(["?"] * len(values))
        conditions.append(f"{col} IN ({placeholders})")
        params.extend(values)

    project_keys = [p.upper() for p in (projects or []) if p]
    if project:
        project_keys.append(project.upper())
    if project_keys:
        in_clause("project", project_keys)

    if status:
        in_clause("status", status)
    if status_category:
        in_clause("status_category", status_category)
    if priority:
        in_clause("priority", priority)
    if issue_type:
        in_clause("issue_type", issue_type)

    if assignee:
        conditions.append("LOWER(assignee) = LOWER(?)")
        params.append(assignee)
    if assignee_email:
        conditions.append("LOWER(assignee_email) = LOWER(?)")
        params.append(assignee_email)
    if reporter_email:
        conditions.append("LOWER(reporter_email) = LOWER(?)")
        params.append(reporter_email)

    ua = resolve_relative_date(updated_after)
    if ua:
        conditions.append("updated >= ?")
        params.append(ua)
    ub = resolve_relative_date(updated_before)
    if ub:
        conditions.append("updated < ?")
        params.append(ub)
    ca = resolve_relative_date(created_after)
    if ca:
        conditions.append("created >= ?")
        params.append(ca)

    if labels:
        # Stored as JSON arrays; use LIKE on the JSON text. Each label adds an AND.
        for label in labels:
            conditions.append("labels LIKE ?")
            params.append(f'%"{label}"%')

    if open_only:
        conditions.append("(status_category IS NULL OR status_category != 'Done')")

    if text_search:
        keys = [r["key"] for r in search_tickets(conn, text_search, limit=500)]
        if not keys:
            return []
        placeholders = ",".join(["?"] * len(keys))
        conditions.append(f"key IN ({placeholders})")
        params.extend(keys)

    allowed_order_cols = {
        "updated", "created", "resolved", "priority", "status", "key", "time_spent",
    }
    order_col = order_by if order_by in allowed_order_cols else "updated"
    order_dir_sql = "ASC" if str(order_dir).upper() == "ASC" else "DESC"

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(int(limit))
    rows = conn.execute(
        f"SELECT * FROM tickets {where} ORDER BY {order_col} {order_dir_sql} LIMIT ?",
        params,
    ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        for c in ("labels", "components", "fix_versions", "sprint"):
            v = d.get(c)
            if v:
                try:
                    d[c] = json.loads(v)
                except json.JSONDecodeError:
                    pass
        out.append(d)
    return out


def list_tickets(
    conn: sqlite3.Connection,
    *,
    project: Optional[str] = None,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    updated_since: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    conditions = []
    params: list[Any] = []
    if project:
        conditions.append("project = ?")
        params.append(project.upper())
    if status:
        conditions.append("status = ?")
        params.append(status)
    if assignee:
        conditions.append("assignee = ?")
        params.append(assignee)
    if updated_since:
        conditions.append("updated >= ?")
        params.append(updated_since)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM tickets {where} ORDER BY updated DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def db_stats(conn: sqlite3.Connection) -> dict:
    def count(table: str) -> int:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    last_run = conn.execute(
        "SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()

    return {
        "db_path": str(DB_PATH),
        "tickets": count("tickets"),
        "comments": count("comments"),
        "links": count("links"),
        "worklogs": count("worklogs"),
        "attachments": count("attachments"),
        "last_sync": dict(last_run) if last_run else None,
    }
