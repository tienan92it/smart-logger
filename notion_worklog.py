"""
Notion Worklog Query Module

Fetches time-log entries from a Notion database via the internal queryCollection API.
Reuses session auth from notion_auth (token_v2 cookie).
"""

from __future__ import annotations

import json
import os
import time
import random
import uuid
from datetime import datetime
from typing import Any, Optional

import requests
from rich.console import Console

from notion_auth import (
    clear_token,
    get_notion_credentials,
    get_token_via_playwright,
    load_stored_token,
    save_token,
)
from notion_form import NotionAuthError, get_project_page_id

console = Console()

NOTION_QUERY_URL = "https://app.notion.com/api/v3/queryCollection"
NOTION_SAVE_URL = "https://app.notion.com/api/v3/saveTransactionsFanout"
NOTION_CLIENT_VERSION = "23.13.20260706.0619"
_MAX_FETCH_LIMIT = 500
_MAX_TRANSIENT_ATTEMPTS = 5
_TRANSIENT_HTTP = frozenset({429, 502, 503, 504})


class NotionWorklogError(Exception):
    """Raised when worklog query fails."""


def _prop_ids() -> dict[str, str]:
    return {
        "project": os.getenv("NOTION_PROP_ID_PROJECT", "cjkf"),
        "proof_of_works": os.getenv("NOTION_PROP_ID_PROOF", "SJfi"),
        "time_spent": os.getenv("NOTION_PROP_ID_TIME", "RUz\\"),
        "task_type": os.getenv("NOTION_PROP_ID_TYPE", "ZA~q"),
        "on_date": os.getenv("NOTION_PROP_ID_DATE", "joDR"),
        "status": os.getenv("NOTION_PROP_ID_STATUS", "QeGl"),
        "created_by_filter": os.getenv("NOTION_PROP_ID_CREATED_BY", "nlOG"),
        "created_by": os.getenv("NOTION_PROP_ID_CREATED_BY_VALUE", "nWkE"),
    }


def _space_id() -> str:
    space_id = os.getenv("NOTION_SPACE_ID")
    if not space_id:
        raise NotionWorklogError("Missing NOTION_SPACE_ID in .env file.")
    return space_id


def _allowed_statuses() -> tuple[str, ...]:
    raw = os.getenv("NOTION_WORKLOG_STATUSES")
    if raw:
        try:
            values = json.loads(raw)
            if isinstance(values, list) and values:
                return tuple(str(v) for v in values)
        except json.JSONDecodeError:
            pass
    return ("Draft", "Reviewed")


def normalize_worklog_status(status: str) -> str:
    """Return canonical status label (case-insensitive match)."""
    cleaned = (status or "").strip()
    if not cleaned:
        raise NotionWorklogError("Status must not be empty.")
    for allowed in _allowed_statuses():
        if cleaned.lower() == allowed.lower():
            return allowed
    raise NotionWorklogError(
        f"Invalid status '{status}'. Allowed: {', '.join(_allowed_statuses())}"
    )


def _collection_id() -> str:
    value = os.getenv("NOTION_COLLECTION_ID")
    if not value:
        raise NotionWorklogError(
            "Missing NOTION_COLLECTION_ID in .env.\n"
            "Copy it from the worklog database URL or queryCollection request payload."
        )
    return value


def _collection_view_id() -> str:
    value = os.getenv("NOTION_COLLECTION_VIEW_ID")
    if not value:
        raise NotionWorklogError(
            "Missing NOTION_COLLECTION_VIEW_ID in .env.\n"
            "Copy it from the collection view URL (v=...) or queryCollection payload."
        )
    return value


def _load_user_directory() -> dict[str, str]:
    """Map display names / aliases to Notion user IDs."""
    directory: dict[str, str] = {}
    raw = os.getenv("NOTION_WORKLOG_USERS")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                directory.update({str(k): str(v) for k, v in parsed.items()})
        except json.JSONDecodeError as exc:
            raise NotionWorklogError(f"NOTION_WORKLOG_USERS is not valid JSON: {exc}") from exc

    stored = load_stored_token() or {}
    user_id = stored.get("user_id")
    if user_id:
        directory.setdefault("me", user_id)
        directory.setdefault("self", user_id)
    return directory


