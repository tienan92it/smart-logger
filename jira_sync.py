"""
Jira local cache: ingest layer.

Pulls issues from the Jira REST API and writes them to the local SQLite store
in `jira_store.py`. No reporting / display / business logic here — callers
(CLI, MCP, skills) decide *what* to sync and *what to do with* the cache.

Pull strategy
-------------
- Default JQL targets the current user's work in the last 180 days:
      (assignee = currentUser() OR reporter = currentUser()) AND updated >= -180d
- The caller can override with explicit JQL, project key, or `--since` date.
- Pagination uses Jira's startAt/maxResults; fields are fetched in one call
  with `*all` plus `expand=renderedFields` so descriptions/comments come back
  rendered to HTML alongside their raw form.
- Worklogs are pulled per-ticket via `jira.worklogs(key)` to bypass the
  20-entry cap on search responses.

Custom fields (Sprint, Story Points, Epic Link) are resolved by *name* on
first use because their field IDs differ across Jira instances. The lookup is
cached on the JiraSyncer instance.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable, Optional

from jira import JIRA
from jira.resources import Issue
from jira.exceptions import JIRAError

import jira_store as store


# --- Custom field resolution ------------------------------------------------

_SPRINT_NAMES = {"sprint"}
_STORY_POINTS_NAMES = {"story points", "story point estimate"}
_EPIC_LINK_NAMES = {"epic link", "parent link"}


def _resolve_custom_fields(jira: JIRA) -> dict[str, Optional[str]]:
    """Map known custom fields by lowercased name to their Jira field IDs."""
    resolved: dict[str, Optional[str]] = {"sprint": None, "story_points": None, "epic_link": None}
    try:
        for field_meta in jira.fields():
            name = (field_meta.get("name") or "").lower()
            fid = field_meta.get("id")
            if name in _SPRINT_NAMES and not resolved["sprint"]:
                resolved["sprint"] = fid
            elif name in _STORY_POINTS_NAMES and not resolved["story_points"]:
                resolved["story_points"] = fid
            elif name in _EPIC_LINK_NAMES and not resolved["epic_link"]:
                resolved["epic_link"] = fid
    except Exception:
        pass
    return resolved


# --- ADF flattening ---------------------------------------------------------

def _flatten_adf(node) -> str:
    """Recursively pull plain text out of Atlassian Document Format nodes."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(_flatten_adf(x) for x in node).strip()
    if isinstance(node, dict):
        if "text" in node and isinstance(node["text"], str):
            return node["text"]
        if "content" in node:
            return _flatten_adf(node["content"])
    return ""


