"""
Jira local cache: analytics layer.

Read-only queries against the SQLite cache. No Jira API calls, no I/O outside
of SQLite. Each function returns a plain Python data structure so callers
(CLI, MCP, skills) can render it however they want.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Optional

import jira_store as store


def _date_only(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return value[:10]


def counts_by_status(project: Optional[str] = None) -> list[dict]:
    """Return open vs closed counts per status, optionally scoped to a project."""
    with store.open_db() as conn:
        params: list = []
        where = ""
        if project:
            where = "WHERE project = ?"
            params.append(project.upper())
        rows = conn.execute(
            f"""
            SELECT status, status_category, COUNT(*) AS n
            FROM tickets {where}
            GROUP BY status, status_category
            ORDER BY n DESC
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def counts_by_project() -> list[dict]:
    with store.open_db() as conn:
        rows = conn.execute(
            "SELECT project, COUNT(*) AS n FROM tickets GROUP BY project ORDER BY n DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def counts_by_assignee(project: Optional[str] = None, limit: int = 20) -> list[dict]:
    with store.open_db() as conn:
        params: list = []
        where_parts = ["assignee IS NOT NULL"]
        if project:
            where_parts.append("project = ?")
            params.append(project.upper())
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT assignee, COUNT(*) AS n
            FROM tickets
            WHERE {' AND '.join(where_parts)}
            GROUP BY assignee
            ORDER BY n DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def worklog_summary(
    *,
    author_email: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> dict:
    """Aggregate worklog seconds by date / project / ticket within a window."""
    with store.open_db() as conn:
        params: list = []
        where_parts = ["1=1"]
        if author_email:
            where_parts.append("LOWER(w.author_email) = LOWER(?)")
            params.append(author_email)
        if start:
            where_parts.append("w.started >= ?")
            params.append(start)
        if end:
            where_parts.append("w.started < ?")
            params.append(end)
        where = " AND ".join(where_parts)

        rows = conn.execute(
            f"""
            SELECT w.ticket_key, w.time_spent_seconds, w.started, t.summary, t.project
            FROM worklogs w
            LEFT JOIN tickets t ON t.key = w.ticket_key
            WHERE {where}
            ORDER BY w.started ASC
            """,
            params,
        ).fetchall()

    by_date: dict[str, int] = defaultdict(int)
    by_project: dict[str, int] = defaultdict(int)
    by_ticket: dict[str, dict] = {}
    total = 0

    for r in rows:
        seconds = r["time_spent_seconds"] or 0
        total += seconds
        d = _date_only(r["started"])
        if d:
            by_date[d] += seconds
        proj = r["project"] or (r["ticket_key"].split("-")[0] if r["ticket_key"] else "?")
        by_project[proj] += seconds
        info = by_ticket.setdefault(
            r["ticket_key"],
            {"key": r["ticket_key"], "summary": r["summary"], "seconds": 0},
        )
        info["seconds"] += seconds

    return {
        "total_seconds": total,
        "by_date": dict(sorted(by_date.items())),
        "by_project": dict(sorted(by_project.items(), key=lambda x: x[1], reverse=True)),
        "by_ticket": sorted(by_ticket.values(), key=lambda x: x["seconds"], reverse=True),
    }


def stale_tickets(
    *,
    days: int = 14,
    project: Optional[str] = None,
    open_only: bool = True,
    limit: int = 25,
) -> list[dict]:
    """Tickets that haven't moved in N days. Useful for triage reports."""
    with store.open_db() as conn:
        params: list = []
        where_parts = []
        if open_only:
            where_parts.append("(status_category IS NULL OR status_category != 'Done')")
        if project:
            where_parts.append("project = ?")
            params.append(project.upper())
        params.extend([days, limit])
        where = " AND ".join(where_parts) if where_parts else "1=1"
        rows = conn.execute(
            f"""
            SELECT key, summary, status, priority, assignee, updated
            FROM tickets
            WHERE {where}
              AND updated IS NOT NULL
              AND julianday('now') - julianday(updated) >= ?
            ORDER BY updated ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def relationship_graph(key: str, depth: int = 1) -> dict:
    """
    Return the local relationship graph for a ticket as nodes + edges.

    depth = 1 → direct neighbors only
    depth = 2 → also neighbors-of-neighbors
    """
    key = key.upper()
    visited: set[str] = set()
    edges: list[dict] = []

    def expand(node_key: str, remaining: int) -> None:
        if node_key in visited or remaining < 0:
            return
        visited.add(node_key)
        if remaining == 0:
            return
        with store.open_db() as conn:
            rows = conn.execute(
                """
                SELECT source_key, target_key, link_type, direction, label
                FROM links
                WHERE source_key = ? OR target_key = ?
                """,
                (node_key, node_key),
            ).fetchall()
        for r in rows:
            edge = dict(r)
            edges.append(edge)
            other = edge["target_key"] if edge["source_key"] == node_key else edge["source_key"]
            if other and other not in visited:
                expand(other, remaining - 1)

    expand(key, depth)

    nodes: list[dict] = []
    with store.open_db() as conn:
        for nk in visited:
            row = conn.execute(
                "SELECT key, summary, status, priority FROM tickets WHERE key = ?",
                (nk,),
            ).fetchone()
            nodes.append(dict(row) if row else {"key": nk, "summary": None, "status": None, "priority": None})

    return {"root": key, "nodes": nodes, "edges": edges}


def top_labels(limit: int = 20) -> list[dict]:
    """Most common labels across the cache (label → ticket count)."""
    with store.open_db() as conn:
        rows = conn.execute("SELECT labels FROM tickets WHERE labels IS NOT NULL").fetchall()
    counter: Counter = Counter()
    for r in rows:
        try:
            labels = json.loads(r["labels"])
        except (json.JSONDecodeError, TypeError):
            continue
        for label in labels or []:
            counter[label] += 1
    return [{"label": k, "count": v} for k, v in counter.most_common(limit)]


# ---------------------------------------------------------------------------
# Higher-level reports (for the jira-analyst skill / `smart-log jira-report`)
# ---------------------------------------------------------------------------

def _user_scope_where(assignee_email: Optional[str], project: Optional[str], open_only: bool) -> tuple[str, list]:
    parts: list[str] = []
    params: list = []
    if assignee_email:
        parts.append("LOWER(assignee_email) = LOWER(?)")
        params.append(assignee_email)
    if project:
        parts.append("project = ?")
        params.append(project.upper())
    if open_only:
        parts.append("(status_category IS NULL OR status_category != 'Done')")
    where = (" AND ".join(parts)) if parts else "1=1"
    return where, params


def workload_digest(
    *,
    assignee_email: Optional[str] = None,
    project: Optional[str] = None,
    recent_days: int = 7,
    top_n: int = 5,
) -> dict:
    """
    Compact dashboard for one user / project:
    counts by status_category, priority, project, epic; top priorities and
    most recently active open tickets; stale count; time spent in the recent
    window vs. the prior window.
    """
    where, params = _user_scope_where(assignee_email, project, open_only=True)

    with store.open_db() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM tickets WHERE {where}", params).fetchone()["n"]

        by_status = [
            dict(r) for r in conn.execute(
                f"SELECT status_category, status, COUNT(*) AS n FROM tickets WHERE {where} "
                f"GROUP BY status_category, status ORDER BY n DESC",
                params,
            ).fetchall()
        ]

        by_priority = [
            dict(r) for r in conn.execute(
                f"SELECT priority, COUNT(*) AS n FROM tickets WHERE {where} "
                f"GROUP BY priority ORDER BY CASE priority "
                f"WHEN 'Highest' THEN 1 WHEN 'High' THEN 2 WHEN 'Medium' THEN 3 "
                f"WHEN 'Low' THEN 4 WHEN 'Lowest' THEN 5 ELSE 6 END",
                params,
            ).fetchall()
        ]

        by_project = [
            dict(r) for r in conn.execute(
                f"SELECT project, COUNT(*) AS n FROM tickets WHERE {where} "
                f"GROUP BY project ORDER BY n DESC",
                params,
            ).fetchall()
        ]

        by_type = [
            dict(r) for r in conn.execute(
                f"SELECT issue_type, COUNT(*) AS n FROM tickets WHERE {where} "
                f"GROUP BY issue_type ORDER BY n DESC",
                params,
            ).fetchall()
        ]

        top_priorities = [
            dict(r) for r in conn.execute(
                f"SELECT key, summary, status, priority, updated, epic_key FROM tickets "
                f"WHERE {where} AND priority IN ('Highest', 'High') "
                f"ORDER BY CASE priority WHEN 'Highest' THEN 1 ELSE 2 END, updated DESC LIMIT ?",
                params + [top_n],
            ).fetchall()
        ]

        recent_activity = [
            dict(r) for r in conn.execute(
                f"SELECT key, summary, status, priority, updated FROM tickets "
                f"WHERE {where} ORDER BY updated DESC LIMIT ?",
                params + [top_n * 2],
            ).fetchall()
        ]

        stale_count = conn.execute(
            f"SELECT COUNT(*) AS n FROM tickets "
            f"WHERE {where} AND updated IS NOT NULL "
            f"AND julianday('now') - julianday(updated) >= 14",
            params,
        ).fetchone()["n"]

    recent_summary = worklog_summary(
        author_email=assignee_email,
        start=_n_days_ago(recent_days),
        end=_n_days_ago(0),
    )
    prev_summary = worklog_summary(
        author_email=assignee_email,
        start=_n_days_ago(recent_days * 2),
        end=_n_days_ago(recent_days),
    )

    return {
        "scope": {"assignee_email": assignee_email, "project": project},
        "total_open": total,
        "by_status_category": by_status,
        "by_priority": by_priority,
        "by_project": by_project,
        "by_type": by_type,
        "top_priorities": top_priorities,
        "recent_activity": recent_activity,
        "stale_count": stale_count,
        "time_spent_recent_seconds": recent_summary["total_seconds"],
        "time_spent_prev_seconds": prev_summary["total_seconds"],
        "recent_window_days": recent_days,
    }


def epic_rollup(
    *,
    assignee_email: Optional[str] = None,
    project: Optional[str] = None,
    open_only: bool = False,
    limit: int = 50,
) -> list[dict]:
    """
    Group cached tickets by `epic_key`.

    Returns per epic: total tickets, done count, percent_complete, time spent
    (seconds), last update, and the epic's own summary if it's also cached.
    Tickets without an epic are collapsed into a single `(no epic)` row.
    """
    where, params = _user_scope_where(assignee_email, project, open_only)

    with store.open_db() as conn:
        rows = conn.execute(
            f"""
            SELECT
                COALESCE(t.epic_key, '(no epic)') AS epic_key,
                COUNT(*) AS total,
                SUM(CASE WHEN status_category = 'Done' THEN 1 ELSE 0 END) AS done,
                SUM(COALESCE(time_spent, 0)) AS total_seconds,
                MAX(updated) AS last_updated
            FROM tickets t
            WHERE {where}
            GROUP BY COALESCE(t.epic_key, '(no epic)')
            ORDER BY total DESC
            LIMIT ?
            """,
            params + [limit],
        ).fetchall()

        results = []
        for r in rows:
            row = dict(r)
            epic_summary = None
            epic_status = None
            if row["epic_key"] != "(no epic)":
                er = conn.execute(
                    "SELECT summary, status FROM tickets WHERE key = ?",
                    (row["epic_key"],),
                ).fetchone()
                if er:
                    epic_summary = er["summary"]
                    epic_status = er["status"]
            total = row["total"] or 0
            done = row["done"] or 0
            row["percent_complete"] = round((done / total) * 100, 1) if total else 0.0
            row["epic_summary"] = epic_summary
            row["epic_status"] = epic_status
            results.append(row)
        return results


def daily_plan(
    *,
    assignee_email: str,
    project: Optional[str] = None,
    stale_in_progress_days: int = 7,
) -> dict:
    """
    Recommended focus list for today.

    Buckets (all scoped to one assignee):
    - `in_progress`         active tickets, ordered by recent activity
    - `next_up`             High/Highest priority To-Do tickets
    - `stale_in_progress`   in-progress tickets idle >= N days (unblock these)
    - `blocked`             status name contains "Block" OR has inward "Blocks" link
    """
    where, params = _user_scope_where(assignee_email, project, open_only=True)

    with store.open_db() as conn:
        in_progress = [
            dict(r) for r in conn.execute(
                f"SELECT key, summary, status, priority, updated, epic_key, time_spent FROM tickets "
                f"WHERE {where} AND status_category = 'In Progress' ORDER BY updated DESC",
                params,
            ).fetchall()
        ]
        next_up = [
            dict(r) for r in conn.execute(
                f"SELECT key, summary, status, priority, updated, epic_key FROM tickets "
                f"WHERE {where} AND status_category = 'To Do' AND priority IN ('Highest', 'High') "
                f"ORDER BY CASE priority WHEN 'Highest' THEN 1 ELSE 2 END, updated DESC",
                params,
            ).fetchall()
        ]
        stale_in_progress = [
            dict(r) for r in conn.execute(
                f"SELECT key, summary, status, priority, updated FROM tickets "
                f"WHERE {where} AND status_category = 'In Progress' AND updated IS NOT NULL "
                f"AND julianday('now') - julianday(updated) >= ? ORDER BY updated ASC",
                params + [stale_in_progress_days],
            ).fetchall()
        ]
        blocked = [
            dict(r) for r in conn.execute(
                f"""
                SELECT DISTINCT t.key, t.summary, t.status, t.priority, t.updated
                FROM tickets t
                LEFT JOIN links l ON l.source_key = t.key AND l.direction = 'inward' AND LOWER(l.link_type) LIKE '%block%'
                WHERE {where} AND (LOWER(t.status) LIKE '%block%' OR l.id IS NOT NULL)
                ORDER BY t.updated DESC
                """,
                params,
            ).fetchall()
        ]

    return {
        "scope": {"assignee_email": assignee_email, "project": project},
        "in_progress": in_progress,
        "next_up": next_up,
        "stale_in_progress": stale_in_progress,
        "blocked": blocked,
    }


def scope_distribution(
    *,
    field: str = "components",
    project: Optional[str] = None,
    assignee_email: Optional[str] = None,
    open_only: bool = False,
    limit: int = 25,
) -> list[dict]:
    """
    Count tickets per value of a JSON-array column.

    `field` ∈ {"components", "labels", "fix_versions"}. Useful for "what scopes
    am I working in" or "where is effort concentrated".
    """
    if field not in {"components", "labels", "fix_versions"}:
        raise ValueError(f"Unsupported field for scope_distribution: {field}")

    where, params = _user_scope_where(assignee_email, project, open_only)
    with store.open_db() as conn:
        rows = conn.execute(
            f"SELECT {field} FROM tickets WHERE {where} AND {field} IS NOT NULL",
            params,
        ).fetchall()

    counter: Counter = Counter()
    for r in rows:
        try:
            values = json.loads(r[field])
        except (json.JSONDecodeError, TypeError):
            continue
        for v in values or []:
            counter[v] += 1
    return [{"value": k, "count": v} for k, v in counter.most_common(limit)]


# --- Internal date helpers --------------------------------------------------

def _n_days_ago(days: int) -> str:
    from datetime import datetime, timedelta
    d = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
    return d.strftime("%Y-%m-%d")
