"""
Notion Form Submission Module

Submits task logs via Notion's internal form API.
"""

import os
import json
import time
import random
import requests
from datetime import datetime
from typing import Optional
from rich.console import Console

from notion_auth import get_notion_credentials, clear_token, save_token, get_token_via_playwright

console = Console()


class NotionFormError(Exception):
    """Raised when form submission fails."""
    pass


class NotionAuthError(Exception):
    """Raised when authentication fails."""
    pass


def get_project_page_id(project_name: str) -> Optional[str]:
    """
    Get the Notion project page ID by project name.
    
    Supports two env formats:
    1. JSON mapping: NOTION_PROJECTS={"DF": "page-id-1", "GBI": "page-id-2"}
    2. Individual vars: NOTION_PROJECT_DF=page-id-1, NOTION_PROJECT_GBI=page-id-2
    
    Args:
        project_name: Project name (e.g., "DF", "GBI")
    
    Returns:
        Project page ID or None if not found
    """
    if not project_name:
        return os.getenv("NOTION_PROJECT_DEFAULT")
    
    project_upper = project_name.upper()
    
    # Try JSON mapping first
    projects_json = os.getenv("NOTION_PROJECTS")
    if projects_json:
        try:
            projects = json.loads(projects_json)
            # Try exact match first
            if project_name in projects:
                return projects[project_name]
            # Try case-insensitive match
            for name, page_id in projects.items():
                if name.upper() == project_upper:
                    return page_id
        except json.JSONDecodeError:
            console.print("[yellow]Warning: NOTION_PROJECTS is not valid JSON[/yellow]")
    
    # Try individual env var (NOTION_PROJECT_DF, etc.)
    env_key = f"NOTION_PROJECT_{project_upper}"
    page_id = os.getenv(env_key)
    if page_id:
        return page_id
    
    # Fallback to default
    return os.getenv("NOTION_PROJECT_DEFAULT")


_TRANSIENT_HTTP = frozenset({429, 502, 503, 504})
_MAX_TRANSIENT_ATTEMPTS = 5


def _notion_response_retryable(response: requests.Response) -> bool:
    """True when Notion asks us to retry (transient infra / rate limits)."""
    if response.status_code in _TRANSIENT_HTTP:
        return True
    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError):
        return False

    def walk(obj) -> bool:
        if isinstance(obj, dict):
            if obj.get("retryable") is True:
                return True
            return any(walk(v) for v in obj.values())
        if isinstance(obj, list):
            return any(walk(x) for x in obj)
        return False

    return walk(data)


