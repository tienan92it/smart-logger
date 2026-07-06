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
    space_id = os.getenv("NOTION_SPACE_ID")
    if not space_id:
        raise NotionWorklogError("Missing NOTION_SPACE_ID in .env file.")

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
