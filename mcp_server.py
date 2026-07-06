import os
import json
import logging
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Import from existing modules
# We need to make sure we can import these. 
# Since this script is in the same directory, it should work.
from ai_orchestrator import orchestrate, Intent, OrchestratorResult
from main import (
    get_jira_client,
    is_valid_jira_key,
    _resolve_filters,
    _ensure_ticket_cached,
    _refresh_ticket_cache,
    _current_user_email,
)
from notion_form import submit_notion_form, NotionAuthError, NotionFormError
from memory_bank import load_memory, save_memory, add_issue
import jira_store
import jira_reports

# Load env vars
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("smart-logger-mcp")

# Create MCP server
mcp = FastMCP("Smart Logger")

def _format_tasks_markdown(rows: list[dict]) -> str:
    """Format cached ticket rows as a Markdown table for the agent."""
    if not rows:
        return "No tasks found."

    md = "| Key | Summary | Status | Priority |\n"
    md += "|---|---|---|---|\n"
    for row in rows:
        summary = (row.get("summary") or "").replace("|", "\\|")
        md += f"| {row.get('key') or '?'} | {summary} | {row.get('status') or '-'} | {row.get('priority') or '-'} |\n"

    return md


def _format_seconds(seconds: int) -> str:
    hours = (seconds or 0) // 3600
    minutes = ((seconds or 0) % 3600) // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    if minutes:
        return f"{minutes}m"
    return "-"

