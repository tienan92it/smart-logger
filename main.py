import os
import sys
import json
import typer
from rich.console import Console
from rich.table import Table
from typing import List, Optional
from dotenv import load_dotenv
from jira import JIRA
from ai_client import get_ai_client

from notion_form import submit_notion_form, NotionFormError, NotionAuthError
from notion_auth import get_notion_credentials, clear_token, load_stored_token
from notion_worklog import (
    query_worklogs,
    NotionWorklogError,
    build_query_kwargs,
    period_to_date_range,
)
from memory_bank import load_memory, save_memory, add_issue
from ai_orchestrator import orchestrate, Intent, OrchestratorResult

# Load Config
load_dotenv()
console = Console()
app = typer.Typer()


def _env_flag(name: str) -> bool:
    """Parse common truthy env values for local operational switches."""
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _dry_run_enabled() -> bool:
    return _env_flag("SMART_LOG_DRY_RUN")

# CLI subcommand names (hyphenated). Used so Typer does not treat them as bare NLP.
_SUBCOMMAND_NAMES = frozenset(
    {
        "log", "tasks",
        "notion-login", "notion-status", "notion-logout", "notion-worklogs",
        "jira-sync", "jira-show", "jira-search", "jira-stats", "jira-report",
        "_smart_",
    }
)


def _prepend_smart_if_needed() -> None:
    """
    Route `smart-log <natural language>` to hidden command `_smart_` so it does not
    steal tokens meant for real subcommands (e.g. `notion-login`).
    """
    argv = sys.argv[1:]
    if not argv:
        return
    i = 0
    while i < len(argv):
        if argv[i] in ("-p", "--project") and i + 1 < len(argv):
            i += 2
            continue
        break
    if i >= len(argv):
        return
    next_tok = argv[i]
    if next_tok.startswith("-"):
        return
    if next_tok in _SUBCOMMAND_NAMES:
        return
    sys.argv = [sys.argv[0], "_smart_"] + sys.argv[1:]


# --- DISPLAY HELPERS ---

def _format_time_spent(seconds: int) -> str:
    """Format seconds into human-readable time (e.g., '2h 30m')."""
    if not seconds or seconds == 0:
        return "-"
    
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    
    if hours > 0 and minutes > 0:
        return f"{hours}h {minutes}m"
    elif hours > 0:
        return f"{hours}h"
    elif minutes > 0:
        return f"{minutes}m"
    return "-"


_STATUS_COLORS = {
    "In Progress": "blue",
    "To Do": "white",
    "Done": "green",
    "Testing": "yellow",
    "Done UAT": "green",
    "Blocked": "red",
}

_PRIORITY_COLORS = {
    "Highest": "red",
    "High": "yellow",
    "Medium": "white",
    "Low": "dim",
    "Lowest": "dim",
}


def _display_tasks_table(rows: list, show_desc: bool = False, show_time: bool = False) -> None:
    """Standard table view over cache rows (dicts from `jira_store`)."""
    table = Table(title="My Jira Tasks", show_lines=True)
    table.add_column("Key", style="cyan", no_wrap=True)
    table.add_column("Summary", style="white")
    table.add_column("Status", style="magenta")
    table.add_column("Priority", style="yellow")
    if show_time:
        table.add_column("Time", style="green", justify="right")

    total_seconds = 0
    for row in rows:
        priority = row.get("priority") or "-"
        summary = row.get("summary") or ""
        if len(summary) > 55 and not show_desc:
            summary = summary[:55] + "..."

        cells = [
            row.get("key") or "?",
            summary,
            row.get("status") or "-",
            priority,
        ]
        if show_time:
            seconds = row.get("time_spent") or 0
            total_seconds += seconds
            cells.append(_format_time_spent(seconds))
        table.add_row(*cells)

    console.print(table)
    footer = f"[dim]Showing {len(rows)} tasks[/dim]"
    if show_time and total_seconds > 0:
        total_hours = total_seconds / 3600
        footer += f" | [bold green]Total time: {_format_time_spent(total_seconds)} ({total_hours:.1f}h)[/bold green]"
    console.print(footer)


def _display_grouped_tasks(rows: list, group_by: str, show_desc: bool = False, show_time: bool = False) -> None:
    """Grouped table view over cache rows. Group keys: project | status | priority | type."""
    from collections import defaultdict

    groups: dict = defaultdict(list)
    for row in rows:
        if group_by == "project":
            key = row.get("project") or (row.get("key") or "OTHER").split("-")[0]
        elif group_by == "status":
            key = row.get("status") or "None"
        elif group_by == "priority":
            key = row.get("priority") or "None"
        elif group_by == "type":
            key = row.get("issue_type") or "Unknown"
        else:
            key = "All"
        groups[key].append(row)

    sorted_groups = sorted(groups.items(), key=lambda x: len(x[1]), reverse=True)

    group_times: dict = {}
    total_seconds = 0
    if show_time:
        for group_name, group_rows in sorted_groups:
            group_seconds = sum((r.get("time_spent") or 0) for r in group_rows)
            group_times[group_name] = group_seconds
            total_seconds += group_seconds

    total = len(rows)
    if show_time:
        summary_parts = [f"[bold]{k}[/bold]: {len(v)} ({_format_time_spent(group_times.get(k, 0))})" for k, v in sorted_groups]
    else:
        summary_parts = [f"[bold]{k}[/bold]: {len(v)}" for k, v in sorted_groups]

    console.print(f"\n[bold]Tasks by {group_by.title()}[/bold] ({total} total)")
    if show_time and total_seconds > 0:
        total_hours = total_seconds / 3600
        console.print(f"  [bold green]Total time logged: {_format_time_spent(total_seconds)} ({total_hours:.1f}h)[/bold green]")
    console.print("  " + " | ".join(summary_parts))
    console.print()

    for group_name, group_rows in sorted_groups:
        group_time_str = ""
        if show_time:
            group_time_str = f" - {_format_time_spent(group_times.get(group_name, 0))}"

        table = Table(
            title=f"{group_name} ({len(group_rows)}){group_time_str}",
            show_lines=False,
            title_style="bold cyan",
            border_style="dim",
        )
        table.add_column("Key", style="cyan", no_wrap=True, width=12)
        table.add_column("Summary", style="white", ratio=3)
        table.add_column("Status", style="magenta", width=14)
        table.add_column("Priority", width=10)
        if show_time:
            table.add_column("Time", style="green", justify="right", width=10)

        for row in group_rows:
            priority_name = row.get("priority") or "-"
            priority_color = _PRIORITY_COLORS.get(priority_name, "white")
            status_name = row.get("status") or "-"
            status_color = _STATUS_COLORS.get(status_name, "white")

            summary = row.get("summary") or ""
            if len(summary) > 50 and not show_desc:
                summary = summary[:50] + "..."

            cells = [
                row.get("key") or "?",
                summary,
                f"[{status_color}]{status_name}[/{status_color}]",
                f"[{priority_color}]{priority_name}[/{priority_color}]",
            ]
            if show_time:
                cells.append(_format_time_spent(row.get("time_spent") or 0))
            
            table.add_row(*cells)

        console.print(table)
        console.print()


# --- LOCAL CACHE HELPERS ---

def _current_user_email() -> str:
    return os.getenv("JIRA_EMAIL", "").strip()