def resolve_user_ids(users: Optional[list[str]] = None, quiet: bool = False) -> list[str]:
    """
    Resolve user names/aliases/UUIDs to Notion user IDs.

    When `users` is empty, returns the authenticated user's ID.
    """
    directory = _load_user_directory()
    if not users:
        creds = get_notion_credentials(quiet=quiet)
        uid = creds.get("user_id")
        if not uid:
            raise NotionAuthError("Missing user_id in stored Notion session.")
        return [uid]

    resolved: list[str] = []
    for name in users:
        token = name.strip()
        if not token:
            continue

        if len(token) == 36 and token.count("-") == 4:
            resolved.append(token)
            continue

        needle = token.lower()
        match_id: Optional[str] = None
        for label, uid in directory.items():
            if label.lower() == needle or needle in label.lower():
                match_id = uid
                break

        if not match_id:
            known = ", ".join(sorted(directory)) or "(none — set NOTION_WORKLOG_USERS)"
            raise NotionWorklogError(
                f"Unknown user '{name}'. Known aliases: {known}"
            )
        resolved.append(match_id)

    if not resolved:
        raise NotionWorklogError("No users resolved for query.")
    return resolved


def _notion_response_retryable(response: requests.Response) -> bool:
    if response.status_code in _TRANSIENT_HTTP:
        return True
    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError):
        return False

    def walk(obj: Any) -> bool:
        if isinstance(obj, dict):
            if obj.get("retryable") is True:
                return True
            return any(walk(v) for v in obj.values())
        if isinstance(obj, list):
            return any(walk(x) for x in obj)
        return False

    return walk(data)


def _request_headers(creds: dict, space_id: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://app.notion.com",
        "Referer": f"https://app.notion.com/p/dwarves/{_collection_id().replace('-', '')}",
        "notion-audit-log-platform": "web",
        "notion-client-version": NOTION_CLIENT_VERSION,
        "x-notion-active-user-header": creds.get("user_id", ""),
        "x-notion-space-id": space_id,
    }


def _request_cookies(creds: dict) -> dict[str, str]:
    cookies = {"token_v2": creds["token_v2"]}
    if creds.get("user_id"):
        cookies["notion_user_id"] = creds["user_id"]
    return cookies


def _build_filter(
    user_ids: list[str],
    project_page_id: Optional[str],
    prop: dict[str, str],
) -> dict[str, Any]:
    filters: list[dict[str, Any]] = []

    if project_page_id:
        filters.append(
            {
                "property": prop["project"],
                "filter": {
                    "operator": "relation_contains",
                    "value": {"type": "exact", "value": project_page_id},
                },
            }
        )

    filters.append(
        {
            "property": prop["created_by_filter"],
            "filter": {
                "operator": "person_contains",
                "value": [
                    {
                        "type": "exact",
                        "value": {"table": "notion_user", "id": uid},
                    }
                    for uid in user_ids
                ],
            },
        }
    )

    return {"operator": "and", "filters": filters}


