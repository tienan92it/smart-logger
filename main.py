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

# Load Config
load_dotenv()
console = Console()
app = typer.Typer()

@app.callback()
def main():
    """Smart Logger - Log your work to Jira and Notion with AI."""
    pass

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
    return json.loads(clean_json)


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
    console.print(f"[bold blue]🤖 AI Agent is parsing:[/bold blue] '{task}'...")
    
    # 1. AI Parsing
    parsed_data = ai_parse_log(task)
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
            console.print(f"[bold green]✔ Logged to Jira: {issue_key}[/bold green]")
            jira_logged = True
        except Exception as e:
            console.print(f"[yellow]⚠ Jira skipped: {e}[/yellow]")
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