def _resolve_filters(filters: dict) -> dict:
    """
    Turn AI-produced filter shorthand into kwargs for `jira_store.query_tickets`.

    - `assignee_self`/`reporter_self` → `assignee_email`/`reporter_email` via JIRA_EMAIL.
    - If the AI did not specify any identity filter, default to current-user scope
      (matches the historical "my tasks" behavior).
    - Relative dates pass through (resolved inside `query_tickets`).
    - Single-string status/priority/etc. are coerced to lists.
    """
    filters = dict(filters or {})
    identity_set = any(filters.get(k) for k in ("assignee_self", "reporter_self", "assignee_email", "reporter_email"))
    if not identity_set:
        filters["assignee_self"] = True

    out: dict = {}
    out["project"] = filters.get("project")
    out["projects"] = filters.get("projects")
    for key in ("status", "status_category", "priority", "issue_type", "labels"):
        v = filters.get(key)
        if isinstance(v, str):
            v = [v]
        out[key] = v

    email = _current_user_email()
    if filters.get("assignee_self") and email:
        out["assignee_email"] = email
    if filters.get("reporter_self") and email:
        out["reporter_email"] = email
    if filters.get("assignee_email"):
        out["assignee_email"] = filters["assignee_email"]
    if filters.get("reporter_email"):
        out["reporter_email"] = filters["reporter_email"]

    for key in ("updated_after", "updated_before", "created_after", "text_search"):
        if filters.get(key):
            out[key] = filters[key]

    out["open_only"] = bool(filters.get("open_only", False))
    out["order_by"] = filters.get("order_by") or "updated"
    out["order_dir"] = filters.get("order_dir") or "DESC"
    out["limit"] = int(filters.get("limit") or 50)
    return out


def _filter_summary(filters: dict) -> str:
    """One-line debug string describing the filters that will hit the cache."""
    parts = []
    for k in ("project", "projects", "status", "status_category", "priority",
              "issue_type", "labels", "assignee_email", "reporter_email",
              "updated_after", "updated_before", "created_after",
              "text_search", "open_only"):
        v = filters.get(k)
        if v in (None, [], False, ""):
            continue
        parts.append(f"{k}={v}")
    parts.append(f"order={filters.get('order_by')} {filters.get('order_dir')}")
    parts.append(f"limit={filters.get('limit')}")
    return " ".join(parts)


def _empty_cache_hint(scope: str) -> str:
    return (
        f"No {scope} in local cache. Run [bold]smart-log jira-sync[/bold] "
        f"(or [bold]smart-log jira-sync --key <KEY>[/bold] for a single ticket)."
    )


def _ensure_ticket_cached(key: str) -> Optional[dict]:
    """
    Return the cached ticket; if missing, pull just that one ticket from Jira
    and re-read. Returns `None` only if the ticket also can't be fetched from API.
    """
    import jira_store as _store
    import jira_sync as _sync

    key_u = key.upper()
    with _store.open_db() as conn:
        ticket = _store.get_ticket(conn, key_u)
    if ticket:
        return ticket

    console.print(f"[dim]{key_u} not cached — pulling from Jira...[/dim]")
    try:
        _sync.sync_single(get_jira_client(), key_u)
    except Exception as e:
        console.print(f"[yellow]Single-ticket sync failed: {e}[/yellow]")
        return None
    with _store.open_db() as conn:
        return _store.get_ticket(conn, key_u)


def _refresh_ticket_cache(jira, key: str) -> bool:
    """
    Force-refresh one ticket in the local cache (used after we write to Jira,
    so the cache reflects the new worklog / time_spent / status).

    Failures are non-fatal — the write to Jira already succeeded; the user
    just won't see the refreshed numbers locally until the next sync.
    """
    import jira_sync as _sync

    try:
        _sync.sync_single(jira, key.upper())
        return True
    except Exception as e:
        console.print(f"[yellow]Cache refresh for {key} skipped: {e}[/yellow]")
        return False


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context):
    """
    Smart Logger - AI-powered work logging to Jira and Notion.
    
    Just describe what you want in natural language:
    
    Examples:
        python main.py "2h on GBI-123 implementing feature"
        python main.py "my tasks"
        python main.py "show in progress bugs"
        python main.py "what is GBI-123"
    """
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())

# --- SMART HANDLER ---