def _query_collection_raw(
    creds: dict,
    *,
    user_ids: list[str],
    project_page_id: Optional[str],
    fetch_limit: int,
    retry_on_auth_fail: bool = True,
) -> dict[str, Any]:
    space_id = _space_id()
    prop = _prop_ids()
    payload = {
        "clientType": "notion_app",
        "source": {
            "type": "collection",
            "id": _collection_id(),
            "spaceId": space_id,
        },
        "collectionView": {
            "id": _collection_view_id(),
            "spaceId": space_id,
        },
        "loader": {
            "reducers": {
                "collection_group_results": {
                    "type": "results",
                    "limit": min(fetch_limit, _MAX_FETCH_LIMIT),
                    "loadContentCover": False,
                }
            },
            "filter": _build_filter(user_ids, project_page_id, prop),
            "sort": [{"property": prop["on_date"], "direction": "descending"}],
            "searchQuery": "",
            "archiveStatus": "NON_ARCHIVED",
            "userId": creds.get("user_id", ""),
            "userTimeZone": os.getenv("NOTION_USER_TIMEZONE", "Asia/Saigon"),
            "propertyAggregations": [
                {
                    "type": "aggregation",
                    "aggregation": {"property": prop["time_spent"], "aggregator": "sum"},
                },
                {
                    "type": "aggregation",
                    "aggregation": {"property": prop["on_date"], "aggregator": "unique"},
                },
            ],
        },
    }

    headers = _request_headers(creds, space_id)
    cookies = _request_cookies(creds)

    for attempt in range(_MAX_TRANSIENT_ATTEMPTS):
        response = requests.post(
            NOTION_QUERY_URL,
            json=payload,
            headers=headers,
            cookies=cookies,
            timeout=60,
        )

        if response.status_code == 401 or "Unauthorized" in response.text:
            if retry_on_auth_fail:
                console.print("[yellow]Token expired. Re-authenticating...[/yellow]")
                clear_token()
                email = os.getenv("NOTION_EMAIL")
                new_creds = get_token_via_playwright(email)
                save_token(new_creds)
                return _query_collection_raw(
                    new_creds,
                    user_ids=user_ids,
                    project_page_id=project_page_id,
                    fetch_limit=fetch_limit,
                    retry_on_auth_fail=False,
                )
            raise NotionAuthError(
                "Authentication failed. Run 'smart-log notion-login' to re-authenticate."
            )

        if response.ok:
            return response.json()

        if _notion_response_retryable(response) and attempt < _MAX_TRANSIENT_ATTEMPTS - 1:
            delay = min(30.0, (2 ** (attempt + 1)) + random.uniform(0, 1.0))
            console.print(
                f"[yellow]Notion temporarily unavailable ({response.status_code}), "
                f"retrying in {delay:.1f}s...[/yellow]"
            )
            time.sleep(delay)
            continue

        raise NotionWorklogError(
            f"queryCollection failed ({response.status_code}): {response.text[:500]}"
        )

    raise NotionWorklogError("queryCollection failed after retries.")


def _unwrap_block(block_wrap: dict[str, Any]) -> dict[str, Any]:
    value = block_wrap.get("value", block_wrap)
    if isinstance(value, dict) and "value" in value and isinstance(value["value"], dict):
        return value["value"]
    return value if isinstance(value, dict) else {}


def _unwrap_user(user_wrap: dict[str, Any]) -> dict[str, Any]:
    value = user_wrap.get("value", user_wrap)
    if isinstance(value, dict) and "value" in value and isinstance(value["value"], dict):
        return value["value"]
    return value if isinstance(value, dict) else {}


def _plain_text(prop_value: Any) -> str:
    if not prop_value or not isinstance(prop_value, list):
        return ""
    parts: list[str] = []
    for segment in prop_value:
        if not segment:
            continue
        if isinstance(segment, list) and segment:
            text = segment[0]
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts).strip()


