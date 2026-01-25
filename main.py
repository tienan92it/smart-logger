import os
import json
import typer
from rich.console import Console
from rich.table import Table
from typing import Optional
from dotenv import load_dotenv
from jira import JIRA
from google import genai

from notion_form import submit_notion_form, NotionFormError, NotionAuthError
from notion_auth import get_notion_credentials, clear_token, load_stored_token
from memory_bank import load_memory, save_memory, add_issue
from ai_orchestrator import orchestrate, Intent, OrchestratorResult

# Load Config
load_dotenv()
console = Console()
app = typer.Typer()


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


def _get_time_spent_map(jira, issues: list) -> dict:
    """Fetch time spent for multiple issues. Returns {issue_key: seconds}."""
    time_map = {}
    
    console.print("[dim]Fetching time spent data...[/dim]")
    
    for issue in issues:
        try:
            # Get aggregated time spent from issue (faster than fetching worklogs)
            time_spent = getattr(issue.fields, 'timespent', None) or 0
            time_map[issue.key] = time_spent
        except Exception:
            time_map[issue.key] = 0
    
    return time_map


def _display_tasks_table(issues: list, show_desc: bool = False, show_time: bool = False, jira=None) -> None:
    """Display tasks in a standard table format."""
    time_map = {}
    if show_time and jira:
        time_map = _get_time_spent_map(jira, issues)
    
    table = Table(title="My Jira Tasks", show_lines=True)
    table.add_column("Key", style="cyan", no_wrap=True)
    table.add_column("Summary", style="white")
    table.add_column("Status", style="magenta")
    table.add_column("Priority", style="yellow")
    if show_time:
        table.add_column("Time", style="green", justify="right")
    
    total_seconds = 0
    for issue in issues:
        priority = issue.fields.priority.name if issue.fields.priority else "-"
        summary = issue.fields.summary
        if len(summary) > 55 and not show_desc:
            summary = summary[:55] + "..."
        
        row = [
            issue.key,
            summary,
            str(issue.fields.status),
            priority
        ]
        
        if show_time:
            seconds = time_map.get(issue.key, 0)
            total_seconds += seconds
            row.append(_format_time_spent(seconds))
        
        table.add_row(*row)
    
    console.print(table)
    
    # Show totals
    footer = f"[dim]Showing {len(issues)} tasks[/dim]"
    if show_time and total_seconds > 0:
        total_hours = total_seconds / 3600
        footer += f" | [bold green]Total time: {_format_time_spent(total_seconds)} ({total_hours:.1f}h)[/bold green]"
    console.print(footer)