def _smart_handler(input_text: str, project: Optional[str] = None):
    """
    Smart handler that uses AI orchestrator to route user input.
    """
    # Use default project from env if not specified
    if not project:
        project = os.getenv("NOTION_PROJECT_DEFAULT_NAME", "")
    
    def log_work_handler(log_data: dict) -> OrchestratorResult:
        """Handle log_work intent."""
        issue_key = log_data.get('key', '')
        time_jira = log_data.get('time_jira', '')
        time_hours = log_data.get('time_hours', 0)
        description = log_data.get('desc', '')
        task_type = log_data.get('task_type', 'Development')
        
        console.print(f"[green]Parsed:[/green] {issue_key or 'No ticket'} | {time_jira} ({time_hours}h) | {task_type} | {description}")

        if _dry_run_enabled():
            console.print("[yellow]Dry run enabled: skipping Jira and Notion submission.[/yellow]")
            return OrchestratorResult(success=True, intent=Intent.LOG_WORK, message="Dry run completed", data=log_data)
        
        # Title comes from local cache (single-ticket sync on miss); only the
        # write goes to the Jira API.
        issue_title = ""
        jira_logged = False
        
        if is_valid_jira_key(issue_key):
            cached = _ensure_ticket_cached(issue_key)
            if cached:
                issue_title = cached.get("summary") or ""
            try:
                jira = get_jira_client()
                jira.add_worklog(issue=issue_key, timeSpent=time_jira, comment=description)
                console.print(f"[bold green]Logged to Jira: {issue_key}[/bold green]")
                jira_logged = True

                if _refresh_ticket_cache(jira, issue_key):
                    console.print(f"[dim]Local cache refreshed for {issue_key}[/dim]")

                memory = load_memory()
                memory = add_issue(memory, issue_key, issue_title)
                save_memory(memory)
            except Exception as e:
                console.print(f"[yellow]Jira skipped: {e}[/yellow]")
        else:
            console.print("[dim]No Jira ticket, skipping Jira.[/dim]")
        
        # Sync to Notion
        try:
            if jira_logged and issue_title:
                proof_of_works = f"{issue_key}: {issue_title}"
            elif is_valid_jira_key(issue_key):
                proof_of_works = f"{issue_key}: {description}"
            else:
                proof_of_works = description
            
            submit_notion_form(
                issue_key=issue_key if is_valid_jira_key(issue_key) else "",
                description=proof_of_works,
                time_hours=time_hours,
                task_type=task_type,
                project=project,
            )
            console.print("[bold green]Synced to Notion![/bold green]")
            
        except NotionAuthError as e:
            console.print(f"[red]Notion Auth Error: {e}[/red]")
            console.print("[yellow]Run 'python main.py notion-login' to re-authenticate.[/yellow]")
            return OrchestratorResult(success=False, intent=Intent.LOG_WORK, message=str(e))
        except NotionFormError as e:
            console.print(f"[red]Notion Error: {e}[/red]")
            return OrchestratorResult(success=False, intent=Intent.LOG_WORK, message=str(e))
        
        return OrchestratorResult(success=True, intent=Intent.LOG_WORK, message="Work logged successfully")
    
    def query_tasks_handler(query_plan: dict) -> OrchestratorResult:
        """
        Handle query_tasks intent. Reads from the **local Jira cache** only.

        Expects the AI plan to contain `filters` + `display`. Legacy `jql` keys
        are ignored — the AI prompt has been retargeted at structured filters.
        """
        import jira_store as _store

        try:
            display = query_plan.get("display", {}) or {}
            reasoning = query_plan.get("reasoning", "")
            if reasoning:
                console.print(f"[dim]AI Plan: {reasoning}[/dim]")

            raw_filters = query_plan.get("filters") or {}
            filters = _resolve_filters(raw_filters)
            console.print(f"[dim]Filter: {_filter_summary(filters)}[/dim]")

            with _store.open_db() as conn:
                rows = _store.query_tickets(conn, **filters)

            if not rows:
                console.print(f"[yellow]No matching tickets in local cache.[/yellow]")
                console.print(f"[dim]{_empty_cache_hint('tickets')}[/dim]")
                return OrchestratorResult(success=True, intent=Intent.QUERY_TASKS, message="No tasks found")

            show_time = display.get("show_time_spent", False)
            columns = display.get("columns") or ["key", "summary", "status", "priority"]
            show_desc = "description" in columns
            display_format = display.get("format", "table")
            group_by = display.get("group_by")

            if display_format == "grouped" and group_by:
                _display_grouped_tasks(rows, group_by, show_desc, show_time)
            else:
                _display_tasks_table(rows, show_desc, show_time)

            return OrchestratorResult(success=True, intent=Intent.QUERY_TASKS, message=f"Found {len(rows)} tasks")
        except Exception as e:
            console.print(f"[red]Cache query error: {e}[/red]")
            return OrchestratorResult(success=False, intent=Intent.QUERY_TASKS, message=str(e))

    def task_detail_handler(issue_key: str) -> OrchestratorResult:
        """Handle task_detail intent. Reads from cache; pulls one ticket if missing."""
        import jira_store as _store

        try:
            ticket = _ensure_ticket_cached(issue_key)
            if not ticket:
                console.print(f"[yellow]{issue_key} not found.[/yellow]")
                return OrchestratorResult(success=False, intent=Intent.TASK_DETAIL, message="Ticket not found")

            memory = load_memory()
            memory = add_issue(memory, ticket["key"], ticket.get("summary") or "")
            save_memory(memory)

            with _store.open_db() as conn:
                links = _store.get_links(conn, ticket["key"])
                comments = _store.get_comments(conn, ticket["key"])

            console.print(f"\n[bold cyan]{ticket['key']}[/bold cyan]: {ticket.get('summary') or ''}")
            console.print(f"[dim]Status:[/dim] {ticket.get('status') or '-'}")
            console.print(f"[dim]Priority:[/dim] {ticket.get('priority') or '-'}")
            console.print(f"[dim]Type:[/dim] {ticket.get('issue_type') or '-'}")
            console.print(f"[dim]Assignee:[/dim] {ticket.get('assignee') or '-'}")
            if ticket.get("updated"):
                console.print(f"[dim]Updated:[/dim] {ticket['updated']}")

            desc = ticket.get("description")
            if desc:
                snippet = desc if len(desc) <= 500 else desc[:500] + "..."
                console.print(f"\n[dim]Description:[/dim]\n{snippet}")

            if links:
                console.print(f"\n[dim]Links:[/dim] " + ", ".join(
                    f"{l.get('label') or l['link_type']}→{l['target_key']}" for l in links[:6]
                ))
            if comments:
                console.print(f"[dim]Comments cached:[/dim] {len(comments)}")

            return OrchestratorResult(success=True, intent=Intent.TASK_DETAIL, message="Task details retrieved")
        except Exception as e:
            console.print(f"[red]Cache read error: {e}[/red]")
            return OrchestratorResult(success=False, intent=Intent.TASK_DETAIL, message=str(e))

    def work_summary_handler(period: Optional[str], project_filter: Optional[str] = None) -> OrchestratorResult:
        """Handle work_summary intent. Aggregates **local** worklogs (no API)."""
        from datetime import datetime, timedelta
        import jira_reports as _reports

        # The AI may emit `period: null` for vague summary requests
        # ("summary my tasks"); fall back to a sensible default.
        period = (period or "").strip().lower() or "last_week"

        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        period_label = period.replace("_", " ").title()

        if period == "today":
            start, end = today, today + timedelta(days=1)
        elif period == "yesterday":
            start, end = today - timedelta(days=1), today
        elif period == "this_week":
            start, end = today - timedelta(days=today.weekday()), today + timedelta(days=1)
        elif period == "last_week":
            start = today - timedelta(days=today.weekday() + 7)
            end = today - timedelta(days=today.weekday())
        elif period == "this_month":
            start, end = today.replace(day=1), today + timedelta(days=1)
        elif period == "last_month":
            first = today.replace(day=1)
            start = (first - timedelta(days=1)).replace(day=1)
            end = first
        else:
            start, end = today - timedelta(days=7), today + timedelta(days=1)
            period_label = "Last 7 Days"

        start_str = start.strftime("%Y-%m-%d")
        end_str = end.strftime("%Y-%m-%d")
        console.print(f"[dim]Reading local worklogs from {start_str} to {end_str}...[/dim]")

        try:
            summary = _reports.worklog_summary(
                author_email=_current_user_email() or None,
                start=start_str,
                end=end_str,
            )
        except Exception as e:
            console.print(f"[red]Cache read error: {e}[/red]")
            return OrchestratorResult(success=False, intent=Intent.WORK_SUMMARY, message=str(e))

        if project_filter:
            project_filter_u = project_filter.upper()
            summary["by_ticket"] = [t for t in summary["by_ticket"] if (t.get("key") or "").startswith(project_filter_u + "-")]
            summary["by_project"] = {k: v for k, v in summary["by_project"].items() if k == project_filter_u}
            summary["total_seconds"] = sum(summary["by_project"].values())

        if summary["total_seconds"] == 0:
            console.print(f"[yellow]No local worklogs for {period_label}.[/yellow]")
            console.print(f"[dim]{_empty_cache_hint('worklogs')}[/dim]")
            return OrchestratorResult(success=True, intent=Intent.WORK_SUMMARY, message="No worklogs found")

        total_hours = summary["total_seconds"] / 3600
        console.print(f"\n[bold cyan]Work Summary: {period_label}[/bold cyan]")
        console.print(f"[bold green]Total: {_format_time_spent(summary['total_seconds'])} ({total_hours:.1f} hours)[/bold green]\n")

        if summary["by_date"]:
            console.print("[bold]By Date:[/bold]")
            for d, seconds in summary["by_date"].items():
                hours = seconds / 3600
                console.print(f"  {d}: {_format_time_spent(seconds)} ({hours:.1f}h)")
            console.print()

        if summary["by_project"]:
            console.print("[bold]By Project:[/bold]")
            for proj, seconds in summary["by_project"].items():
                console.print(f"\n  [cyan]{proj}[/cyan]: {_format_time_spent(seconds)} ({seconds/3600:.1f}h)")
                proj_tickets = [t for t in summary["by_ticket"] if (t.get("key") or "").startswith(proj + "-")]
                for t in proj_tickets[:10]:
                    tsum = (t.get("summary") or "")[:45]
                    console.print(f"    {t['key']}: {_format_time_spent(t['seconds'])} - {tsum}")

        return OrchestratorResult(success=True, intent=Intent.WORK_SUMMARY, message=f"Total: {total_hours:.1f} hours")

    def notion_worklogs_handler(plan: dict) -> OrchestratorResult:
        """Handle notion_worklogs intent — reads from Notion queryCollection API."""
        kwargs = build_query_kwargs(plan)
        if plan.get("period") and not plan.get("since") and not plan.get("until"):
            _, _, period_label = period_to_date_range(plan.get("period"))
            console.print(f"[dim]Fetching Notion worklogs ({period_label})...[/dim]")
        else:
            console.print("[dim]Fetching Notion worklogs...[/dim]")

        try:
            result = query_worklogs(**kwargs, quiet=True)
        except NotionAuthError as e:
            return OrchestratorResult(
                success=False,
                intent=Intent.NOTION_WORKLOGS,
                message=f"Notion Auth Error: {e}. Run 'smart-log notion-login' to re-authenticate.",
            )
        except NotionWorklogError as e:
            return OrchestratorResult(success=False, intent=Intent.NOTION_WORKLOGS, message=str(e))

        if not result["entries"]:
            return OrchestratorResult(
                success=True,
                intent=Intent.NOTION_WORKLOGS,
                message="No Notion worklog entries found for the given filters.",
            )

        _display_notion_worklogs(result)
        total = result.get("total_effort_hours") or 0
        return OrchestratorResult(
            success=True,
            intent=Intent.NOTION_WORKLOGS,
            message=f"Found {len(result['entries'])} entries, {total:g}h total",
        )
    
    # Run orchestrator
    result = orchestrate(
        user_input=input_text,
        log_work_handler=log_work_handler,
        query_tasks_handler=query_tasks_handler,
        task_detail_handler=task_detail_handler,
        work_summary_handler=work_summary_handler,
        notion_worklogs_handler=notion_worklogs_handler,
    )
    
    # Handle clarify and help intents
    if result.intent == Intent.HELP:
        console.print(result.message)
    elif result.intent == Intent.CLARIFY:
        console.print(f"[yellow]{result.message}[/yellow]")
    elif not result.success:
        console.print(f"[red]Error: {result.message}[/red]")