def _number_value(prop_value: Any) -> Optional[float]:
    text = _plain_text(prop_value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _date_value(prop_value: Any) -> Optional[str]:
    if not prop_value or not isinstance(prop_value, list):
        return None
    for segment in prop_value:
        if not isinstance(segment, list) or len(segment) < 2:
            continue
        payload = segment[1]
        if not isinstance(payload, list):
            continue
        for item in payload:
            if (
                isinstance(item, list)
                and len(item) >= 2
                and item[0] == "d"
                and isinstance(item[1], dict)
            ):
                return item[1].get("start_date") or item[1].get("start")
    return None


def _relation_page_ids(prop_value: Any) -> list[str]:
    ids: list[str] = []
    if not prop_value or not isinstance(prop_value, list):
        return ids
    for segment in prop_value:
        if not isinstance(segment, list) or len(segment) < 2:
            continue
        payload = segment[1]
        if not isinstance(payload, list):
            continue
        for item in payload:
            if isinstance(item, list) and len(item) >= 2 and item[0] == "p":
                ids.append(str(item[1]))
    return ids


def _person_ids(prop_value: Any) -> list[str]:
    ids: list[str] = []
    if not prop_value or not isinstance(prop_value, list):
        return ids
    for segment in prop_value:
        if not isinstance(segment, list) or len(segment) < 2:
            continue
        payload = segment[1]
        if not isinstance(payload, list):
            continue
        for item in payload:
            if isinstance(item, list) and len(item) >= 2 and item[0] == "u":
                ids.append(str(item[1]))
    return ids


def _page_title(record_map: dict[str, Any], page_id: str) -> str:
    block = record_map.get("block", {}).get(page_id)
    if not block:
        return page_id
    page = _unwrap_block(block)
    return _plain_text(page.get("properties", {}).get("title")) or page_id


def _user_name(record_map: dict[str, Any], user_id: str) -> str:
    user = record_map.get("notion_user", {}).get(user_id)
    if not user:
        return user_id
    profile = _unwrap_user(user)
    return profile.get("name") or profile.get("email") or user_id


def _parse_worklog_row(record_map: dict[str, Any], block_id: str, prop: dict[str, str]) -> dict[str, Any]:
    block_wrap = record_map.get("block", {}).get(block_id)
    if not block_wrap:
        return {"id": block_id}

    page = _unwrap_block(block_wrap)
    properties = page.get("properties") or {}

    project_ids = _relation_page_ids(properties.get(prop["project"]))
    project_name = _page_title(record_map, project_ids[0]) if project_ids else ""

    person_ids = _person_ids(properties.get(prop["created_by"]))
    if page.get("created_by_id"):
        created_by = _user_name(record_map, page["created_by_id"])
    elif person_ids:
        created_by = ", ".join(_user_name(record_map, uid) for uid in person_ids)
    else:
        created_by = "-"

    return {
        "id": block_id,
        "created_by": created_by,
        "project": project_name,
        "date": _date_value(properties.get(prop["on_date"])) or "",
        "status": _plain_text(properties.get(prop["status"])) or "-",
        "effort_hours": _number_value(properties.get(prop["time_spent"])),
        "focus_areas": _plain_text(properties.get(prop["task_type"])) or "-",
        "key_deliverables": _plain_text(properties.get(prop["proof_of_works"])) or "-",
    }


def _in_date_range(date_str: str, since: Optional[str], until: Optional[str]) -> bool:
    if not date_str:
        return True
    if since and date_str < since:
        return False
    if until and date_str > until:
        return False
    return True


def query_worklogs(
    users: Optional[list[str]] = None,
    project: Optional[str] = None,
    limit: int = 50,
    since: Optional[str] = None,
    until: Optional[str] = None,
    quiet: bool = False,
) -> dict[str, Any]:
    """
    Query Notion worklog rows for the given users.

    Returns:
        {
            "entries": [...],
            "total_effort_hours": float,
            "aggregations": {...},
            "truncated": bool,
        }
    """
    user_ids = resolve_user_ids(users, quiet=quiet)
    project_page_id = get_project_page_id(project) if project else None

    creds = get_notion_credentials(quiet=quiet)
    fetch_limit = max(limit, 50)
    if fetch_limit > _MAX_FETCH_LIMIT:
        fetch_limit = _MAX_FETCH_LIMIT

    data = _query_collection_raw(
        creds,
        user_ids=user_ids,
        project_page_id=project_page_id,
        fetch_limit=fetch_limit,
    )

    reducer = data.get("result", {}).get("reducerResults", {}).get("collection_group_results", {})
    block_ids = reducer.get("blockIds") or []
    has_more = bool(reducer.get("hasMore"))
    record_map = data.get("recordMap") or {}
    prop = _prop_ids()

    entries = [
        row
        for block_id in block_ids
        if (row := _parse_worklog_row(record_map, block_id, prop))
        and _in_date_range(row.get("date") or "", since, until)
    ]

    if limit > 0:
        entries = entries[:limit]

    total_effort = sum(e.get("effort_hours") or 0 for e in entries)

    aggregations = {}
    for key, value in data.get("result", {}).get("reducerResults", {}).items():
        if key.startswith("table:") and isinstance(value, dict):
            agg = value.get("aggregationResult")
            if isinstance(agg, dict):
                aggregations[key] = agg.get("value")

    return {
        "entries": entries,
        "total_effort_hours": total_effort,
        "aggregations": aggregations,
        "truncated": has_more or len(block_ids) > len(entries),
        "fetched": len(block_ids),
    }


def period_to_date_range(period: Optional[str] = None) -> tuple[Optional[str], Optional[str], str]:
    """Convert period shorthand to inclusive since/until dates (YYYY-MM-DD)."""
    from datetime import datetime, timedelta

    period = (period or "").strip().lower() or "last_week"
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    period_label = period.replace("_", " ").title()

    if period == "today":
        start, end = today, today
    elif period == "yesterday":
        start = end = today - timedelta(days=1)
    elif period == "this_week":
        start = today - timedelta(days=today.weekday())
        end = today
    elif period == "last_week":
        start = today - timedelta(days=today.weekday() + 7)
        end = start + timedelta(days=6)
    elif period == "this_month":
        start, end = today.replace(day=1), today
    elif period == "last_month":
        first = today.replace(day=1)
        end = first - timedelta(days=1)
        start = end.replace(day=1)
    else:
        start = today - timedelta(days=7)
        end = today
        period_label = "Last 7 Days"

    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), period_label