@mcp.tool()
def smart_log(instruction: str, project: Optional[str] = None) -> str:
    """
    Log work, query tasks, or get details using natural language.
    
    Args:
        instruction: Natural language instruction (e.g. "2h on GBI-123", "my tasks", "details GBI-123")
        project: Optional project code (e.g. "DF", "GBI") for logging work.
    """
    
    # Use default project from env if not specified
    if not project:
        project = os.getenv("NOTION_PROJECT_DEFAULT_NAME", "")

    # Define handlers that return strings/results instead of printing to console
    
    def log_work_handler(log_data: dict) -> OrchestratorResult:
        issue_key = log_data.get('key', '')
        time_jira = log_data.get('time_jira', '')
        time_hours = log_data.get('time_hours', 0)
        description = log_data.get('desc', '')
        task_type = log_data.get('task_type', 'Development')

        output = []
        output.append(f"Parsed: {issue_key or 'No ticket'} | {time_jira} ({time_hours}h) | {task_type} | {description}")

        # Title comes from the local cache (single-ticket sync on miss).
        # Only the worklog write hits the Jira API.
        issue_title = ""
        jira_logged = False

        if is_valid_jira_key(issue_key):
            cached = _ensure_ticket_cached(issue_key)
            if cached:
                issue_title = cached.get("summary") or ""
            try:
                jira = get_jira_client()
                jira.add_worklog(issue=issue_key, timeSpent=time_jira, comment=description)
                output.append(f"Logged to Jira: {issue_key}")
                jira_logged = True

                if _refresh_ticket_cache(jira, issue_key):
                    output.append(f"Local cache refreshed for {issue_key}")

                memory = load_memory()
                memory = add_issue(memory, issue_key, issue_title)
                save_memory(memory)
            except Exception as e:
                output.append(f"Jira skipped: {e}")
        else:
            output.append("No Jira ticket, skipping Jira.")
        
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
            output.append("Synced to Notion!")
            
        except NotionAuthError as e:
            msg = f"Notion Auth Error: {e}. Run 'python main.py notion-login' in terminal to re-authenticate."
            output.append(msg)
            return OrchestratorResult(success=False, intent=Intent.LOG_WORK, message="\n".join(output))
        except NotionFormError as e:
            msg = f"Notion Error: {e}"
            output.append(msg)
            return OrchestratorResult(success=False, intent=Intent.LOG_WORK, message="\n".join(output))
        
        return OrchestratorResult(success=True, intent=Intent.LOG_WORK, message="\n".join(output))

    def query_tasks_handler(query_plan: dict) -> OrchestratorResult:
        """Read from the local cache; no Jira API calls."""
        try:
            raw_filters = query_plan.get("filters") or {}
            filters = _resolve_filters(raw_filters)
            with jira_store.open_db() as conn:
                rows = jira_store.query_tickets(conn, **filters)
            if not rows:
                return OrchestratorResult(
                    success=True, intent=Intent.QUERY_TASKS,
                    message="No tasks in local cache. Run `jira_sync_local` first.",
                )
            md = _format_tasks_markdown(rows)
            return OrchestratorResult(
                success=True, intent=Intent.QUERY_TASKS,
                message=f"Found {len(rows)} tickets (local cache):\n\n{md}",
            )
        except Exception as e:
            return OrchestratorResult(success=False, intent=Intent.QUERY_TASKS, message=str(e))

    def task_detail_handler(issue_key: str) -> OrchestratorResult:
        """Read from cache; auto-sync the single ticket if it's missing."""
        try:
            ticket = _ensure_ticket_cached(issue_key)
            if not ticket:
                return OrchestratorResult(
                    success=False, intent=Intent.TASK_DETAIL,
                    message=f"Ticket {issue_key} not found in local cache or Jira.",
                )

            memory = load_memory()
            memory = add_issue(memory, ticket["key"], ticket.get("summary") or "")
            save_memory(memory)

            with jira_store.open_db() as conn:
                links = jira_store.get_links(conn, ticket["key"])
                comments = jira_store.get_comments(conn, ticket["key"])

            details = f"**{ticket['key']}: {ticket.get('summary') or ''}**\n"
            details += f"- Status: {ticket.get('status') or '-'}\n"
            details += f"- Priority: {ticket.get('priority') or '-'}\n"
            details += f"- Type: {ticket.get('issue_type') or '-'}\n"
            details += f"- Assignee: {ticket.get('assignee') or '-'}\n"
            if links:
                details += "- Links: " + ", ".join(
                    f"{l.get('label') or l['link_type']}→{l['target_key']}" for l in links[:6]
                ) + "\n"
            if ticket.get("description"):
                desc = ticket["description"]
                if len(desc) > 1000:
                    desc = desc[:1000] + "..."
                details += f"\n**Description:**\n{desc}"
            if comments:
                details += f"\n\n_Cached comments: {len(comments)}_"

            return OrchestratorResult(success=True, intent=Intent.TASK_DETAIL, message=details)
        except Exception as e:
            return OrchestratorResult(success=False, intent=Intent.TASK_DETAIL, message=str(e))

    def work_summary_handler(period: Optional[str], project_filter: Optional[str] = None) -> OrchestratorResult:
        """Aggregate **local** worklog data; no Jira API."""
        from datetime import datetime, timedelta

        period = (period or "").strip().lower() or "last_week"

        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
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

        start_str = start.strftime("%Y-%m-%d")
        end_str = end.strftime("%Y-%m-%d")

        try:
            summary = jira_reports.worklog_summary(
                author_email=_current_user_email() or None,
                start=start_str,
                end=end_str,
            )
        except Exception as e:
            return OrchestratorResult(success=False, intent=Intent.WORK_SUMMARY, message=str(e))

        if project_filter:
            pf = project_filter.upper()
            summary["by_ticket"] = [t for t in summary["by_ticket"] if (t.get("key") or "").startswith(pf + "-")]
            summary["by_project"] = {k: v for k, v in summary["by_project"].items() if k == pf}
            summary["total_seconds"] = sum(summary["by_project"].values())

        if summary["total_seconds"] == 0:
            return OrchestratorResult(
                success=True, intent=Intent.WORK_SUMMARY,
                message=f"No local worklogs from {start_str} to {end_str}. Run `jira_sync_local` to refresh.",
            )

        out = [
            f"**Work summary {start_str} → {end_str}**",
            f"Total: {_format_seconds(summary['total_seconds'])} ({summary['total_seconds']/3600:.1f}h)",
        ]
        if summary["by_date"]:
            out.append("\n**By date:**")
            for d, sec in summary["by_date"].items():
                out.append(f"- {d}: {_format_seconds(sec)}")
        if summary["by_project"]:
            out.append("\n**By project:**")
            for proj, sec in summary["by_project"].items():
                out.append(f"- {proj}: {_format_seconds(sec)}")
        if summary["by_ticket"]:
            out.append("\n**By ticket (top 10):**")
            for t in summary["by_ticket"][:10]:
                tsum = (t.get("summary") or "")[:60]
                out.append(f"- {t['key']}: {_format_seconds(t['seconds'])} — {tsum}")

        return OrchestratorResult(success=True, intent=Intent.WORK_SUMMARY, message="\n".join(out))

    # Run orchestrator
    result = orchestrate(
        user_input=instruction,
        log_work_handler=log_work_handler,
        query_tasks_handler=query_tasks_handler,
        task_detail_handler=task_detail_handler,
        work_summary_handler=work_summary_handler,
    )
    
    return result.message