@app.command("_smart_", hidden=True)
def _smart_nlp(
    ctx: typer.Context,
    project: Optional[str] = typer.Option(
        None, "--project", "-p", help="Notion project name for logging"
    ),
    words: List[str] = typer.Argument(
        ...,
        metavar="TEXT",
        help="Natural language (e.g. '2h on GBI-123' or 'my tasks')",
    ),
):
    """Internal entry for bare `smart-log <text>` (see _prepend_smart_if_needed)."""
    input_text = " ".join(words).strip()
    if not input_text:
        console.print(ctx.get_help())
        raise typer.Exit(0)
    _smart_handler(input_text, project)


# --- SERVICES ---

def get_jira_client():
    return JIRA(
        server=os.getenv("JIRA_SERVER"),
        basic_auth=(os.getenv("JIRA_EMAIL"), os.getenv("JIRA_API_TOKEN"))
    )

def ai_parse_log(natural_input: str):
    """
    Uses AI to convert natural language into structured data.
    """
    client = get_ai_client()
    
    # Task type classification mapping
    task_type_guide = """
    Classify the work into ONE of these task types:
    - "Development": coding, programming, implementing features, fixing bugs, debugging, technical implementation
    - "Design": UI/UX design, wireframes, mockups, visual design, design review
    - "Meeting": meetings, calls, sync-ups, standups, discussions, interviews
    - "Documentation": writing docs, README, API docs, technical writing, wikis
    - "Research": investigating, exploring, POC, spike, learning, analysis
    - "Planning": sprint planning, roadmap, estimation, task breakdown, architecture planning
    - "Other": anything that doesn't fit above categories
    """
    
    prompt = f"""
    Extract the following from this text: "{natural_input}"
    1. Issue Key (e.g., PROJ-123, GBI-645, KFS-644)
    2. Time Spent in Jira format (like '2h', '30m', '1h 30m')
    3. Time as decimal hours (e.g., 2.0, 0.5, 1.5)
    4. Description (a clean summary of the work)
    5. Task type based on this guide:
    {task_type_guide}
    
    Return ONLY a JSON string: {{"key": "...", "time_jira": "...", "time_hours": ..., "desc": "...", "task_type": "..."}}
    
    Examples:
    - "2h on GBI-645 implementing Redis" -> {{"key": "GBI-645", "time_jira": "2h", "time_hours": 2.0, "desc": "implementing Redis", "task_type": "Development"}}
    - "1h meeting for sprint planning" -> {{"key": "...", "time_jira": "1h", "time_hours": 1.0, "desc": "sprint planning", "task_type": "Meeting"}}
    - "30m writing API docs for GBI-123" -> {{"key": "GBI-123", "time_jira": "30m", "time_hours": 0.5, "desc": "writing API docs", "task_type": "Documentation"}}
    - "1h researching Redis Sentinel" -> {{"key": "...", "time_jira": "1h", "time_hours": 1.0, "desc": "researching Redis Sentinel", "task_type": "Research"}}
    """
    raw = client.generate(prompt)
    clean_json = raw.replace('```json', '').replace('```', '').strip()
    
    try:
        return json.loads(clean_json)
    except json.JSONDecodeError:
        raise ValueError(
            f"Could not parse work log from input. AI response: {clean_json[:200]}...\n"
            "Hint: Work log entries should include time spent (e.g., '2h on GBI-123 fixing bugs').\n"
            "To list your tasks, use: python main.py tasks"
        )


def ai_parse_task_query(natural_input: str) -> dict:
    """
    Uses AI to convert natural language into JQL filter components.
    """
    client = get_ai_client()
    
    prompt = f"""
    Convert this natural language request into Jira JQL filter components: "{natural_input}"
    
    Extract any of these filters if mentioned:
    - status: exact Jira status like "To Do", "In Progress", "Done", "Blocked"
    - priority: "Highest", "High", "Medium", "Low", "Lowest"
    - issue_type: "Bug", "Task", "Story", "Epic"
    - project: project key like "PROJ"
    - updated: relative time like "-1w" (last week), "-1d" (last day), "-1m" (last month)
    - created: relative time like "-1w", "-1d", "-1m"
    - text_search: keywords to search in summary/description
    
    Return ONLY a JSON object with the filters found. Use null for filters not mentioned.
    Example: {{"status": "In Progress", "priority": "High", "issue_type": null, "project": null, "updated": "-1w", "created": null, "text_search": null}}
    """
    raw = client.generate(prompt)
    clean_json = raw.replace('```json', '').replace('```', '').strip()
    return json.loads(clean_json)


def build_jql_from_filters(filters: dict) -> str:
    """
    Build a JQL query string from parsed filter components.
    """
    conditions = ["assignee = currentUser()"]
    
    if filters.get("status"):
        conditions.append(f'status = "{filters["status"]}"')
    if filters.get("priority"):
        conditions.append(f'priority = "{filters["priority"]}"')
    if filters.get("issue_type"):
        conditions.append(f'issuetype = "{filters["issue_type"]}"')
    if filters.get("project"):
        conditions.append(f'project = "{filters["project"]}"')
    if filters.get("updated"):
        conditions.append(f'updated >= {filters["updated"]}')
    if filters.get("created"):
        conditions.append(f'created >= {filters["created"]}')
    if filters.get("text_search"):
        conditions.append(f'text ~ "{filters["text_search"]}"')
    
    return " AND ".join(conditions) + " ORDER BY updated DESC"

# --- COMMANDS ---

def is_valid_jira_key(key: str) -> bool:
    """Check if a string looks like a valid Jira issue key (e.g., PROJ-123)."""
    import re
    if not key or key in ("...", "null", "None", ""):
        return False
    # Jira keys are typically: PROJECT-NUMBER (e.g., GBI-645, KFS-123)
    return bool(re.match(r'^[A-Z][A-Z0-9]+-\d+$', key.upper()))