def build_query_kwargs(plan: dict[str, Any]) -> dict[str, Any]:
    """Map orchestrator/MCP plan fields to `query_worklogs` kwargs."""
    since = plan.get("since")
    until = plan.get("until")
    period = plan.get("period")
    if not since and not until and period:
        since, until, _ = period_to_date_range(period)

    users = plan.get("users")
    if isinstance(users, str):
        users = [users]

    limit = plan.get("limit")
    return {
        "users": users or None,
        "project": plan.get("project") or None,
        "limit": int(limit) if limit else 50,
        "since": since,
        "until": until,
    }


def detect_period_in_text(text: str) -> Optional[str]:
    """Best-effort period shorthand from natural language."""
    normalized = text.lower().replace("_", " ").replace("-", " ")
    compact = normalized.replace(" ", "")
    if "today" in normalized:
        return "today"
    if "yesterday" in normalized:
        return "yesterday"
    if "this week" in normalized or "thisweek" in compact:
        return "this_week"
    if "last week" in normalized or "lastweek" in compact:
        return "last_week"
    if "this month" in normalized or "thismonth" in compact:
        return "this_month"
    if "last month" in normalized or "lastmonth" in compact:
        return "last_month"
    return None


def match_users_in_text(text: str) -> list[str]:
    """Match NOTION_WORKLOG_USERS aliases mentioned in free text."""
    needle = text.lower()
    matched: list[str] = []
    for alias in _load_user_directory():
        if alias.lower() in {"me", "self"}:
            continue
        if alias.lower() in needle:
            matched.append(alias)
    return matched


