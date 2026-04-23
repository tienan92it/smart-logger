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
from main import get_jira_client, is_valid_jira_key, build_jql_from_filters
from notion_form import submit_notion_form, NotionAuthError, NotionFormError
from memory_bank import load_memory, save_memory, add_issue

# Load env vars
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("smart-logger-mcp")

# Create MCP server
mcp = FastMCP("Smart Logger")

def _format_tasks_markdown(issues) -> str:
    """Format Jira issues as a Markdown table."""
    if not issues:
        return "No tasks found."
    
    md = "| Key | Summary | Status | Priority |\n"
    md += "|---|---|---|---|\n"
    for issue in issues:
        priority = issue.fields.priority.name if hasattr(issue.fields, 'priority') and issue.fields.priority else "-"
        # Escape pipes in summary
        summary = issue.fields.summary.replace("|", "\\|")
        md += f"| {issue.key} | {summary} | {str(issue.fields.status)} | {priority} |\n"
    
    return md

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
        
        # Try to log to Jira
        issue_title = ""
        jira_logged = False
        
        if is_valid_jira_key(issue_key):
            try:
                jira = get_jira_client()
                issue = jira.issue(issue_key)
                issue_title = issue.fields.summary
                jira.add_worklog(issue=issue, timeSpent=time_jira, comment=description)
                output.append(f"Logged to Jira: {issue_key}")
                jira_logged = True
                
                # Update memory
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
        try:
            jira = get_jira_client()
            
            if "jql" in query_plan:
                jql = query_plan["jql"]
            else:
                filters = query_plan.get("filters", query_plan)
                jql = build_jql_from_filters(filters or {})
            
            issues = jira.search_issues(jql, maxResults=50)
            
            if not issues:
                return OrchestratorResult(success=True, intent=Intent.QUERY_TASKS, message="No tasks found.")
            
            md_table = _format_tasks_markdown(issues)
            return OrchestratorResult(success=True, intent=Intent.QUERY_TASKS, message=f"Found {len(issues)} tasks:\n\n{md_table}")
            
        except Exception as e:
            return OrchestratorResult(success=False, intent=Intent.QUERY_TASKS, message=str(e))

    def task_detail_handler(issue_key: str) -> OrchestratorResult:
        try:
            jira = get_jira_client()
            issue = jira.issue(issue_key)
            
            # Update memory
            memory = load_memory()
            memory = add_issue(memory, issue_key, issue.fields.summary)
            save_memory(memory)
            
            details = f"**{issue.key}: {issue.fields.summary}**\n"
            details += f"- Status: {issue.fields.status}\n"
            details += f"- Priority: {issue.fields.priority.name if hasattr(issue.fields, 'priority') and issue.fields.priority else 'None'}\n"
            details += f"- Type: {issue.fields.issuetype.name}\n"
            
            if issue.fields.description:
                details += f"\n**Description:**\n{issue.fields.description[:1000]}"
            
            return OrchestratorResult(success=True, intent=Intent.TASK_DETAIL, message=details)
            
        except Exception as e:
            return OrchestratorResult(success=False, intent=Intent.TASK_DETAIL, message=str(e))

    def work_summary_handler(period: str, project_filter: str = None) -> OrchestratorResult:
        # Reuse the logic from main.py but tailored for string output
        # For brevity, I'll just return a placeholder or simple implementation
        # implementing full logic is complex due to dependencies on 'rich' in main.py
        # I'll rely on a simplified version or just say "Summary not fully implemented in MCP yet"
        # Or better, copy the logic if essential.
        # Let's try to implement a simple version.
        from datetime import datetime, timedelta
        
        try:
            jira = get_jira_client()
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            
            # Simple period handling
            if period == "today":
                start_date = today
                end_date = today + timedelta(days=1)
            elif period == "yesterday":
                start_date = today - timedelta(days=1)
                end_date = today
            else: # last week default
                start_date = today - timedelta(days=7)
                end_date = today + timedelta(days=1)
                
            start_str = start_date.strftime("%Y-%m-%d")
            end_str = end_date.strftime("%Y-%m-%d")
            
            jql = f'worklogDate >= "{start_str}" AND worklogDate < "{end_str}" AND worklogAuthor = currentUser() ORDER BY updated DESC'
            issues = jira.search_issues(jql, maxResults=50)
            
            if not issues:
                return OrchestratorResult(success=True, intent=Intent.WORK_SUMMARY, message=f"No worklogs found from {start_str} to {end_str}")
                
            # We need to iterate worklogs to sum up time, which is slow.
            # For now, just return the list of issues touched.
            msg = f"Issues worked on from {start_str} to {end_str}:\n" + _format_tasks_markdown(issues)
            return OrchestratorResult(success=True, intent=Intent.WORK_SUMMARY, message=msg)
            
        except Exception as e:
            return OrchestratorResult(success=False, intent=Intent.WORK_SUMMARY, message=str(e))

    # Run orchestrator
    result = orchestrate(
        user_input=instruction,
        log_work_handler=log_work_handler,
        query_tasks_handler=query_tasks_handler,
        task_detail_handler=task_detail_handler,
        work_summary_handler=work_summary_handler,
    )
    
    return result.message

if __name__ == "__main__":
    mcp.run()