# ---------------------------------------------------------------------------
# Local Jira cache tools (agent-driven sync + read)
# ---------------------------------------------------------------------------

import jira_sync


def _ticket_to_markdown(ticket: dict, comments: list, links: list, worklogs: list) -> str:
    """Render a cached ticket as a compact Markdown block for the agent."""
    lines = [f"## {ticket['key']}: {ticket.get('summary') or ''}"]
    meta = [
        f"**Status:** {ticket.get('status') or '-'}",
        f"**Priority:** {ticket.get('priority') or '-'}",
        f"**Type:** {ticket.get('issue_type') or '-'}",
        f"**Assignee:** {ticket.get('assignee') or '-'}",
        f"**Reporter:** {ticket.get('reporter') or '-'}",
        f"**Updated:** {ticket.get('updated') or '-'}",
    ]
    if ticket.get("parent_key"):
        meta.append(f"**Parent:** {ticket['parent_key']}")
    if ticket.get("epic_key"):
        meta.append(f"**Epic:** {ticket['epic_key']}")
    if ticket.get("sprint"):
        sprint = ticket["sprint"]
        meta.append(f"**Sprint:** {sprint.get('name') if isinstance(sprint, dict) else sprint}")
    if ticket.get("labels"):
        meta.append(f"**Labels:** {', '.join(ticket['labels'])}")
    lines.append(" · ".join(meta))

    if ticket.get("description"):
        desc = ticket["description"]
        if len(desc) > 1500:
            desc = desc[:1500] + "..."
        lines.append(f"\n**Description:**\n{desc}")

    if links:
        lines.append("\n**Relationships:**")
        for ln in links:
            lines.append(f"- {ln['direction']} {ln.get('label') or ln['link_type']}: {ln['target_key']}")

    if comments:
        lines.append(f"\n**Comments ({len(comments)}):**")
        for c in comments[-5:]:
            body = (c.get("body") or "").strip()
            if len(body) > 400:
                body = body[:400] + "..."
            lines.append(f"- _{c.get('author') or '?'} ({(c.get('created') or '')[:19]}):_ {body}")

    if worklogs:
        total = sum(w.get("time_spent_seconds") or 0 for w in worklogs)
        hours = total / 3600
        lines.append(f"\n**Worklogs:** {len(worklogs)} entries, total {hours:.1f}h")

    return "\n".join(lines)