@app.command()
def log(
    task: str,
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Notion project name (e.g., 'DF', 'HF'). Defaults to NOTION_PROJECT_DEFAULT"),
):
    """
    Smart Log: "Spent 2h on PROJ-123 fixing bugs"
    
    If a valid Jira ticket is found, logs to both Jira and Notion.
    Otherwise, logs only to Notion.
    
    Examples:
        log "2h on GBI-645 implementing feature"      # Uses NOTION_PROJECT_DEFAULT
        log -p DF "2h on GBI-645 implementing feature"
        log --project DF "1h team meeting"
    """
    # Use default project from env if not specified
    if not project:
        project = os.getenv("NOTION_PROJECT_DEFAULT_NAME", "")
        if project:
            console.print(f"[dim]Using default project: {project}[/dim]")
    
    console.print(f"[bold blue]AI parsing:[/bold blue] '{task}'...")
    
    # 1. AI Parsing
    try:
        parsed_data = ai_parse_log(task)
    except ValueError as e:
        console.print(f"[red]❌ Parse Error: {e}[/red]")
        raise typer.Exit(1)
    issue_key = parsed_data.get('key', '')
    time_jira = parsed_data['time_jira']
    time_hours = parsed_data['time_hours']
    description = parsed_data['desc']
    task_type = parsed_data.get('task_type', 'Development')
    
    console.print(f"[green]✔ Parsed:[/green] {issue_key or 'No ticket'} | {time_jira} ({time_hours}h) | {task_type} | {description}")

    if _dry_run_enabled():
        console.print("[yellow]Dry run enabled: skipping Jira and Notion submission.[/yellow]")
        return

    # 2. Try to log to Jira (only if valid ticket key)
    issue_title = ""
    jira_logged = False
    
    if is_valid_jira_key(issue_key):
        cached = _ensure_ticket_cached(issue_key)
        if cached:
            issue_title = cached.get("summary") or ""
        try:
            jira = get_jira_client()
            jira.add_worklog(issue=issue_key, timeSpent=time_jira, comment=description)
            console.print(f"[bold green]Logged to Jira: {issue_key}[/bold green]")
            jira_logged = True

            if _refresh_ticket_cache(jira, issue_key):
                console.print(f"[dim]Local cache refreshed for {issue_key}[/dim]")

            memory = load_memory()
            memory = add_issue(memory, issue_key, issue_title)
            save_memory(memory)
        except Exception as e:
            console.print(f"[yellow]Jira skipped: {e}[/yellow]")
            console.print("[dim]Will continue to log to Notion only.[/dim]")
    else:
        console.print("[dim]No Jira ticket found, skipping Jira.[/dim]")

    # 3. Sync to Notion via Form API
    try:
        console.print("[dim]Syncing to Notion...[/dim]")
        
        # Build proof of works text
        if jira_logged and issue_title:
            # Use Jira ticket title if we logged to Jira
            proof_of_works = f"{issue_key}: {issue_title}"
        elif is_valid_jira_key(issue_key):
            # Has ticket key but couldn't fetch title
            proof_of_works = f"{issue_key}: {description}"
        else:
            # No ticket, just use description
            proof_of_works = description
        
        submit_notion_form(
            issue_key=issue_key if is_valid_jira_key(issue_key) else "",
            description=proof_of_works,
            time_hours=time_hours,
            task_type=task_type,
            project=project,
        )
        console.print("[bold green]✔ Synced to Notion![/bold green]")
    except NotionAuthError as e:
        console.print(f"[red]❌ Notion Auth Error: {e}[/red]")
        console.print("[yellow]Run 'python main.py notion-login' to re-authenticate.[/yellow]")
    except NotionFormError as e:
        console.print(f"[red]❌ Notion Error: {e}[/red]")


@app.command()
def notion_login():
    """
    Login to Notion via browser to get authentication token.
    
    Opens a browser window for you to login. Token is saved for future use.
    """
    console.print("[bold blue]🔐 Notion Login[/bold blue]")
    console.print("[dim]This will open a browser window for you to login to Notion.[/dim]\n")
    
    try:
        # Force new login
        clear_token()
        creds = get_notion_credentials(force_login=True)
        
        console.print("\n[bold green]✔ Login successful![/bold green]")
        console.print(f"[dim]User ID: {creds.get('user_id', 'N/A')}[/dim]")
        console.print("[green]You can now use 'python main.py log' to log tasks![/green]")
        
    except Exception as e:
        console.print(f"[red]❌ Login failed: {e}[/red]")


@app.command()
def notion_status():
    """
    Show Notion authentication status and configuration.
    """
    console.print("[bold blue]📋 Notion Status[/bold blue]\n")
    
    # Check stored token
    stored = load_stored_token()
    if stored and stored.get("token_v2"):
        console.print("[green]✔ Token:[/green] Stored")
        console.print(f"  [dim]User ID: {stored.get('user_id', 'N/A')}[/dim]")
        console.print(f"  [dim]Saved at: {stored.get('saved_at', 'N/A')}[/dim]")
    else:
        console.print("[yellow]✗ Token:[/yellow] Not found")
        console.print("  [dim]Run 'python main.py notion-login' to authenticate.[/dim]")
    
    # Check env config
    console.print("\n[bold]Configuration (.env):[/bold]")
    
    form_id = os.getenv("NOTION_FORM_ID")
    space_id = os.getenv("NOTION_SPACE_ID")
    
    if form_id:
        console.print(f"[green]✔ NOTION_FORM_ID:[/green] {form_id[:8]}...")
    else:
        console.print("[yellow]✗ NOTION_FORM_ID:[/yellow] Not set")
    
    if space_id:
        console.print(f"[green]✔ NOTION_SPACE_ID:[/green] {space_id[:8]}...")
    else:
        console.print("[yellow]✗ NOTION_SPACE_ID:[/yellow] Not set")
    
    email = os.getenv("NOTION_EMAIL")
    if email:
        console.print(f"[green]✔ NOTION_EMAIL:[/green] {email}")
    else:
        console.print("[dim]○ NOTION_EMAIL:[/dim] Not set (optional, for pre-fill)")


@app.command()
def notion_logout():
    """
    Clear stored Notion authentication token.
    """
    clear_token()
    console.print("[green]✔ Logged out from Notion.[/green]")


def _display_notion_worklogs(result: dict) -> None:
    """Render Notion worklog query results as a Rich table."""
    entries = result.get("entries") or []
    if not entries:
        console.print("[yellow]No worklog entries found for the given filters.[/yellow]")
        return

    table = Table(title="Notion Worklogs", show_lines=True)
    table.add_column("Created by", style="cyan", no_wrap=True)
    table.add_column("Project", style="green", no_wrap=True)
    table.add_column("Date", no_wrap=True)
    table.add_column("Status", style="magenta")
    table.add_column("Effort", justify="right", style="yellow")
    table.add_column("Focus", style="blue")
    table.add_column("Key deliverables", style="white")

    for row in entries:
        effort = row.get("effort_hours")
        effort_str = f"{effort:g}h" if effort is not None else "-"
        deliverables = row.get("key_deliverables") or "-"
        if len(deliverables) > 70:
            deliverables = deliverables[:70] + "..."
        table.add_row(
            row.get("created_by") or "-",
            row.get("project") or "-",
            row.get("date") or "-",
            row.get("status") or "-",
            effort_str,
            row.get("focus_areas") or "-",
            deliverables,
        )

    console.print(table)
    total = result.get("total_effort_hours") or 0
    footer = f"[dim]Showing {len(entries)} entries · total effort {total:g}h[/dim]"
    if result.get("truncated"):
        footer += " · [yellow]results may be truncated (increase --limit or narrow filters)[/yellow]"
    console.print(footer)


@app.command("notion-worklogs")
def notion_worklogs_cmd(
    user: Optional[List[str]] = typer.Option(
        None,
        "--user",
        "-u",
        help="Filter by user name/alias (NOTION_WORKLOG_USERS). Defaults to authenticated user.",
    ),
    project: Optional[str] = typer.Option(
        None,
        "--project",
        "-p",
        help="Filter by Notion project name (NOTION_PROJECT_* mapping).",
    ),
    limit: int = typer.Option(50, "--limit", "-n", help="Maximum rows to show"),
    since: Optional[str] = typer.Option(
        None, "--since", help="Include entries on/after YYYY-MM-DD"
    ),
    until: Optional[str] = typer.Option(
        None, "--until", help="Include entries on/before YYYY-MM-DD"
    ),
    as_json: bool = typer.Option(False, "--json", help="Print raw JSON instead of a table"),
):
    """
    Fetch worklog entries from the Notion time-tracking database.

    Examples:
        smart-log notion-worklogs
        smart-log notion-worklogs -u antran -u "Thuần Thiên"
        smart-log notion-worklogs -p Kafi --since 2026-07-01
        smart-log notion-worklogs --json -n 100
    """
    try:
        result = query_worklogs(
            users=user,
            project=project,
            limit=limit,
            since=since,
            until=until,
            quiet=as_json,
        )
    except NotionAuthError as e:
        console.print(f"[red]Notion Auth Error: {e}[/red]")
        console.print("[yellow]Run 'smart-log notion-login' to re-authenticate.[/yellow]")
        raise typer.Exit(1)
    except NotionWorklogError as e:
        console.print(f"[red]Notion worklog error: {e}[/red]")
        raise typer.Exit(1)

    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    _display_notion_worklogs(result)