def submit_notion_form(
    issue_key: str,
    description: str,
    time_hours: float,
    task_type: str = "Development",
    project: str = "",
    date: Optional[str] = None,
    retry_on_auth_fail: bool = True,
) -> dict:
    """
    Submit a task log to Notion via the internal form API.
    
    Args:
        issue_key: Jira issue key (e.g., "GBI-645")
        description: Proof of works text (e.g., "GBI-645: Fix login bug")
        time_hours: Time spent in hours (e.g., 0.5, 1, 2)
        task_type: Type of task ("Development", "Meeting", etc.)
        project: Project name for Notion (e.g., "DF", "GBI")
        date: Date in YYYY-MM-DD format (defaults to today)
        retry_on_auth_fail: If True, retry with fresh login on auth failure
    
    Returns:
        API response dict
    
    Raises:
        NotionFormError: If submission fails
        NotionAuthError: If authentication fails
    """
    # Get credentials
    creds = get_notion_credentials()
    
    # Get form config from environment
    form_id = os.getenv("NOTION_FORM_ID")
    space_id = os.getenv("NOTION_SPACE_ID")
    
    if not form_id or not space_id:
        raise NotionFormError(
            "Missing NOTION_FORM_ID or NOTION_SPACE_ID in .env file.\n"
            "Get these from the form submission URL in browser DevTools."
        )
    
    # Prepare the submission
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    
    # Property IDs from your form (these are specific to your Notion form)
    # From the curl: cjkf=Project, RUz\=Time, SJfi=Proof, ZA~q=TaskType, joDR=Date
    prop_ids = {
        "project": os.getenv("NOTION_PROP_ID_PROJECT", "cjkf"),
        "proof_of_works": os.getenv("NOTION_PROP_ID_PROOF", "SJfi"),
        "time_spent": os.getenv("NOTION_PROP_ID_TIME", "RUz\\"),  # Note: backslash not quote
        "task_type": os.getenv("NOTION_PROP_ID_TYPE", "ZA~q"),
        "on_date": os.getenv("NOTION_PROP_ID_DATE", "joDR"),
    }
    
    # Get project page ID by project name
    project_page_id = get_project_page_id(project)
    if not project_page_id:
        raise NotionFormError(
            f"No project mapping found for '{project}'. Set one of:\n"
            f"  NOTION_PROJECTS='{{\"DF\": \"page-id\", \"{project}\": \"page-id\"}}'\n"
            f"  NOTION_PROJECT_{project.upper()}=page-id\n"
            "  NOTION_PROJECT_DEFAULT=page-id"
        )
    
    # Build the payload
    # description already contains "KEY: title" format from caller
    payload = {
        "formId": form_id,
        "spaceId": space_id,
        "blockProperties": {
            prop_ids["project"]: [["‣", [["p", project_page_id, space_id]]]],
            prop_ids["time_spent"]: [[str(time_hours)]],
            prop_ids["proof_of_works"]: [[description]],
            prop_ids["task_type"]: [[task_type]],
            prop_ids["on_date"]: [["‣", [["d", {"type": "date", "start_date": date}]]]],
            "title": [["New submission"]],
        },
        "filePropertyIdToTokens": {}
    }
    
    # Make the request
    url = "https://www.notion.so/api/v3/submitForm"
    
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://www.notion.so",
        "Referer": f"https://www.notion.so/dwarves/{form_id.replace('-', '')}",
        "notion-audit-log-platform": "web",
        "notion-client-version": "23.13.20260122.1150",
        "x-notion-active-user-header": creds.get("user_id", ""),
    }
    
    cookies = {
        "token_v2": creds["token_v2"],
    }
    if creds.get("user_id"):
        cookies["notion_user_id"] = creds["user_id"]
    
    try:
        for attempt in range(_MAX_TRANSIENT_ATTEMPTS):
            response = requests.post(
                url,
                json=payload,
                headers=headers,
                cookies=cookies,
                timeout=30,
            )

            # Check for auth errors
            if response.status_code == 401 or "Unauthorized" in response.text:
                if retry_on_auth_fail:
                    console.print("[yellow]Token expired. Re-authenticating...[/yellow]")
                    clear_token()
                    # Get fresh token
                    email = os.getenv("NOTION_EMAIL")
                    new_creds = get_token_via_playwright(email)
                    save_token(new_creds)
                    # Retry once
                    return submit_notion_form(
                        issue_key=issue_key,
                        description=description,
                        time_hours=time_hours,
                        task_type=task_type,
                        project=project,
                        date=date,
                        retry_on_auth_fail=False,  # Don't retry again
                    )
                else:
                    raise NotionAuthError("Authentication failed. Please run 'notion-login' to re-authenticate.")

            if response.ok:
                return response.json()

            if _notion_response_retryable(response) and attempt < _MAX_TRANSIENT_ATTEMPTS - 1:
                delay = min(30.0, (2 ** (attempt + 1)) + random.uniform(0, 1.0))
                console.print(
                    f"[yellow]Notion temporarily unavailable ({response.status_code}), "
                    f"retrying in {delay:.1f}s (attempt {attempt + 1}/{_MAX_TRANSIENT_ATTEMPTS})...[/yellow]"
                )
                time.sleep(delay)
                continue

            # Debug: print response for errors (final failure or non-retryable)
            if response.status_code >= 400:
                console.print(f"[red]Response status: {response.status_code}[/red]")
                console.print(f"[red]Response body: {response.text[:500]}[/red]")
                console.print("\n[yellow]Debug - Payload sent:[/yellow]")
                console.print(f"  Form ID: {form_id}")
                console.print(f"  Space ID: {space_id}")
                console.print(f"  Properties: {list(payload['blockProperties'].keys())}")

            response.raise_for_status()
            return response.json()

    except requests.exceptions.RequestException as e:
        raise NotionFormError(f"Failed to submit form: {e}")