def _display_grouped_tasks(issues: list, group_by: str, show_desc: bool = False, show_time: bool = False, jira=None) -> None:
    """Display tasks grouped by project, status, or priority."""
    from collections import defaultdict
    
    # Fetch time data if needed
    time_map = {}
    if show_time and jira:
        time_map = _get_time_spent_map(jira, issues)
    
    # Group issues
    groups = defaultdict(list)
    
    for issue in issues:
        if group_by == "project":
            # Group by project prefix (e.g., GBI, KFS)
            key = issue.key.split("-")[0] if "-" in issue.key else "OTHER"
        elif group_by == "status":
            key = str(issue.fields.status)
        elif group_by == "priority":
            key = issue.fields.priority.name if issue.fields.priority else "None"
        elif group_by == "type":
            key = issue.fields.issuetype.name if issue.fields.issuetype else "Unknown"
        else:
            key = "All"
        
        groups[key].append(issue)
    
    # Sort groups by count (descending)
    sorted_groups = sorted(groups.items(), key=lambda x: len(x[1]), reverse=True)
    
    # Calculate time per group if showing time
    group_times = {}
    total_seconds = 0
    if show_time:
        for group_name, group_issues in sorted_groups:
            group_seconds = sum(time_map.get(issue.key, 0) for issue in group_issues)
            group_times[group_name] = group_seconds
            total_seconds += group_seconds
    
    # Display summary header
    total = len(issues)
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
    
    # Status color mapping
    status_colors = {
        "In Progress": "blue",
        "To Do": "white",
        "Done": "green",
        "Testing": "yellow",
        "Done UAT": "green",
        "Blocked": "red",
    }
    
    priority_colors = {
        "Highest": "red",
        "High": "yellow",
        "Medium": "white",
        "Low": "dim",
        "Lowest": "dim",
    }
    
    # Display each group
    for group_name, group_issues in sorted_groups:
        # Create table for this group
        group_time_str = ""
        if show_time:
            group_time_str = f" - {_format_time_spent(group_times.get(group_name, 0))}"
        
        table = Table(
            title=f"{group_name} ({len(group_issues)}){group_time_str}",
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
        
        for issue in group_issues:
            priority_name = issue.fields.priority.name if issue.fields.priority else "-"
            priority_color = priority_colors.get(priority_name, "white")
            status_name = str(issue.fields.status)
            status_color = status_colors.get(status_name, "white")
            
            summary = issue.fields.summary
            if len(summary) > 50 and not show_desc:
                summary = summary[:50] + "..."
            
            row = [
                issue.key,
                summary,
                f"[{status_color}]{status_name}[/{status_color}]",
                f"[{priority_color}]{priority_name}[/{priority_color}]"
            ]
            
            if show_time:
                row.append(_format_time_spent(time_map.get(issue.key, 0)))
            
            table.add_row(*row)
        
        console.print(table)
        console.print()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    input_text: Optional[str] = typer.Argument(None, help="Natural language input (e.g., '2h on GBI-123' or 'my tasks')"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Notion project name for logging"),
):
    """
    Smart Logger - AI-powered work logging to Jira and Notion.
    
    Just describe what you want in natural language:
    
    Examples:
        python main.py "2h on GBI-123 implementing feature"
        python main.py "my tasks"
        python main.py "show in progress bugs"
        python main.py "what is GBI-123"
    """
    if ctx.invoked_subcommand is None and input_text:
        # Smart mode - use AI orchestrator
        _smart_handler(input_text, project)
    elif ctx.invoked_subcommand is None:
        # No input and no subcommand - show help
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
        
        # Try to log to Jira
        issue_title = ""
        jira_logged = False
        
        if is_valid_jira_key(issue_key):
            try:
                jira = get_jira_client()
                issue = jira.issue(issue_key)
                issue_title = issue.fields.summary
                jira.add_worklog(issue=issue, timeSpent=time_jira, comment=description)
                console.print(f"[bold green]Logged to Jira: {issue_key}[/bold green]")
                jira_logged = True
                
                # Update memory with issue details
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
    
    def query_tasks_handler(query_result: dict) -> OrchestratorResult:
        """Handle query_tasks intent with grouping and display options."""
        try:
            jira = get_jira_client()
            
            # Handle both old format (just filters) and new format (filters + display)
            if "filters" in query_result:
                filters = query_result.get("filters", {})
                display = query_result.get("display", {})
            else:
                # Old format - just filters
                filters = query_result
                display = {}
            
            jql = build_jql_from_filters(filters or {})
            console.print(f"[dim]JQL: {jql}[/dim]")
            
            # Request timespent field if showing time
            show_time = display.get("show_time_spent", False) if display else False
            fields = "summary,status,priority,issuetype"
            if show_time:
                fields += ",timespent"
            
            issues = jira.search_issues(jql, maxResults=50, fields=fields)
            
            if not issues:
                console.print("[yellow]No tasks found.[/yellow]")
                return OrchestratorResult(success=True, intent=Intent.QUERY_TASKS, message="No tasks found")
            
            group_by = display.get("group_by") if display else None
            show_desc = display.get("show_description", False) if display else False
            
            if group_by:
                # Grouped display
                _display_grouped_tasks(issues, group_by, show_desc, show_time, jira)
            else:
                # Standard table display
                _display_tasks_table(issues, show_desc, show_time, jira)
            
            return OrchestratorResult(success=True, intent=Intent.QUERY_TASKS, message=f"Found {len(issues)} tasks")
            
        except Exception as e:
            console.print(f"[red]Jira Error: {e}[/red]")
            return OrchestratorResult(success=False, intent=Intent.QUERY_TASKS, message=str(e))
    
    def task_detail_handler(issue_key: str) -> OrchestratorResult:
        """Handle task_detail intent."""
        try:
            jira = get_jira_client()
            issue = jira.issue(issue_key)
            
            # Update memory
            memory = load_memory()
            memory = add_issue(memory, issue_key, issue.fields.summary)
            save_memory(memory)
            
            # Display details
            console.print(f"\n[bold cyan]{issue.key}[/bold cyan]: {issue.fields.summary}")
            console.print(f"[dim]Status:[/dim] {issue.fields.status}")
            console.print(f"[dim]Priority:[/dim] {issue.fields.priority.name if issue.fields.priority else 'None'}")
            console.print(f"[dim]Type:[/dim] {issue.fields.issuetype.name}")
            
            if issue.fields.description:
                desc = issue.fields.description[:500]
                if len(issue.fields.description) > 500:
                    desc += "..."
                console.print(f"\n[dim]Description:[/dim]\n{desc}")
            
            return OrchestratorResult(success=True, intent=Intent.TASK_DETAIL, message="Task details retrieved")
            
        except Exception as e:
            console.print(f"[red]Jira Error: {e}[/red]")
            return OrchestratorResult(success=False, intent=Intent.TASK_DETAIL, message=str(e))
    
    def work_summary_handler(period: str, project_filter: str = None) -> OrchestratorResult:
        """Handle work_summary intent - show summary of worklogs."""
        from datetime import datetime, timedelta
        from collections import defaultdict
        
        try:
            jira = get_jira_client()
            
            # Calculate date range
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            
            if period == "today":
                start_date = today
                end_date = today + timedelta(days=1)
                period_label = "Today"
            elif period == "yesterday":
                start_date = today - timedelta(days=1)
                end_date = today
                period_label = "Yesterday"
            elif period == "this_week":
                start_date = today - timedelta(days=today.weekday())
                end_date = today + timedelta(days=1)
                period_label = "This Week"
            elif period == "last_week":
                start_date = today - timedelta(days=today.weekday() + 7)
                end_date = today - timedelta(days=today.weekday())
                period_label = "Last Week"
            elif period == "this_month":
                start_date = today.replace(day=1)
                end_date = today + timedelta(days=1)
                period_label = "This Month"
            elif period == "last_month":
                first_of_month = today.replace(day=1)
                start_date = (first_of_month - timedelta(days=1)).replace(day=1)
                end_date = first_of_month
                period_label = "Last Month"
            else:
                # Default to last week
                start_date = today - timedelta(days=7)
                end_date = today + timedelta(days=1)
                period_label = "Last 7 Days"
            
            start_str = start_date.strftime("%Y-%m-%d")
            end_str = end_date.strftime("%Y-%m-%d")
            
            console.print(f"[dim]Fetching worklogs from {start_str} to {end_str}...[/dim]")
            
            # Build JQL to find issues with worklogs in period
            jql = f'worklogDate >= "{start_str}" AND worklogDate < "{end_str}" AND worklogAuthor = currentUser()'
            if project_filter:
                jql += f' AND project = "{project_filter}"'
            jql += ' ORDER BY updated DESC'
            
            issues = jira.search_issues(jql, maxResults=100, fields="summary,project,timespent,worklog")
            
            if not issues:
                console.print(f"[yellow]No worklogs found for {period_label}.[/yellow]")
                return OrchestratorResult(success=True, intent=Intent.WORK_SUMMARY, message="No worklogs found")
            
            # Collect worklog data
            total_seconds = 0
            by_project = defaultdict(lambda: {"seconds": 0, "issues": []})
            by_date = defaultdict(int)
            
            current_user = os.getenv("JIRA_EMAIL", "").lower()
            
            for issue in issues:
                try:
                    worklogs = jira.worklogs(issue.key)
                    for wl in worklogs:
                        # Filter by author and date
                        wl_author = getattr(wl, 'author', None)
                        if wl_author:
                            author_email = getattr(wl_author, 'emailAddress', '').lower()
                            author_name = getattr(wl_author, 'displayName', '').lower()
                        else:
                            continue
                        
                        if current_user not in author_email and current_user not in author_name:
                            continue
                        
                        wl_started = getattr(wl, 'started', '')
                        if wl_started:
                            wl_date = datetime.strptime(wl_started[:10], "%Y-%m-%d")
                            if start_date <= wl_date < end_date:
                                seconds = getattr(wl, 'timeSpentSeconds', 0)
                                total_seconds += seconds
                                
                                project_key = issue.key.split("-")[0]
                                by_project[project_key]["seconds"] += seconds
                                if issue.key not in [i["key"] for i in by_project[project_key]["issues"]]:
                                    by_project[project_key]["issues"].append({
                                        "key": issue.key,
                                        "summary": issue.fields.summary,
                                        "seconds": 0
                                    })
                                # Add to issue
                                for i in by_project[project_key]["issues"]:
                                    if i["key"] == issue.key:
                                        i["seconds"] += seconds
                                
                                date_key = wl_date.strftime("%Y-%m-%d (%a)")
                                by_date[date_key] += seconds
                                
                except Exception:
                    continue
            
            if total_seconds == 0:
                console.print(f"[yellow]No worklogs found for {period_label}.[/yellow]")
                return OrchestratorResult(success=True, intent=Intent.WORK_SUMMARY, message="No worklogs found")
            
            # Display summary
            total_hours = total_seconds / 3600
            
            console.print(f"\n[bold cyan]Work Summary: {period_label}[/bold cyan]")
            console.print(f"[bold green]Total: {_format_time_spent(total_seconds)} ({total_hours:.1f} hours)[/bold green]\n")
            
            # By date
            if by_date:
                console.print("[bold]By Date:[/bold]")
                for date_key in sorted(by_date.keys()):
                    hours = by_date[date_key] / 3600
                    console.print(f"  {date_key}: {_format_time_spent(by_date[date_key])} ({hours:.1f}h)")
                console.print()
            
            # By project
            if by_project:
                console.print("[bold]By Project:[/bold]")
                for proj_key in sorted(by_project.keys(), key=lambda x: by_project[x]["seconds"], reverse=True):
                    proj_data = by_project[proj_key]
                    proj_hours = proj_data["seconds"] / 3600
                    console.print(f"\n  [cyan]{proj_key}[/cyan]: {_format_time_spent(proj_data['seconds'])} ({proj_hours:.1f}h)")
                    
                    # Show issues
                    sorted_issues = sorted(proj_data["issues"], key=lambda x: x["seconds"], reverse=True)
                    for issue_info in sorted_issues[:10]:  # Limit to top 10
                        summary = issue_info["summary"][:45] + "..." if len(issue_info["summary"]) > 45 else issue_info["summary"]
                        console.print(f"    {issue_info['key']}: {_format_time_spent(issue_info['seconds'])} - {summary}")
            
            return OrchestratorResult(success=True, intent=Intent.WORK_SUMMARY, message=f"Total: {total_hours:.1f} hours")
            
        except Exception as e:
            console.print(f"[red]Jira Error: {e}[/red]")
            return OrchestratorResult(success=False, intent=Intent.WORK_SUMMARY, message=str(e))
    
    # Run orchestrator
    result = orchestrate(
        user_input=input_text,
        log_work_handler=log_work_handler,
        query_tasks_handler=query_tasks_handler,
        task_detail_handler=task_detail_handler,
        work_summary_handler=work_summary_handler,
    )
    
    # Handle clarify and help intents
    if result.intent == Intent.HELP:
        console.print(result.message)
    elif result.intent == Intent.CLARIFY:
        console.print(f"[yellow]{result.message}[/yellow]")
    elif not result.success:
        console.print(f"[red]Error: {result.message}[/red]")


# --- SERVICES ---

def get_jira_client():
    return JIRA(
        server=os.getenv("JIRA_SERVER"),
        basic_auth=(os.getenv("JIRA_EMAIL"), os.getenv("JIRA_API_TOKEN"))
    )

def get_genai_client():
    return genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

def ai_parse_log(natural_input: str):
    """
    Uses AI to convert natural language into structured data.
    """
    client = get_genai_client()
    
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
    response = client.models.generate_content(
        model='gemini-2.0-flash',  # Fast & Free tier eligible
        contents=prompt
    )
    # Simple cleanup to ensure we get just the JSON
    clean_json = response.text.replace('```json', '').replace('```', '').strip()
    
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
    client = get_genai_client()
    
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
    response = client.models.generate_content(
        model='gemini-2.0-flash',
        contents=prompt
    )
    clean_json = response.text.replace('```json', '').replace('```', '').strip()
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

    # 2. Try to log to Jira (only if valid ticket key)
    issue_title = ""
    jira_logged = False
    
    if is_valid_jira_key(issue_key):
        try:
            jira = get_jira_client()
            console.print("[dim]Checking Jira ticket...[/dim]")
            issue = jira.issue(issue_key)
            issue_title = issue.fields.summary  # Get the actual ticket title
            jira.add_worklog(issue=issue, timeSpent=time_jira, comment=description)
            console.print(f"[bold green]Logged to Jira: {issue_key}[/bold green]")
            jira_logged = True
            
            # Save to memory for future context
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


@app.command()
def tasks(
    query: Optional[str] = typer.Argument(None, help="Natural language query (e.g., 'high priority bugs', 'in progress tasks')"),
    status: Optional[str] = typer.Option(None, "--status", "-s", help="Filter by status (e.g., 'To Do', 'In Progress')"),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum number of tasks to show"),
):
    """
    Show your Jira tasks. Use natural language or flags to filter.
    
    Examples:
        tasks                              # Show all your tasks
        tasks "in progress"                # AI-powered: tasks in progress
        tasks "high priority bugs"         # AI-powered: high priority bugs
        tasks "updated this week"          # AI-powered: recently updated
        tasks --status "In Progress"       # Manual filter by status
    """
    try:
        jira = get_jira_client()
        
        # Build JQL query
        if query:
            # Use AI to parse natural language
            console.print(f"[bold blue]🤖 AI Agent is parsing:[/bold blue] '{query}'...")
            filters = ai_parse_task_query(query)
            jql = build_jql_from_filters(filters)
            console.print(f"[dim]Generated JQL: {jql}[/dim]")
        elif status:
            jql = f'assignee = currentUser() AND status = "{status}" ORDER BY updated DESC'
        else:
            jql = "assignee = currentUser() ORDER BY updated DESC"
        
        console.print("[dim]Fetching your tasks from Jira...[/dim]")
        issues = jira.search_issues(jql, maxResults=limit)
        
        if not issues:
            console.print("[yellow]No tasks found.[/yellow]")
            return
        
        # Build table
        table = Table(title="📋 My Jira Tasks", show_lines=True)
        table.add_column("Key", style="cyan", no_wrap=True)
        table.add_column("Summary", style="white")
        table.add_column("Status", style="magenta")
        table.add_column("Priority", style="yellow")
        
        for issue in issues:
            priority = issue.fields.priority.name if issue.fields.priority else "-"
            table.add_row(
                issue.key,
                issue.fields.summary[:60] + "..." if len(issue.fields.summary) > 60 else issue.fields.summary,
                str(issue.fields.status),
                priority
            )
        
        console.print(table)
        console.print(f"[dim]Showing {len(issues)} of your tasks[/dim]")
        
    except Exception as e:
        console.print(f"[red]❌ Jira Error: {e}[/red]")


if __name__ == "__main__":
    app()