@mcp.tool()
def jira_sync_local(
    jql: Optional[str] = None,
    project: Optional[str] = None,
    since: Optional[str] = None,
    key: Optional[str] = None,
    only_mine: bool = True,
    full: bool = False,
    max_tickets: Optional[int] = None,
    fetch_worklogs: bool = True,
) -> str:
    """
    Sync Jira tickets into the local cache (~/.smart-logger/jira_cache.db).

    Pulls each matched ticket's description, comments, issue links, worklogs,
    and attachment metadata. Subsequent calls update existing rows in place.

    Args:
        jql: Custom JQL. If provided, overrides project/since/only_mine.
        project: Project key to scope the sync (e.g. "GBI").
        since: Date string (YYYY-MM-DD); only tickets updated >= this date.
        key: Sync a single ticket (e.g. "GBI-645").
        only_mine: Restrict to assignee/reporter = currentUser() (default true).
        full: Ignore the default 180-day window.
        max_tickets: Hard cap on tickets fetched.
        fetch_worklogs: Pull worklogs per ticket (slower; default true).
    """
    try:
        jira = get_jira_client()
    except Exception as e:
        return f"Jira connection error: {e}"

    try:
        if key:
            result = jira_sync.sync_single(jira, key, fetch_worklogs=fetch_worklogs)
        else:
            window_days = 0 if full else 180
            effective_jql = jql or jira_sync.build_default_jql(
                project=project, since=since, only_mine=only_mine, window_days=window_days,
            )
            result = jira_sync.sync(
                jira,
                jql=effective_jql,
                max_tickets=max_tickets,
                fetch_worklogs=fetch_worklogs,
            )
    except Exception as e:
        return f"Sync failed: {e}"

    summary = (
        f"Synced {result.tickets} tickets, {result.comments} comments, "
        f"{result.links} links, {result.worklogs} worklogs, {result.attachments} attachments.\n"
        f"JQL: {result.jql}"
    )
    if result.errors:
        summary += f"\nErrors: {len(result.errors)} (first: {result.errors[0]})"
    return summary


@mcp.tool()
def jira_get_ticket_local(
    key: str,
    include_comments: bool = True,
    include_links: bool = True,
    include_worklogs: bool = False,
) -> str:
    """
    Fetch a ticket from the local cache as Markdown.

    Returns "(not in cache)" if the ticket has never been synced. Run
    `jira_sync_local(key="...")` first in that case.
    """
    with jira_store.open_db() as conn:
        ticket = jira_store.get_ticket(conn, key)
        if not ticket:
            return f"{key.upper()} not in local cache. Run jira_sync_local with key='{key.upper()}'."
        comments = jira_store.get_comments(conn, key) if include_comments else []
        links = jira_store.get_links(conn, key) if include_links else []
        worklogs = jira_store.get_worklogs(conn, key) if include_worklogs else []
    return _ticket_to_markdown(ticket, comments, links, worklogs)