def build_status_update_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Normalize orchestrator/CLI plan for status updates."""
    query = build_query_kwargs(plan)
    status = plan.get("status")
    from_status = plan.get("from_status")
    if from_status:
        from_status = normalize_worklog_status(from_status)
    return {
        "entry_id": (plan.get("entry_id") or "").strip() or None,
        "status": normalize_worklog_status(status) if status else None,
        "users": query.get("users"),
        "project": query.get("project"),
        "since": query.get("since"),
        "until": query.get("until"),
        "from_status": from_status,
        "limit": int(plan.get("limit") or 500),
        "preview": bool(plan.get("preview", False)),
    }


def format_status_update_markdown(result: dict[str, Any]) -> str:
    """Summarize a bulk/single status update for MCP output."""
    if result.get("preview"):
        lines = [f"**Preview — {result.get('matched', 0)} matching entries** (no changes made)"]
    else:
        lines = [
            f"**Updated {len(result.get('updated') or [])} entries → {result.get('status')}**"
        ]
    skipped = result.get("skipped") or []
    if skipped:
        lines.append(f"_Skipped {len(skipped)} (already {result.get('status')} or filtered out)._")
    if result.get("truncated"):
        lines.append("_Warning: query was truncated — narrow filters or run again._")

    updated = result.get("updated") or []
    preview_rows = result.get("preview_rows") or updated
    rows = preview_rows[:20]
    if rows:
        lines.extend(["", "| Date | Project | Created by | Status | Deliverables |", "|---|---|---|---|---|"])
        for row in rows:
            deliverables = (row.get("key_deliverables") or "-").replace("|", "\\|")
            if len(deliverables) > 60:
                deliverables = deliverables[:60] + "..."
            lines.append(
                f"| {row.get('date') or '-'} | {row.get('project') or '-'} | "
                f"{row.get('created_by') or '-'} | {row.get('status') or '-'} | {deliverables} |"
            )
        if len(preview_rows) > len(rows):
            lines.append(f"\n_…and {len(preview_rows) - len(rows)} more._")
    elif not skipped:
        lines.append("\n_No matching entries._")
    return "\n".join(lines)


def format_worklogs_markdown(result: dict[str, Any], max_rows: int = 50) -> str:
    """Format query result as a Markdown table for MCP / agent output."""
    entries = result.get("entries") or []
    if not entries:
        return "No Notion worklog entries found for the given filters."

    total = result.get("total_effort_hours") or 0
    lines = [
        f"**Notion worklogs** — {len(entries)} entries, {total:g}h total",
        "",
        "| Created by | Project | Date | Status | Effort | Focus | Key deliverables |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in entries[:max_rows]:
        effort = row.get("effort_hours")
        effort_str = f"{effort:g}h" if effort is not None else "-"
        deliverables = (row.get("key_deliverables") or "-").replace("|", "\\|")
        if len(deliverables) > 80:
            deliverables = deliverables[:80] + "..."
        lines.append(
            f"| {row.get('created_by') or '-'} | {row.get('project') or '-'} | "
            f"{row.get('date') or '-'} | {row.get('status') or '-'} | {effort_str} | "
            f"{row.get('focus_areas') or '-'} | {deliverables} |"
        )
    if len(entries) > max_rows:
        lines.append(f"\n_Showing {max_rows} of {len(entries)} entries._")
    if result.get("truncated"):
        lines.append("_Results may be truncated — narrow filters or raise limit._")
    return "\n".join(lines)


def _build_status_update_transaction(
    entry_id: str,
    status: str,
    *,
    space_id: str,
    user_id: str,
    status_prop: str,
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    pointer = {"id": entry_id, "table": "block", "spaceId": space_id}
    return {
        "id": str(uuid.uuid4()),
        "spaceId": space_id,
        "debug": {"userAction": "smart-logger.updateWorklogStatus"},
        "operations": [
            {
                "command": "updateBlockPropertyValue",
                "pointer": pointer,
                "path": ["properties", status_prop],
                "args": {"primitiveOp": {"command": "set", "args": [[status]]}},
                "additionalUpdatedPointers": [pointer],
            },
            {
                "command": "update",
                "pointer": pointer,
                "path": [],
                "args": {
                    "last_edited_time": now_ms,
                    "last_edited_by_id": user_id,
                    "last_edited_by_table": "notion_user",
                },
            },
        ],
    }


def _post_save_transactions(
    transactions: list[dict[str, Any]],
    creds: dict,
    *,
    quiet: bool = False,
    retry_on_auth_fail: bool = True,
    retry_callback: Optional[Any] = None,
) -> dict[str, Any]:
    if not transactions:
        raise NotionWorklogError("No transactions to save.")

    space_id = _space_id()
    payload = {"requestId": str(uuid.uuid4()), "transactions": transactions}
    headers = _request_headers(creds, space_id)
    cookies = _request_cookies(creds)

    for attempt in range(_MAX_TRANSIENT_ATTEMPTS):
        response = requests.post(
            NOTION_SAVE_URL,
            json=payload,
            headers=headers,
            cookies=cookies,
            timeout=60,
        )

        if response.status_code == 401 or "Unauthorized" in response.text:
            if retry_on_auth_fail and retry_callback:
                if not quiet:
                    console.print("[yellow]Token expired. Re-authenticating...[/yellow]")
                clear_token()
                email = os.getenv("NOTION_EMAIL")
                new_creds = get_token_via_playwright(email)
                save_token(new_creds)
                return retry_callback(new_creds)
            raise NotionAuthError(
                "Authentication failed. Run 'smart-log notion-login' to re-authenticate."
            )

        if response.ok:
            try:
                return response.json()
            except (json.JSONDecodeError, ValueError):
                return {}

        if _notion_response_retryable(response) and attempt < _MAX_TRANSIENT_ATTEMPTS - 1:
            delay = min(30.0, (2 ** (attempt + 1)) + random.uniform(0, 1.0))
            if not quiet:
                console.print(
                    f"[yellow]Notion temporarily unavailable ({response.status_code}), "
                    f"retrying in {delay:.1f}s...[/yellow]"
                )
            time.sleep(delay)
            continue

        raise NotionWorklogError(
            f"saveTransactionsFanout failed ({response.status_code}): {response.text[:500]}"
        )

    raise NotionWorklogError("saveTransactionsFanout failed after retries.")


def _entry_matches_status_filter(entry: dict[str, Any], target: str, from_status: Optional[str]) -> tuple[bool, str]:
    current = (entry.get("status") or "-").strip()
    if current.lower() == target.lower():
        return False, f"already {target}"
    if from_status and current.lower() != from_status.lower():
        return False, f"status is {current}, not {from_status}"
    return True, ""


def update_worklogs_status(
    status: str,
    *,
    entry_id: Optional[str] = None,
    users: Optional[list[str]] = None,
    project: Optional[str] = None,
    period: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    from_status: Optional[str] = None,
    limit: int = 500,
    preview: bool = False,
    quiet: bool = False,
    retry_on_auth_fail: bool = True,
) -> dict[str, Any]:
    """
    Update one or many Notion worklog rows to `status`.

    Identify rows by:
    - `entry_id` alone, or
    - filters: users / project / period / since / until (at least one date scope required)
    """
    canonical_status = normalize_worklog_status(status)
    canonical_from = normalize_worklog_status(from_status) if from_status else None

    plan = {
        "status": canonical_status,
        "entry_id": entry_id,
        "users": users,
        "project": project,
        "period": period,
        "since": since,
        "until": until,
        "from_status": canonical_from,
        "limit": limit,
        "preview": preview,
    }
    normalized = build_status_update_plan(plan)

    if normalized["entry_id"]:
        if preview:
            return {
                "status": canonical_status,
                "preview": True,
                "matched": 1,
                "updated": [],
                "skipped": [],
                "preview_rows": [{"id": normalized["entry_id"], "status": "?"}],
                "truncated": False,
            }
        single = update_worklog_status(
            normalized["entry_id"],
            canonical_status,
            quiet=quiet,
            retry_on_auth_fail=retry_on_auth_fail,
        )
        return {
            "status": canonical_status,
            "preview": False,
            "matched": 1,
            "updated": [single],
            "skipped": [],
            "truncated": False,
        }

    if not normalized["since"] and not normalized["until"]:
        raise NotionWorklogError(
            "Bulk status updates need a date scope: use --period, --since/--until, "
            "or pass a single entry id."
        )

    query_result = query_worklogs(
        users=normalized["users"],
        project=normalized["project"],
        since=normalized["since"],
        until=normalized["until"],
        limit=normalized["limit"],
        quiet=True,
    )
    entries = query_result.get("entries") or []

    to_update: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for entry in entries:
        ok, reason = _entry_matches_status_filter(entry, canonical_status, canonical_from)
        if ok:
            to_update.append(entry)
        else:
            skipped.append({**entry, "skip_reason": reason})

    preview_rows = to_update if preview else []
    if preview:
        return {
            "status": canonical_status,
            "preview": True,
            "matched": len(to_update),
            "updated": [],
            "skipped": skipped,
            "preview_rows": preview_rows,
            "truncated": bool(query_result.get("truncated")),
            "filters": {
                "users": normalized["users"],
                "project": normalized["project"],
                "since": normalized["since"],
                "until": normalized["until"],
                "from_status": canonical_from,
            },
        }

    if not to_update:
        return {
            "status": canonical_status,
            "preview": False,
            "matched": 0,
            "updated": [],
            "skipped": skipped,
            "truncated": bool(query_result.get("truncated")),
        }

    space_id = _space_id()
    prop = _prop_ids()
    creds = get_notion_credentials(quiet=quiet)
    user_id = creds.get("user_id")
    if not user_id:
        raise NotionAuthError("Missing user_id in stored Notion session.")

    transactions = [
        _build_status_update_transaction(
            entry["id"],
            canonical_status,
            space_id=space_id,
            user_id=user_id,
            status_prop=prop["status"],
        )
        for entry in to_update
    ]

    def _retry(new_creds: dict) -> dict[str, Any]:
        return update_worklogs_status(
            canonical_status,
            entry_id=entry_id,
            users=users,
            project=project,
            period=period,
            since=since,
            until=until,
            from_status=from_status,
            limit=limit,
            preview=False,
            quiet=quiet,
            retry_on_auth_fail=False,
        )

    _post_save_transactions(
        transactions,
        creds,
        quiet=quiet,
        retry_on_auth_fail=retry_on_auth_fail,
        retry_callback=_retry,
    )

    updated = [
        {
            "entry_id": entry["id"],
            "status": canonical_status,
            "previous_status": entry.get("status"),
            "date": entry.get("date"),
            "project": entry.get("project"),
            "created_by": entry.get("created_by"),
            "key_deliverables": entry.get("key_deliverables"),
        }
        for entry in to_update
    ]
    return {
        "status": canonical_status,
        "preview": False,
        "matched": len(to_update),
        "updated": updated,
        "skipped": skipped,
        "truncated": bool(query_result.get("truncated")),
        "filters": {
            "users": normalized["users"],
            "project": normalized["project"],
            "since": normalized["since"],
            "until": normalized["until"],
            "from_status": canonical_from,
        },
    }


def update_worklog_status(
    entry_id: str,
    status: str,
    *,
    quiet: bool = False,
    retry_on_auth_fail: bool = True,
) -> dict[str, Any]:
    """
    Update a Notion worklog row status via saveTransactionsFanout.

    Args:
        entry_id: Notion block/page id (from `notion-worklogs --json`).
        status: Target status label (e.g. Draft, Reviewed).

    Returns:
        API response dict (typically empty on success).
    """
    entry_id = entry_id.strip()
    if not entry_id:
        raise NotionWorklogError("entry_id is required.")

    canonical_status = normalize_worklog_status(status)
    space_id = _space_id()
    prop = _prop_ids()
    creds = get_notion_credentials(quiet=quiet)
    user_id = creds.get("user_id")
    if not user_id:
        raise NotionAuthError("Missing user_id in stored Notion session.")

    payload = {
        "requestId": str(uuid.uuid4()),
        "transactions": [
            _build_status_update_transaction(
                entry_id,
                canonical_status,
                space_id=space_id,
                user_id=user_id,
                status_prop=prop["status"],
            )
        ],
    }

    def _retry(_new_creds: dict) -> dict[str, Any]:
        return update_worklog_status(
            entry_id,
            canonical_status,
            quiet=quiet,
            retry_on_auth_fail=False,
        )

    _post_save_transactions(
        payload["transactions"],
        creds,
        quiet=quiet,
        retry_on_auth_fail=retry_on_auth_fail,
        retry_callback=_retry,
    )
    return {
        "entry_id": entry_id,
        "status": canonical_status,
        "response": {},
    }