@app.command()
def tasks(
    query: Optional[str] = typer.Argument(None, help="Natural language query (e.g., 'high priority bugs', 'in progress tasks')"),
    status: Optional[str] = typer.Option(None, "--status", "-s", help="Filter by status (e.g., 'To Do', 'In Progress')"),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum number of tasks to show"),
):
    """
    Show your Jira tasks from the **local cache**.

    All reads come from the local SQLite store (`jira-sync` populates it).
    Use natural language for AI-planned filters, or `--status` for an explicit
    direct filter.

    Examples:
        tasks                              # Your tasks (cache)
        tasks "in progress"                # AI-planned filter
        tasks "high priority bugs"         # AI-planned filter
        tasks --status "In Progress"       # Explicit filter
    """
    import jira_store as _store

    try:
        if query:
            console.print(f"[bold blue]AI planning filter:[/bold blue] '{query}'...")
            from ai_orchestrator import plan_task_query
            plan = plan_task_query(query)
            filters_in = plan.get("filters", {})
            if plan.get("reasoning"):
                console.print(f"[dim]Plan: {plan['reasoning']}[/dim]")
        elif status:
            filters_in = {"assignee_self": True, "status": [status]}
        else:
            filters_in = {"assignee_self": True}

        filters_in.setdefault("limit", limit)
        filters = _resolve_filters(filters_in)
        console.print(f"[dim]Filter: {_filter_summary(filters)}[/dim]")

        with _store.open_db() as conn:
            rows = _store.query_tickets(conn, **filters)

        if not rows:
            console.print("[yellow]No tasks found in local cache.[/yellow]")
            console.print(f"[dim]{_empty_cache_hint('tickets')}[/dim]")
            return

        table = Table(title="My Jira Tasks", show_lines=True)
        table.add_column("Key", style="cyan", no_wrap=True)
        table.add_column("Summary", style="white")
        table.add_column("Status", style="magenta")
        table.add_column("Priority", style="yellow")

        for row in rows:
            summary = row.get("summary") or ""
            if len(summary) > 60:
                summary = summary[:60] + "..."
            table.add_row(
                row.get("key") or "?",
                summary,
                row.get("status") or "-",
                row.get("priority") or "-",
            )

        console.print(table)
        console.print(f"[dim]Showing {len(rows)} of your tasks (local cache)[/dim]")
    except Exception as e:
        console.print(f"[red]Cache read error: {e}[/red]")


# --- JIRA LOCAL CACHE COMMANDS ---

def _format_duration_seconds(seconds: int) -> str:
    return _format_time_spent(seconds)