def _as_text(value) -> Optional[str]:
    """Coerce ADF dict / list / str into a plain text string."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        text = _flatten_adf(value)
        return text or json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


# --- Normalization ----------------------------------------------------------

@dataclass
class NormalizedIssue:
    ticket: dict
    comments: list[dict] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)


def _safe(obj, *attrs, default=None):
    """Chase a dotted attribute path; return default if anything is missing."""
    cur = obj
    for a in attrs:
        if cur is None:
            return default
        cur = getattr(cur, a, None)
    return cur if cur is not None else default


def _extract_user(user) -> tuple[Optional[str], Optional[str]]:
    if not user:
        return None, None
    name = getattr(user, "displayName", None) or getattr(user, "name", None)
    email = getattr(user, "emailAddress", None)
    return name, email


def _extract_sprint(field_value) -> Optional[dict]:
    """Sprint field returns a list of sprint objects; keep the latest."""
    if not field_value:
        return None
    if isinstance(field_value, list) and field_value:
        last = field_value[-1]
        if isinstance(last, dict):
            return {
                "id": last.get("id"),
                "name": last.get("name"),
                "state": last.get("state"),
                "startDate": last.get("startDate"),
                "endDate": last.get("endDate"),
            }
        # Some Jira versions return strings like "com.atlassian.greenhopper...id=42,name=Sprint 3,state=closed..."
        if isinstance(last, str):
            match = re.search(r"name=([^,\]]+)", last)
            state = re.search(r"state=([^,\]]+)", last)
            return {"name": match.group(1) if match else last, "state": state.group(1) if state else None}
    return None


def normalize_issue(
    issue: Issue,
    *,
    custom_fields: dict[str, Optional[str]],
    rendered: Optional[dict] = None,
) -> NormalizedIssue:
    """Convert a jira-python Issue into the shape jira_store expects."""
    fields = issue.fields
    raw = getattr(issue, "raw", {}) or {}
    raw_fields = raw.get("fields", {})

    assignee_name, assignee_email = _extract_user(getattr(fields, "assignee", None))
    reporter_name, reporter_email = _extract_user(getattr(fields, "reporter", None))

    description = _as_text(getattr(fields, "description", None))
    description_rendered = (rendered or {}).get("description")

    sprint_value = None
    story_points = None
    epic_key = None
    if custom_fields.get("sprint"):
        sprint_value = _extract_sprint(raw_fields.get(custom_fields["sprint"]))
    if custom_fields.get("story_points"):
        sp = raw_fields.get(custom_fields["story_points"])
        try:
            story_points = float(sp) if sp is not None else None
        except (TypeError, ValueError):
            story_points = None
    if custom_fields.get("epic_link"):
        epic_key = raw_fields.get(custom_fields["epic_link"])
    # Newer Jira: parent field can hold the epic
    if not epic_key:
        parent = raw_fields.get("parent") or {}
        parent_type = ((parent.get("fields") or {}).get("issuetype") or {}).get("name")
        if isinstance(parent_type, str) and parent_type.lower() == "epic":
            epic_key = parent.get("key")

    parent_key = _safe(fields, "parent", "key")

    ticket = {
        "key": issue.key,
        "project": issue.key.split("-")[0],
        "summary": getattr(fields, "summary", None),
        "description": description,
        "description_rendered": description_rendered,
        "status": _safe(fields, "status", "name"),
        "status_category": _safe(fields, "status", "statusCategory", "name"),
        "priority": _safe(fields, "priority", "name"),
        "issue_type": _safe(fields, "issuetype", "name"),
        "assignee": assignee_name,
        "assignee_email": assignee_email,
        "reporter": reporter_name,
        "reporter_email": reporter_email,
        "parent_key": parent_key,
        "epic_key": epic_key,
        "labels": list(getattr(fields, "labels", []) or []),
        "components": [c.name for c in (getattr(fields, "components", []) or []) if hasattr(c, "name")],
        "fix_versions": [v.name for v in (getattr(fields, "fixVersions", []) or []) if hasattr(v, "name")],
        "sprint": sprint_value,
        "story_points": story_points,
        "time_estimate": getattr(fields, "timeestimate", None),
        "time_spent": getattr(fields, "timespent", None),
        "created": getattr(fields, "created", None),
        "updated": getattr(fields, "updated", None),
        "resolved": getattr(fields, "resolutiondate", None),
        "resolution": _safe(fields, "resolution", "name"),
        "raw_json": raw_fields,
    }

    # Comments
    rendered_comments_by_id: dict[str, str] = {}
    if rendered and isinstance(rendered.get("comment"), dict):
        for c in rendered["comment"].get("comments", []) or []:
            rendered_comments_by_id[c.get("id")] = c.get("body")

    comments: list[dict] = []
    raw_comments = (raw_fields.get("comment") or {}).get("comments", []) or []
    for c in raw_comments:
        author = c.get("author") or {}
        comments.append({
            "id": c.get("id"),
            "author": author.get("displayName") or author.get("name"),
            "author_email": author.get("emailAddress"),
            "body": _as_text(c.get("body")),
            "body_rendered": rendered_comments_by_id.get(c.get("id")),
            "created": c.get("created"),
            "updated": c.get("updated"),
        })

    # Issue links — Jira splits inward / outward, but each link object lists both sides.
    links: list[dict] = []
    for link in raw_fields.get("issuelinks", []) or []:
        link_type = (link.get("type") or {}).get("name")
        outward = (link.get("outwardIssue") or {}).get("key")
        inward = (link.get("inwardIssue") or {}).get("key")
        if outward:
            links.append({
                "target_key": outward,
                "link_type": link_type,
                "direction": "outward",
                "label": (link.get("type") or {}).get("outward"),
            })
        if inward:
            links.append({
                "target_key": inward,
                "link_type": link_type,
                "direction": "inward",
                "label": (link.get("type") or {}).get("inward"),
            })

    # Attachments (metadata only)
    attachments: list[dict] = []
    for a in raw_fields.get("attachment", []) or []:
        author = a.get("author") or {}
        attachments.append({
            "id": a.get("id"),
            "filename": a.get("filename"),
            "mime_type": a.get("mimeType"),
            "size": a.get("size"),
            "author": author.get("displayName"),
            "created": a.get("created"),
            "url": a.get("content"),
        })

    return NormalizedIssue(ticket=ticket, comments=comments, links=links, attachments=attachments)


# --- Worklog fetch ----------------------------------------------------------

def _fetch_worklogs(jira: JIRA, issue_key: str) -> list[dict]:
    """Fetch all worklogs for one issue and normalize them."""
    try:
        wls = jira.worklogs(issue_key)
    except Exception:
        return []
    out: list[dict] = []
    for w in wls:
        author = getattr(w, "author", None)
        author_name, author_email = _extract_user(author)
        out.append({
            "id": getattr(w, "id", None),
            "author": author_name,
            "author_email": author_email,
            "time_spent_seconds": getattr(w, "timeSpentSeconds", 0) or 0,
            "started": getattr(w, "started", None),
            "comment": _as_text(getattr(w, "comment", None)),
            "created": getattr(w, "created", None),
            "updated": getattr(w, "updated", None),
        })
    return out


# --- Sync orchestration -----------------------------------------------------

@dataclass
class SyncResult:
    jql: str
    tickets: int = 0
    comments: int = 0
    links: int = 0
    worklogs: int = 0
    attachments: int = 0
    errors: list[str] = field(default_factory=list)


def _iter_issue_pages(
    jira: JIRA,
    jql: str,
    *,
    page_size: int,
    fields: str = "*all",
    expand: str = "renderedFields",
) -> Iterable[list[Issue]]:
    """
    Yield successive pages of issues for `jql`.

    Dispatches on `jira._is_cloud`:

    - Cloud  → `enhanced_search_issues` with `nextPageToken` paging
              (the legacy `search` endpoint is deprecated and refuses
              `startAt > 0`).
    - Server → `search_issues` with `startAt` paging (unchanged).

    A page is yielded as a plain `list[Issue]`; iteration stops when the
    backend signals no more pages.
    """
    if getattr(jira, "_is_cloud", False):
        next_token: Optional[str] = None
        while True:
            try:
                page = jira.enhanced_search_issues(
                    jql_str=jql,
                    nextPageToken=next_token,
                    maxResults=page_size,
                    fields=fields,
                    expand=expand,
                )
            except JIRAError:
                raise
            issues = list(page)
            if not issues:
                return
            yield issues
            next_token = getattr(page, "_nextPageToken", None) or getattr(page, "nextPageToken", None)
            if not next_token:
                return
    else:
        start_at = 0
        while True:
            page = jira.search_issues(
                jql,
                startAt=start_at,
                maxResults=page_size,
                fields=fields,
                expand=expand,
            )
            issues = list(page)
            if not issues:
                return
            yield issues
            if len(issues) < page_size:
                return
            start_at += len(issues)


def build_default_jql(
    *,
    project: Optional[str] = None,
    since: Optional[str] = None,
    only_mine: bool = True,
    window_days: int = 180,
) -> str:
    """Construct the default JQL for incremental sync.

    Rules
    -----
    - `project`     → `project = <KEY>` (overrides "only_mine" if exclusive)
    - `since`       → `updated >= "YYYY-MM-DD"` (replaces window_days)
    - `only_mine`   → `(assignee = currentUser() OR reporter = currentUser())`
    - `window_days` → `updated >= -<N>d` (fallback when no `since`)
    """
    parts: list[str] = []
    if only_mine and not project:
        parts.append("(assignee = currentUser() OR reporter = currentUser())")
    if project:
        parts.append(f'project = "{project.upper()}"')
    if since:
        parts.append(f'updated >= "{since}"')
    elif window_days:
        parts.append(f"updated >= -{window_days}d")
    jql = " AND ".join(parts) if parts else "assignee = currentUser()"
    return f"{jql} ORDER BY updated DESC"


def sync(
    jira: JIRA,
    *,
    jql: Optional[str] = None,
    project: Optional[str] = None,
    since: Optional[str] = None,
    page_size: int = 100,
    max_tickets: Optional[int] = None,
    fetch_worklogs: bool = True,
    progress: Optional[Callable[[str], None]] = None,
) -> SyncResult:
    """
    Pull issues from Jira and write them to the local cache.

    Returns a SyncResult with per-table counts. Raises on hard auth/network
    failures; per-ticket errors are collected in `result.errors`.
    """
    jql = jql or build_default_jql(project=project, since=since)
    log = progress or (lambda _msg: None)
    custom_fields = _resolve_custom_fields(jira)
    log(f"JQL: {jql}")

    result = SyncResult(jql=jql)
    seen_keys: set[str] = set()

    with store.open_db() as conn:
        run_id = store.record_sync_start(conn, jql)
        try:
            for issues in _iter_issue_pages(jira, jql, page_size=page_size):
                for issue in issues:
                    if max_tickets is not None and len(seen_keys) >= max_tickets:
                        break
                    try:
                        rendered = (getattr(issue, "raw", {}) or {}).get("renderedFields")
                        norm = normalize_issue(issue, custom_fields=custom_fields, rendered=rendered)
                        store.upsert_ticket(conn, norm.ticket)
                        result.tickets += 1
                        result.comments += store.replace_comments(conn, issue.key, norm.comments)
                        result.links += store.replace_links(conn, issue.key, norm.links)
                        result.attachments += store.replace_attachments(conn, issue.key, norm.attachments)

                        if fetch_worklogs:
                            wls = _fetch_worklogs(jira, issue.key)
                            result.worklogs += store.replace_worklogs(conn, issue.key, wls)

                        store.refresh_fts(conn, issue.key)
                        seen_keys.add(issue.key)
                    except Exception as e:
                        result.errors.append(f"{issue.key}: {e}")

                log(f"  Synced {len(seen_keys)} ticket(s)...")
                if max_tickets is not None and len(seen_keys) >= max_tickets:
                    break

            store.record_sync_finish(
                conn, run_id,
                tickets=result.tickets,
                comments=result.comments,
                links=result.links,
                worklogs=result.worklogs,
                attachments=result.attachments,
                error="; ".join(result.errors)[:2000] if result.errors else None,
            )
        except Exception as e:
            store.record_sync_finish(conn, run_id, error=str(e))
            raise

    return result


def sync_single(jira: JIRA, key: str, *, fetch_worklogs: bool = True) -> SyncResult:
    """Refresh just one ticket. Useful for agent-driven targeted updates."""
    return sync(
        jira,
        jql=f'key = "{key.upper()}"',
        page_size=1,
        max_tickets=1,
        fetch_worklogs=fetch_worklogs,
    )