@mcp.tool()
def jira_search_local(query: str, limit: int = 15) -> str:
    """
    Full-text search across cached summaries, descriptions, and comments.

    Uses SQLite FTS5; supports phrases, AND/OR, NEAR/N, prefix*.
    """
    with jira_store.open_db() as conn:
        try:
            results = jira_store.search_tickets(conn, query, limit=limit)
        except Exception as e:
            return f"Search error: {e}"

    if not results:
        return "No matches in local cache."

    lines = ["| Key | Summary | Status | Match |", "|---|---|---|---|"]
    for r in results:
        summary = (r.get("summary") or "").replace("|", "\\|")
        snippet = (r.get("snippet") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {r['key']} | {summary} | {r.get('status') or '-'} | {snippet} |")
    return "\n".join(lines)


@mcp.tool()
def jira_local_stats(project: Optional[str] = None, stale_days: int = 14) -> str:
    """
    Summary stats on the local Jira cache: totals, status/project/assignee
    breakdowns, and stale tickets (no movement in `stale_days`).
    """
    with jira_store.open_db() as conn:
        stats = jira_store.db_stats(conn)
    out = [
        f"**Local cache:** {stats['tickets']} tickets · {stats['comments']} comments · "
        f"{stats['links']} links · {stats['worklogs']} worklogs · {stats['attachments']} attachments",
        f"**Path:** `{stats['db_path']}`",
    ]
    if stats["last_sync"]:
        ls = stats["last_sync"]
        out.append(f"**Last sync:** {ls.get('finished_at') or ls.get('started_at')} (JQL: `{(ls.get('jql') or '')[:80]}`)")

    by_status = jira_reports.counts_by_status(project=project)
    if by_status:
        out.append("\n**By status:**")
        for r in by_status:
            out.append(f"- {r['status']}: {r['n']}")

    by_assignee = jira_reports.counts_by_assignee(project=project, limit=10)
    if by_assignee:
        out.append("\n**Top assignees:**")
        for r in by_assignee:
            out.append(f"- {r['assignee']}: {r['n']}")

    stale = jira_reports.stale_tickets(days=stale_days, project=project, limit=10)
    if stale:
        out.append(f"\n**Stale (≥ {stale_days}d):**")
        for t in stale:
            out.append(f"- {t['key']} ({t.get('status') or '-'}) — {(t.get('summary') or '')[:60]}")

    return "\n".join(out)


def _format_seconds_h(seconds: int) -> str:
    return f"{(seconds or 0) / 3600:.1f}h"


@mcp.tool()
def jira_report_local(
    kind: str,
    project: Optional[str] = None,
    mine: bool = True,
    days: int = 7,
    field: str = "components",
    open_only: bool = True,
    limit: int = 50,
) -> str:
    """
    Analytical report against the local Jira cache. All reads are offline.

    Args:
        kind: One of "digest", "daily", "epics", "scope", "stale", "worklog".
            - digest   — workload overview: counts, priorities, recent activity, time invested.
            - daily    — focus list: in-progress, high-priority queue, stale in-progress, blocked.
            - epics    — group by epic_key, with completion % and time invested.
            - scope    — count tickets by component/label/fix_version (see `field`).
            - stale    — tickets idle >= `days`.
            - worklog  — time spent in the last `days`.
        project: Optional project key scope.
        mine: True scopes to JIRA_EMAIL; set False for project-wide views.
        days: Window for time-based reports.
        field: For `scope`: "components", "labels", or "fix_versions".
        open_only: Where applicable, restrict to non-Done tickets.
        limit: Result cap.
    """
    email = _current_user_email() if mine else None

    try:
        if kind == "digest":
            d = jira_reports.workload_digest(assignee_email=email, project=project, recent_days=days)
            lines = [
                f"**Workload digest** — {d['total_open']} open tickets",
                f"Time last {d['recent_window_days']}d: {_format_seconds_h(d['time_spent_recent_seconds'])} "
                f"(prev: {_format_seconds_h(d['time_spent_prev_seconds'])})",
                f"Stale (idle ≥ 14d): {d['stale_count']}",
            ]
            if d["by_status_category"]:
                lines.append("\n**By status:**")
                lines.extend(f"- {r.get('status') or '-'}: {r['n']}" for r in d["by_status_category"])
            if d["by_priority"]:
                lines.append("\n**By priority:**")
                lines.extend(f"- {r.get('priority') or '-'}: {r['n']}" for r in d["by_priority"])
            if d["by_project"]:
                lines.append("\n**By project:**")
                lines.extend(f"- {r['project']}: {r['n']}" for r in d["by_project"])
            if d["top_priorities"]:
                lines.append("\n**Top priority:**")
                for t in d["top_priorities"]:
                    lines.append(f"- {t['priority']} | {t['key']} | {t.get('status') or '-'} — {(t.get('summary') or '')[:60]}")
            if d["recent_activity"]:
                lines.append("\n**Recently active:**")
                for t in d["recent_activity"]:
                    lines.append(f"- {t['key']} ({(t.get('updated') or '')[:10]}) — {(t.get('summary') or '')[:60]}")
            return "\n".join(lines)

        if kind == "daily":
            if not email:
                return "daily plan requires `mine=true` and a configured JIRA_EMAIL."
            d = jira_reports.daily_plan(assignee_email=email, project=project)
            sections = []

            def section(title: str, items: list) -> str:
                if not items:
                    return f"_{title}: none_"
                out = [f"**{title}** ({len(items)})"]
                for t in items[:10]:
                    out.append(
                        f"- {t['key']} | {t.get('priority') or '-'} | {t.get('status') or '-'} — "
                        f"{(t.get('summary') or '')[:60]} _(updated {(t.get('updated') or '')[:10]})_"
                    )
                if len(items) > 10:
                    out.append(f"- _... {len(items) - 10} more_")
                return "\n".join(out)

            sections.append("**Daily plan**")
            sections.append(section("Focus now (In Progress)", d["in_progress"]))
            sections.append(section("Next up (High/Highest, To Do)", d["next_up"]))
            sections.append(section("Stale In Progress (unblock these)", d["stale_in_progress"]))
            sections.append(section("Blocked", d["blocked"]))
            return "\n\n".join(sections)

        if kind == "epics":
            rows = jira_reports.epic_rollup(
                assignee_email=email, project=project, open_only=open_only, limit=limit,
            )
            if not rows:
                return "No epics in scope."
            out = ["| Epic | Summary | Status | Tickets | Done | % | Time | Updated |",
                   "|---|---|---|---|---|---|---|---|"]
            for r in rows:
                out.append(
                    f"| {r['epic_key']} | {(r.get('epic_summary') or '')[:50]} | "
                    f"{r.get('epic_status') or '-'} | {r['total']} | {r['done']} | "
                    f"{r['percent_complete']:.0f}% | {_format_seconds_h(r.get('total_seconds') or 0)} | "
                    f"{(r.get('last_updated') or '')[:10]} |"
                )
            return "\n".join(out)

        if kind == "scope":
            rows = jira_reports.scope_distribution(
                field=field, project=project, assignee_email=email, open_only=open_only, limit=limit,
            )
            if not rows:
                return f"No {field} found in scope."
            out = [f"**Scope by {field}:**"]
            out.extend(f"- {r['value']}: {r['count']}" for r in rows)
            return "\n".join(out)

        if kind == "stale":
            rows = jira_reports.stale_tickets(days=days, project=project, open_only=open_only, limit=limit)
            if not rows:
                return f"No tickets idle ≥ {days}d."
            out = [f"**Stale tickets (≥ {days}d):**"]
            for t in rows:
                out.append(
                    f"- {t['key']} | {t.get('status') or '-'} | {t.get('priority') or '-'} — "
                    f"{(t.get('summary') or '')[:60]} _(updated {(t.get('updated') or '')[:10]})_"
                )
            return "\n".join(out)

        if kind == "worklog":
            from datetime import datetime, timedelta
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            start = (today - timedelta(days=days)).strftime("%Y-%m-%d")
            end = (today + timedelta(days=1)).strftime("%Y-%m-%d")
            s = jira_reports.worklog_summary(author_email=email, start=start, end=end)
            if s["total_seconds"] == 0:
                return f"No worklogs in the last {days} days."
            out = [
                f"**Worklog last {days}d:** {_format_seconds_h(s['total_seconds'])}",
            ]
            if s["by_date"]:
                out.append("\n**By date:**")
                out.extend(f"- {d}: {_format_seconds_h(sec)}" for d, sec in s["by_date"].items())
            if s["by_project"]:
                out.append("\n**By project:**")
                out.extend(f"- {p}: {_format_seconds_h(sec)}" for p, sec in s["by_project"].items())
            if s["by_ticket"]:
                out.append("\n**Top tickets:**")
                for t in s["by_ticket"][:10]:
                    out.append(f"- {t['key']}: {_format_seconds_h(t['seconds'])} — {(t.get('summary') or '')[:60]}")
            return "\n".join(out)

        return f"Unknown kind '{kind}'. Valid: digest, daily, epics, scope, stale, worklog."
    except Exception as e:
        return f"Report failed: {e}"


@mcp.tool()
def jira_relationships_local(key: str, depth: int = 1) -> str:
    """
    Return the local relationship graph for a ticket as Markdown.

    depth=1 returns direct neighbors; depth=2 includes neighbors-of-neighbors.
    """
    graph = jira_reports.relationship_graph(key, depth=depth)
    lines = [f"**Root:** {graph['root']}"]
    nodes_by_key = {n["key"]: n for n in graph["nodes"]}
    if graph["edges"]:
        lines.append("\n**Edges:**")
        for e in graph["edges"]:
            other = e["target_key"] if e["source_key"] == graph["root"] else e["source_key"]
            other_summary = (nodes_by_key.get(other) or {}).get("summary") or ""
            lines.append(
                f"- {e['source_key']} —[{e.get('label') or e['link_type']} ({e['direction']})]→ "
                f"{e['target_key']}  {other_summary[:50]}"
            )
    else:
        lines.append("\n_No links in local cache for this ticket._")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