@app.command("jira-sync")
def jira_sync_cmd(
    jql: Optional[str] = typer.Option(None, "--jql", help="Custom JQL (overrides other filters)"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Scope sync to a single project key"),
    since: Optional[str] = typer.Option(None, "--since", help="Incremental: only tickets updated >= YYYY-MM-DD"),
    full: bool = typer.Option(False, "--full", help="Ignore the default 180d window (pull everything matching)"),
    only_mine: bool = typer.Option(True, "--mine/--all", help="Restrict to tickets where you are assignee or reporter"),
    page_size: int = typer.Option(100, "--page-size", help="Tickets fetched per Jira API call"),
    max_tickets: Optional[int] = typer.Option(None, "--max", help="Cap total tickets pulled"),
    no_worklogs: bool = typer.Option(False, "--no-worklogs", help="Skip the per-ticket worklog fetch (faster)"),
    key: Optional[str] = typer.Option(None, "--key", help="Sync a single ticket key (e.g. GBI-645)"),
):
    """
    Pull Jira tickets (with descriptions, comments, links, worklogs) into the local cache.

    Examples:
        smart-log jira-sync                       # default: my work, last 180 days
        smart-log jira-sync --project GBI         # scope to one project
        smart-log jira-sync --since 2025-01-01    # incremental from a date
        smart-log jira-sync --key GBI-645         # refresh one ticket
        smart-log jira-sync --jql 'project = GBI AND status != Done'
    """
    import jira_sync

    try:
        jira = get_jira_client()
    except Exception as e:
        console.print(f"[red]Jira connection error: {e}[/red]")
        raise typer.Exit(1)

    if key:
        console.print(f"[bold blue]Syncing single ticket:[/bold blue] {key}")
        try:
            result = jira_sync.sync_single(jira, key, fetch_worklogs=not no_worklogs)
        except Exception as e:
            console.print(f"[red]Sync failed: {e}[/red]")
            raise typer.Exit(1)
    else:
        window_days = 0 if full else 180
        effective_jql = jql or jira_sync.build_default_jql(
            project=project,
            since=since,
            only_mine=only_mine,
            window_days=window_days,
        )
        console.print(f"[bold blue]Syncing Jira → local cache[/bold blue]")
        try:
            result = jira_sync.sync(
                jira,
                jql=effective_jql,
                page_size=page_size,
                max_tickets=max_tickets,
                fetch_worklogs=not no_worklogs,
                progress=lambda msg: console.print(f"[dim]{msg}[/dim]"),
            )
        except Exception as e:
            console.print(f"[red]Sync failed: {e}[/red]")
            raise typer.Exit(1)

    console.print(
        f"[bold green]Done.[/bold green] "
        f"tickets={result.tickets} comments={result.comments} "
        f"links={result.links} worklogs={result.worklogs} attachments={result.attachments}"
    )
    if result.errors:
        console.print(f"[yellow]{len(result.errors)} per-ticket error(s):[/yellow]")
        for err in result.errors[:5]:
            console.print(f"  [dim]- {err}[/dim]")
        if len(result.errors) > 5:
            console.print(f"  [dim]... ({len(result.errors) - 5} more)[/dim]")


@app.command("jira-show")
def jira_show_cmd(
    key: str = typer.Argument(..., help="Jira issue key (e.g. GBI-645)"),
    comments: bool = typer.Option(True, "--comments/--no-comments", help="Include comments"),
    links: bool = typer.Option(True, "--links/--no-links", help="Include relationships"),
    worklogs: bool = typer.Option(False, "--worklogs", help="Include worklog entries"),
):
    """Show a ticket from the local cache. Run `jira-sync` first if it's missing."""
    import jira_store as _store

    key_u = key.upper()
    with _store.open_db() as conn:
        ticket = _store.get_ticket(conn, key_u)
        if not ticket:
            console.print(f"[yellow]{key_u} not in local cache. Run:[/yellow] smart-log jira-sync --key {key_u}")
            raise typer.Exit(1)
        comment_rows = _store.get_comments(conn, key_u) if comments else []
        link_rows = _store.get_links(conn, key_u) if links else []
        worklog_rows = _store.get_worklogs(conn, key_u) if worklogs else []

    console.print(f"\n[bold cyan]{ticket['key']}[/bold cyan]: {ticket.get('summary') or ''}")
    console.print(f"[dim]Status:[/dim] {ticket.get('status') or '-'}  "
                  f"[dim]Priority:[/dim] {ticket.get('priority') or '-'}  "
                  f"[dim]Type:[/dim] {ticket.get('issue_type') or '-'}")
    console.print(f"[dim]Assignee:[/dim] {ticket.get('assignee') or '-'}  "
                  f"[dim]Reporter:[/dim] {ticket.get('reporter') or '-'}")
    if ticket.get("parent_key") or ticket.get("epic_key"):
        console.print(f"[dim]Parent:[/dim] {ticket.get('parent_key') or '-'}  "
                      f"[dim]Epic:[/dim] {ticket.get('epic_key') or '-'}")
    if ticket.get("sprint"):
        sprint = ticket["sprint"]
        sname = sprint.get("name") if isinstance(sprint, dict) else sprint
        console.print(f"[dim]Sprint:[/dim] {sname}")
    if ticket.get("labels"):
        console.print(f"[dim]Labels:[/dim] {', '.join(ticket['labels'])}")
    console.print(f"[dim]Updated:[/dim] {ticket.get('updated') or '-'}")

    desc = ticket.get("description")
    if desc:
        snippet = desc if len(desc) <= 800 else desc[:800] + "..."
        console.print("\n[bold]Description:[/bold]")
        console.print(snippet)

    if link_rows:
        console.print("\n[bold]Relationships:[/bold]")
        for ln in link_rows:
            console.print(f"  {ln['direction']:>7} | {ln['label'] or ln['link_type']}: {ln['target_key']}")

    if comment_rows:
        console.print(f"\n[bold]Comments ({len(comment_rows)}):[/bold]")
        for c in comment_rows[-10:]:
            body = (c.get("body") or "").strip()
            if len(body) > 300:
                body = body[:300] + "..."
            console.print(f"  [cyan]{c.get('author') or '?'}[/cyan] "
                          f"[dim]{(c.get('created') or '')[:19]}[/dim]")
            console.print(f"    {body}")

    if worklog_rows:
        console.print(f"\n[bold]Worklogs ({len(worklog_rows)}):[/bold]")
        for w in worklog_rows:
            console.print(
                f"  [cyan]{w.get('author') or '?'}[/cyan] "
                f"[dim]{(w.get('started') or '')[:19]}[/dim] "
                f"{_format_duration_seconds(w.get('time_spent_seconds') or 0)}"
            )


@app.command("jira-search")
def jira_search_cmd(
    query: str = typer.Argument(..., help="FTS5 query (e.g. 'redis sentinel', 'auth NEAR/5 oauth')"),
    limit: int = typer.Option(15, "--limit", "-n", help="Max results"),
):
    """Full-text search across cached summaries, descriptions, and comments."""
    import jira_store as _store

    with _store.open_db() as conn:
        try:
            results = _store.search_tickets(conn, query, limit=limit)
        except Exception as e:
            console.print(f"[red]Search error: {e}[/red]")
            raise typer.Exit(1)

    if not results:
        console.print("[yellow]No matches in local cache.[/yellow]")
        return

    table = Table(title=f"Search: {query}", show_lines=False)
    table.add_column("Key", style="cyan", no_wrap=True)
    table.add_column("Summary", style="white")
    table.add_column("Status", style="magenta")
    table.add_column("Match", style="dim")

    for r in results:
        summary = (r.get("summary") or "")[:60]
        table.add_row(
            r["key"],
            summary,
            r.get("status") or "-",
            (r.get("snippet") or "").replace("\n", " ")[:80],
        )
    console.print(table)
    console.print(f"[dim]{len(results)} match(es)[/dim]")


@app.command("jira-stats")
def jira_stats_cmd(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Scope counts to one project"),
    stale_days: int = typer.Option(14, "--stale-days", help="Treat tickets idle >= N days as stale"),
):
    """Summary of the local Jira cache: counts, top assignees, stale tickets."""
    import jira_store as _store
    import jira_reports as _reports

    with _store.open_db() as conn:
        stats = _store.db_stats(conn)

    console.print("[bold cyan]Local Jira cache[/bold cyan]")
    console.print(f"  [dim]Path:[/dim] {stats['db_path']}")
    console.print(
        f"  [bold]{stats['tickets']}[/bold] tickets · "
        f"[bold]{stats['comments']}[/bold] comments · "
        f"[bold]{stats['links']}[/bold] links · "
        f"[bold]{stats['worklogs']}[/bold] worklogs · "
        f"[bold]{stats['attachments']}[/bold] attachments"
    )
    if stats["last_sync"]:
        ls = stats["last_sync"]
        console.print(
            f"  [dim]Last sync:[/dim] {ls.get('finished_at') or ls.get('started_at')} "
            f"(JQL: {(ls.get('jql') or '')[:80]})"
        )

    by_status = _reports.counts_by_status(project=project)
    if by_status:
        console.print("\n[bold]By status:[/bold]")
        for row in by_status:
            console.print(f"  {row['status'] or '-':<18} {row['n']:>4}  [dim]({row.get('status_category') or '-'})[/dim]")

    by_project = _reports.counts_by_project() if not project else []
    if by_project:
        console.print("\n[bold]By project:[/bold]")
        for row in by_project:
            console.print(f"  {row['project']:<10} {row['n']:>4}")

    by_assignee = _reports.counts_by_assignee(project=project, limit=10)
    if by_assignee:
        console.print("\n[bold]Top assignees:[/bold]")
        for row in by_assignee:
            console.print(f"  {row['assignee']:<30} {row['n']:>4}")

    stale = _reports.stale_tickets(days=stale_days, project=project, limit=10)
    if stale:
        console.print(f"\n[bold]Stale (idle ≥ {stale_days}d):[/bold]")
        for t in stale:
            console.print(
                f"  [cyan]{t['key']}[/cyan]  {(t.get('summary') or '')[:50]:<50}  "
                f"[magenta]{t.get('status') or '-'}[/magenta]  "
                f"[dim]{(t.get('updated') or '')[:10]}[/dim]"
            )


# --- JIRA ANALYTICAL REPORTS (skill-facing) ---

_REPORT_KINDS = ("digest", "daily", "epics", "scope", "stale", "worklog")


def _render_digest(d: dict) -> None:
    console.print(f"\n[bold cyan]Workload Digest[/bold cyan] — {d['total_open']} open tickets")
    recent_h = d["time_spent_recent_seconds"] / 3600
    prev_h = d["time_spent_prev_seconds"] / 3600
    delta = recent_h - prev_h
    delta_str = f"{delta:+.1f}h vs. prior {d['recent_window_days']}d"
    console.print(
        f"[dim]Time spent last {d['recent_window_days']}d:[/dim] "
        f"[bold]{recent_h:.1f}h[/bold]  [dim]({delta_str})[/dim]"
    )
    console.print(f"[dim]Stale (idle ≥ 14d):[/dim] {d['stale_count']}")

    if d["by_status_category"]:
        console.print("\n[bold]By status:[/bold]")
        for r in d["by_status_category"]:
            console.print(f"  {(r.get('status') or '-'):<20} [dim]{r.get('status_category') or '-':<12}[/dim] {r['n']:>4}")

    if d["by_priority"]:
        console.print("\n[bold]By priority:[/bold]")
        for r in d["by_priority"]:
            console.print(f"  {(r.get('priority') or '-'):<10} {r['n']:>4}")

    if d["by_project"]:
        console.print("\n[bold]By project:[/bold]")
        for r in d["by_project"]:
            console.print(f"  {r['project']:<10} {r['n']:>4}")

    if d["by_type"]:
        console.print("\n[bold]By type:[/bold]")
        for r in d["by_type"]:
            console.print(f"  {(r.get('issue_type') or '-'):<14} {r['n']:>4}")

    if d["top_priorities"]:
        console.print("\n[bold]Top priority (open):[/bold]")
        for t in d["top_priorities"]:
            console.print(
                f"  [yellow]{t['priority']:<8}[/yellow] [cyan]{t['key']}[/cyan]  "
                f"{(t.get('summary') or '')[:60]:<60} [magenta]{t.get('status') or '-'}[/magenta]"
            )

    if d["recent_activity"]:
        console.print("\n[bold]Recently active:[/bold]")
        for t in d["recent_activity"]:
            console.print(
                f"  [cyan]{t['key']}[/cyan]  {(t.get('summary') or '')[:60]:<60} "
                f"[dim]{(t.get('updated') or '')[:10]}[/dim]"
            )


def _render_daily(d: dict) -> None:
    def bucket(title: str, items: list, max_show: int = 10) -> None:
        if not items:
            console.print(f"[dim]{title}: none[/dim]")
            return
        console.print(f"\n[bold]{title}[/bold] ({len(items)})")
        for t in items[:max_show]:
            console.print(
                f"  [cyan]{t['key']}[/cyan]  [yellow]{t.get('priority') or '-':<8}[/yellow] "
                f"[magenta]{(t.get('status') or '-'):<14}[/magenta] "
                f"{(t.get('summary') or '')[:60]} [dim]{(t.get('updated') or '')[:10]}[/dim]"
            )
        if len(items) > max_show:
            console.print(f"  [dim]... ({len(items) - max_show} more)[/dim]")

    console.print("\n[bold cyan]Daily Plan[/bold cyan]")
    bucket("Focus now (In Progress)", d["in_progress"])
    bucket("Next up (High/Highest, To Do)", d["next_up"])
    bucket("Stale In Progress (needs unblocking)", d["stale_in_progress"])
    bucket("Blocked", d["blocked"])


def _render_epics(rows: list[dict]) -> None:
    if not rows:
        console.print("[yellow]No epics or tickets in scope.[/yellow]")
        return
    console.print("\n[bold cyan]Epic Rollup[/bold cyan]")
    table = Table(show_lines=False)
    table.add_column("Epic", style="cyan", no_wrap=True)
    table.add_column("Summary", style="white")
    table.add_column("Status", style="magenta")
    table.add_column("Tickets", justify="right")
    table.add_column("Done", justify="right")
    table.add_column("% Done", justify="right")
    table.add_column("Time", justify="right", style="green")
    table.add_column("Updated", style="dim")
    for r in rows:
        seconds = r.get("total_seconds") or 0
        table.add_row(
            r["epic_key"],
            (r.get("epic_summary") or "")[:50],
            r.get("epic_status") or "-",
            str(r["total"]),
            str(r["done"]),
            f"{r['percent_complete']:.0f}%",
            _format_time_spent(seconds),
            (r.get("last_updated") or "")[:10],
        )
    console.print(table)


def _render_scope(rows: list[dict], field: str) -> None:
    if not rows:
        console.print(f"[yellow]No {field} found in scope.[/yellow]")
        return
    console.print(f"\n[bold cyan]Scope distribution[/bold cyan] — by {field}")
    for r in rows:
        console.print(f"  {r['value']:<28} {r['count']:>4}")


def _render_stale(rows: list[dict]) -> None:
    if not rows:
        console.print("[yellow]No stale tickets.[/yellow]")
        return
    console.print(f"\n[bold cyan]Stale tickets[/bold cyan] ({len(rows)})")
    for t in rows:
        console.print(
            f"  [cyan]{t['key']}[/cyan]  {(t.get('summary') or '')[:55]:<55}  "
            f"[magenta]{t.get('status') or '-':<14}[/magenta] "
            f"[yellow]{t.get('priority') or '-':<8}[/yellow] "
            f"[dim]{(t.get('updated') or '')[:10]}[/dim]"
        )


def _render_worklog(summary: dict, period_label: str) -> None:
    if summary["total_seconds"] == 0:
        console.print(f"[yellow]No worklogs for {period_label}.[/yellow]")
        return
    total_h = summary["total_seconds"] / 3600
    console.print(f"\n[bold cyan]Worklog summary[/bold cyan] — {period_label}")
    console.print(f"[bold green]Total: {_format_time_spent(summary['total_seconds'])} ({total_h:.1f}h)[/bold green]")

    if summary["by_date"]:
        console.print("\n[bold]By date:[/bold]")
        for d, sec in summary["by_date"].items():
            console.print(f"  {d}: {_format_time_spent(sec)} ({sec/3600:.1f}h)")

    if summary["by_project"]:
        console.print("\n[bold]By project:[/bold]")
        for p, sec in summary["by_project"].items():
            console.print(f"  {p}: {_format_time_spent(sec)} ({sec/3600:.1f}h)")

    if summary["by_ticket"]:
        console.print("\n[bold]Top tickets:[/bold]")
        for t in summary["by_ticket"][:10]:
            tsum = (t.get("summary") or "")[:55]
            console.print(f"  [cyan]{t['key']}[/cyan]  {_format_time_spent(t['seconds'])}  {tsum}")


@app.command("jira-report")
def jira_report_cmd(
    kind: str = typer.Argument(..., help=f"Report kind: {' | '.join(_REPORT_KINDS)}"),
    project: Optional[str] = typer.Option(None, "-p", "--project", help="Scope to one project key"),
    mine: bool = typer.Option(True, "--mine/--all", help="Scope to current user (JIRA_EMAIL)"),
    days: int = typer.Option(7, "--days", help="Window size for time/stale reports"),
    field: str = typer.Option("components", "--field", help="For 'scope': components | labels | fix_versions"),
    open_only: bool = typer.Option(True, "--open/--all-statuses", help="Restrict to open tickets where applicable"),
    limit: int = typer.Option(50, "--limit", "-n", help="Result cap (epics, scope)"),
):
    """
    Analytical reports on the local Jira cache.

    Kinds:
        digest   — workload overview: counts, priorities, recent activity, time
        daily    — recommended focus: in-progress, high-priority queue, stale, blocked
        epics    — group by epic_key with completion % and time spent
        scope    — distribution by components / labels / fix_versions
        stale    — tickets idle ≥ --days
        worklog  — time spent in the last --days

    Examples:
        smart-log jira-report digest
        smart-log jira-report daily
        smart-log jira-report epics --project GBI
        smart-log jira-report scope --field labels
        smart-log jira-report stale --days 21
        smart-log jira-report worklog --days 14
    """
    import jira_reports as _reports

    if kind not in _REPORT_KINDS:
        console.print(f"[red]Unknown kind '{kind}'. Valid: {', '.join(_REPORT_KINDS)}[/red]")
        raise typer.Exit(2)

    email = _current_user_email() if mine else None

    try:
        if kind == "digest":
            data = _reports.workload_digest(assignee_email=email, project=project, recent_days=days)
            _render_digest(data)
        elif kind == "daily":
            if not email:
                console.print("[red]--mine requires JIRA_EMAIL; pass --all for project-wide daily plan with --project.[/red]")
                raise typer.Exit(2)
            data = _reports.daily_plan(assignee_email=email, project=project)
            _render_daily(data)
        elif kind == "epics":
            rows = _reports.epic_rollup(
                assignee_email=email, project=project, open_only=open_only, limit=limit,
            )
            _render_epics(rows)
        elif kind == "scope":
            rows = _reports.scope_distribution(
                field=field, project=project, assignee_email=email, open_only=open_only, limit=limit,
            )
            _render_scope(rows, field)
        elif kind == "stale":
            rows = _reports.stale_tickets(days=days, project=project, open_only=open_only, limit=limit)
            _render_stale(rows)
        elif kind == "worklog":
            from datetime import datetime, timedelta
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            start = (today - timedelta(days=days)).strftime("%Y-%m-%d")
            end = (today + timedelta(days=1)).strftime("%Y-%m-%d")
            summary = _reports.worklog_summary(author_email=email, start=start, end=end)
            _render_worklog(summary, f"last {days} days")
    except Exception as e:
        console.print(f"[red]Report failed: {e}[/red]")
        raise typer.Exit(1)


def cli() -> None:
    """Entry point for the `smart-log` console script (must run argv preprocessing)."""
    _prepend_smart_if_needed()
    app()


if __name__ == "__main__":
    cli()