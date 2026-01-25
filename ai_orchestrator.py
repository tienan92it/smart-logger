"""
AI Orchestrator for Smart Logger

Central intelligence that:
1. Classifies user intent from natural language
2. Enriches prompts with memory context
3. Routes to appropriate action handlers
4. Updates memory after actions
"""

import os
import json
import re
from typing import Optional, Callable
from dataclasses import dataclass
from enum import Enum
from dotenv import load_dotenv
from google import genai
from rich.console import Console

from memory_bank import (
    load_memory,
    save_memory,
    add_issue,
    add_query,
    increment_stat,
    build_context_for_ai,
    get_task_type_hint,
)

load_dotenv()
console = Console()


class Intent(Enum):
    """Possible user intents."""
    LOG_WORK = "log_work"
    QUERY_TASKS = "query_tasks"
    TASK_DETAIL = "task_detail"
    WORK_SUMMARY = "work_summary"  # Summary/report of worklogs
    HELP = "help"
    CLARIFY = "clarify"


@dataclass
class ClassificationResult:
    """Result of intent classification."""
    intent: Intent
    confidence: float
    extracted_data: dict
    message: Optional[str] = None  # For clarify/help intents


def get_genai_client():
    """Get Google GenAI client."""
    return genai.Client(api_key=os.getenv("GEMINI_API_KEY"))


def classify_intent(user_input: str, context: str = "") -> ClassificationResult:
    """
    Use AI to classify user intent from natural language.
    
    Returns classification with:
    - intent: The detected intent type
    - confidence: How confident the AI is (0-1)
    - extracted_data: Relevant data extracted based on intent
    """
    client = get_genai_client()
    
    prompt = f"""
You are an intent classifier for a work logging tool. Analyze the user input and classify their intent.

{context}

User input: "{user_input}"

Classify into ONE of these intents:
1. "log_work" - User wants to LOG time spent on work. MUST have a time indicator (e.g., "2h", "30m", "1 hour", "45 minutes").
   Extract: time_jira (Jira format like "2h", "30m"), time_hours (decimal), issue_key (if mentioned), description
   
2. "query_tasks" - User wants to LIST or SEARCH their tasks/issues. Keywords: "my tasks", "show", "list", "find", "what are", status filters.
   Extract: filters (status, priority, project, etc.)
   
3. "task_detail" - User wants DETAILS about a SPECIFIC task. Usually mentions a specific issue key with "what is", "details", "info about".
   Extract: issue_key

4. "work_summary" - User wants a SUMMARY or REPORT of their LOGGED WORK/WORKLOGS. Keywords: "summary", "report", "how much did I work", "my work last week", "worklog summary", "time report".
   Extract: period (e.g., "last_week", "last_month", "this_week", "this_month", "today", "yesterday"), project (optional filter)
   
5. "help" - User is asking how to use the tool or what it can do.
   Extract: nothing
   
6. "clarify" - Input is ambiguous or doesn't fit other categories. Need more information.
   Extract: clarification_message (what to ask the user)

IMPORTANT RULES:
- If there's a time indicator (2h, 30m, 1 hour, etc.), it's almost always "log_work"
- "my tasks", "my issues", "show tasks" without time = "query_tasks"
- Just an issue key with "what is" or "details" = "task_detail"
- "summary", "report", "my work last week/month" = "work_summary" (NOT query_tasks!)
- If unsure between query_tasks and log_work, check for time indicator

Return ONLY valid JSON:
{{"intent": "...", "confidence": 0.0-1.0, "extracted_data": {{...}}, "message": "optional message for clarify/help"}}

Examples:
- "2h on GBI-123 fixing bugs" -> {{"intent": "log_work", "confidence": 0.95, "extracted_data": {{"time_jira": "2h", "time_hours": 2.0, "issue_key": "GBI-123", "description": "fixing bugs"}}, "message": null}}
- "my GBI tasks" -> {{"intent": "query_tasks", "confidence": 0.9, "extracted_data": {{"filters": {{"project": "GBI"}}}}, "message": null}}
- "show in progress tasks" -> {{"intent": "query_tasks", "confidence": 0.9, "extracted_data": {{"filters": {{"status": "In Progress"}}}}, "message": null}}
- "what is GBI-123" -> {{"intent": "task_detail", "confidence": 0.9, "extracted_data": {{"issue_key": "GBI-123"}}, "message": null}}
- "summary my work last week" -> {{"intent": "work_summary", "confidence": 0.95, "extracted_data": {{"period": "last_week", "project": null}}, "message": null}}
- "report my worklogs this month" -> {{"intent": "work_summary", "confidence": 0.95, "extracted_data": {{"period": "this_month", "project": null}}, "message": null}}
- "how much did I work on GBI last week" -> {{"intent": "work_summary", "confidence": 0.9, "extracted_data": {{"period": "last_week", "project": "GBI"}}, "message": null}}
- "help" -> {{"intent": "help", "confidence": 1.0, "extracted_data": {{}}, "message": null}}
- "GBI-123" -> {{"intent": "clarify", "confidence": 0.5, "extracted_data": {{"issue_key": "GBI-123"}}, "message": "Did you want to log time on GBI-123, see its details, or something else?"}}
"""
    
    response = client.models.generate_content(
        model='gemini-2.0-flash',
        contents=prompt
    )
    
    clean_json = response.text.replace('```json', '').replace('```', '').strip()
    
    try:
        result = json.loads(clean_json)
        return ClassificationResult(
            intent=Intent(result["intent"]),
            confidence=result.get("confidence", 0.8),
            extracted_data=result.get("extracted_data", {}),
            message=result.get("message"),
        )
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        # Fallback: try to detect intent from simple patterns
        return _fallback_classification(user_input)


def _fallback_classification(user_input: str) -> ClassificationResult:
    """
    Simple pattern-based fallback when AI classification fails.
    """
    text = user_input.lower().strip()
    
    # Check for time indicator -> log_work
    time_match = re.search(r'(\d+)\s*(h|hr|hour|m|min|minute)s?', text)
    if time_match:
        return ClassificationResult(
            intent=Intent.LOG_WORK,
            confidence=0.7,
            extracted_data={"raw_input": user_input},
            message=None,
        )
    
    # Check for work summary patterns (before query patterns)
    summary_patterns = ["summary", "report", "how much did i work", "my work last", "worklog", "time report"]
    for pattern in summary_patterns:
        if pattern in text:
            # Try to detect period
            period = "last_week"  # default
            if "today" in text:
                period = "today"
            elif "yesterday" in text:
                period = "yesterday"
            elif "this week" in text:
                period = "this_week"
            elif "this month" in text:
                period = "this_month"
            elif "last month" in text:
                period = "last_month"
            elif "last week" in text:
                period = "last_week"
            
            return ClassificationResult(
                intent=Intent.WORK_SUMMARY,
                confidence=0.7,
                extracted_data={"period": period, "project": None},
                message=None,
            )
    
    # Check for query patterns
    query_patterns = ["my task", "my issue", "show", "list", "find", "what are", "in progress", "to do", "blocked"]
    for pattern in query_patterns:
        if pattern in text:
            return ClassificationResult(
                intent=Intent.QUERY_TASKS,
                confidence=0.6,
                extracted_data={"raw_input": user_input},
                message=None,
            )
    
    # Check for help
    if text in ("help", "?", "how", "what can you do"):
        return ClassificationResult(
            intent=Intent.HELP,
            confidence=0.9,
            extracted_data={},
            message=None,
        )
    
    # Check for issue key pattern
    issue_match = re.search(r'\b([A-Z]+-\d+)\b', user_input, re.IGNORECASE)
    if issue_match:
        return ClassificationResult(
            intent=Intent.TASK_DETAIL,
            confidence=0.5,
            extracted_data={"issue_key": issue_match.group(1).upper()},
            message="I found an issue key. Would you like to see its details or log time on it?",
        )
    
    # Default: need clarification
    return ClassificationResult(
        intent=Intent.CLARIFY,
        confidence=0.3,
        extracted_data={},
        message="I'm not sure what you want to do. Try:\n- Log work: '2h on GBI-123 fixing bugs'\n- List tasks: 'my tasks' or 'show in progress'\n- Task details: 'what is GBI-123'",
    )


def parse_log_data(user_input: str, context: str = "") -> dict:
    """
    Parse work log details from natural language.
    Used when intent is LOG_WORK.
    """
    client = get_genai_client()
    
    # Get task type hint from memory patterns
    memory = load_memory()
    task_hint = get_task_type_hint(memory, user_input)
    task_hint_text = f"\nHint: Based on keywords, this might be '{task_hint}' type work." if task_hint else ""
    
    prompt = f"""
Extract work log details from this text: "{user_input}"

{context}
{task_hint_text}

Extract:
1. Issue Key (e.g., PROJ-123, GBI-645) - use empty string if not found
2. Time Spent in Jira format (like '2h', '30m', '1h 30m')
3. Time as decimal hours (e.g., 2.0, 0.5, 1.5)
4. Description (clean summary of the work done)
5. Task type: "Development", "Design", "Meeting", "Documentation", "Research", "Planning", or "Other"

Return ONLY JSON: {{"key": "...", "time_jira": "...", "time_hours": ..., "desc": "...", "task_type": "..."}}
"""
    
    response = client.models.generate_content(
        model='gemini-2.0-flash',
        contents=prompt
    )
    
    clean_json = response.text.replace('```json', '').replace('```', '').strip()
    return json.loads(clean_json)


def plan_task_query(user_input: str, context: str = "") -> dict:
    """
    AI Agent plans the entire task query execution.
    
    Instead of rigid filter extraction, the AI:
    1. Generates the JQL query directly
    2. Decides how to display results
    3. Specifies columns, grouping, sorting dynamically
    
    This is a more flexible, agentic approach.
    """
    client = get_genai_client()
    
    prompt = f"""
You are an AI agent helping a user query their Jira tasks. Analyze their request and plan the execution.

User request: "{user_input}"

{context}

Your job is to:
1. Generate the appropriate JQL query
2. Decide the best way to display results
3. Choose which columns to show
4. Determine if grouping or special formatting is needed

JQL SYNTAX GUIDE:
- assignee = currentUser() - your tasks
- project = "GBI" - filter by project
- status = "In Progress" - filter by status
- priority = High - filter by priority (Highest, High, Medium, Low, Lowest)
- issuetype = Bug - filter by type
- updated >= -1w - updated in last week
- ORDER BY priority DESC - sort by priority (highest first)
- ORDER BY updated DESC - sort by recently updated
- ORDER BY status ASC - sort by status
- ORDER BY created DESC - sort by creation date

DISPLAY OPTIONS:
- format: "table" (standard table), "grouped" (group by a field), "compact" (minimal)
- group_by: "project", "status", "priority", "type", or null
- columns: array of columns to show, e.g. ["key", "summary", "status", "priority", "time_spent"]
- show_time_spent: true if user wants to see logged time

Think step by step:
1. What is the user asking for?
2. What JQL will get the right data?
3. How should results be displayed to match their expectation?

Return ONLY valid JSON:
{{
  "reasoning": "Brief explanation of your understanding and plan",
  "jql": "the JQL query string",
  "display": {{
    "format": "table|grouped|compact",
    "group_by": "field name or null",
    "columns": ["key", "summary", "status", "priority"],
    "show_time_spent": false
  }}
}}

Examples:
- "show my tasks order by priority" -> {{
    "reasoning": "User wants their tasks sorted by priority, highest first",
    "jql": "assignee = currentUser() ORDER BY priority DESC",
    "display": {{"format": "table", "group_by": null, "columns": ["key", "summary", "status", "priority"], "show_time_spent": false}}
  }}
- "my GBI bugs grouped by status" -> {{
    "reasoning": "User wants GBI project bugs, grouped by their status",
    "jql": "assignee = currentUser() AND project = GBI AND issuetype = Bug ORDER BY status ASC",
    "display": {{"format": "grouped", "group_by": "status", "columns": ["key", "summary", "priority"], "show_time_spent": false}}
  }}
- "in progress tasks with time spent" -> {{
    "reasoning": "User wants in-progress tasks with time tracking info",
    "jql": "assignee = currentUser() AND status = \\"In Progress\\" ORDER BY updated DESC",
    "display": {{"format": "table", "group_by": null, "columns": ["key", "summary", "status", "time_spent"], "show_time_spent": true}}
  }}
- "high priority tasks grouped by project" -> {{
    "reasoning": "User wants high priority tasks organized by project",
    "jql": "assignee = currentUser() AND priority in (Highest, High) ORDER BY priority DESC",
    "display": {{"format": "grouped", "group_by": "project", "columns": ["key", "summary", "status", "priority"], "show_time_spent": false}}
  }}
"""
    
    response = client.models.generate_content(
        model='gemini-2.0-flash',
        contents=prompt
    )
    
    clean_json = response.text.replace('```json', '').replace('```', '').strip()
    return json.loads(clean_json)


# Keep old function for backward compatibility but mark as deprecated
def parse_task_query(user_input: str, context: str = "") -> dict:
    """DEPRECATED: Use plan_task_query instead. This is kept for backward compatibility."""
    return plan_task_query(user_input, context)


@dataclass
class OrchestratorResult:
    """Result from orchestrator."""
    success: bool
    intent: Intent
    message: str
    data: Optional[dict] = None


def orchestrate(
    user_input: str,
    log_work_handler: Optional[Callable] = None,
    query_tasks_handler: Optional[Callable] = None,
    task_detail_handler: Optional[Callable] = None,
    work_summary_handler: Optional[Callable] = None,
) -> OrchestratorResult:
    """
    Main orchestrator entry point.
    
    1. Load memory for context
    2. Classify intent with AI
    3. Route to appropriate handler
    4. Update memory with results
    
    Args:
        user_input: Natural language input from user
        log_work_handler: Function to call for logging work
        query_tasks_handler: Function to call for querying tasks
        task_detail_handler: Function to call for task details
        work_summary_handler: Function to call for work summary/reports
    """
    # 1. Load memory and build context
    memory = load_memory()
    context = build_context_for_ai(memory)
    
    # 2. Classify intent
    console.print(f"[bold blue]Analyzing:[/bold blue] '{user_input}'...")
    classification = classify_intent(user_input, context)
    
    console.print(f"[dim]Intent: {classification.intent.value} (confidence: {classification.confidence:.0%})[/dim]")
    
    # Track the query
    memory = add_query(memory, user_input, classification.intent.value)
    
    # 3. Route based on intent
    result = None
    
    if classification.intent == Intent.LOG_WORK:
        if log_work_handler:
            # Parse detailed log data
            try:
                log_data = parse_log_data(user_input, context)
                result = log_work_handler(log_data)
                
                # Update memory with issue if present
                if log_data.get("key"):
                    memory = add_issue(memory, log_data["key"])
                memory = increment_stat(memory, "total_logs")
                
            except Exception as e:
                result = OrchestratorResult(
                    success=False,
                    intent=classification.intent,
                    message=f"Failed to parse log data: {e}",
                )
        else:
            result = OrchestratorResult(
                success=False,
                intent=classification.intent,
                message="Log work handler not configured",
            )
    
    elif classification.intent == Intent.QUERY_TASKS:
        if query_tasks_handler:
            try:
                filters = parse_task_query(user_input, context)
                result = query_tasks_handler(filters)
                memory = increment_stat(memory, "total_queries")
                
            except Exception as e:
                result = OrchestratorResult(
                    success=False,
                    intent=classification.intent,
                    message=f"Failed to parse query: {e}",
                )
        else:
            result = OrchestratorResult(
                success=False,
                intent=classification.intent,
                message="Query tasks handler not configured",
            )
    
    elif classification.intent == Intent.TASK_DETAIL:
        if task_detail_handler:
            issue_key = classification.extracted_data.get("issue_key", "")
            if issue_key:
                result = task_detail_handler(issue_key)
                memory = add_issue(memory, issue_key)
            else:
                result = OrchestratorResult(
                    success=False,
                    intent=classification.intent,
                    message="Could not extract issue key from input",
                )
        else:
            result = OrchestratorResult(
                success=False,
                intent=classification.intent,
                message="Task detail handler not configured",
            )
    
    elif classification.intent == Intent.WORK_SUMMARY:
        if work_summary_handler:
            try:
                period = classification.extracted_data.get("period", "last_week")
                project = classification.extracted_data.get("project")
                result = work_summary_handler(period, project)
            except Exception as e:
                result = OrchestratorResult(
                    success=False,
                    intent=classification.intent,
                    message=f"Failed to generate summary: {e}",
                )
        else:
            result = OrchestratorResult(
                success=False,
                intent=classification.intent,
                message="Work summary handler not configured",
            )
    
    elif classification.intent == Intent.HELP:
        result = OrchestratorResult(
            success=True,
            intent=classification.intent,
            message=_get_help_message(),
        )
    
    elif classification.intent == Intent.CLARIFY:
        result = OrchestratorResult(
            success=True,
            intent=classification.intent,
            message=classification.message or "Please provide more details about what you want to do.",
        )
    
    # 4. Save memory
    save_memory(memory)
    
    return result or OrchestratorResult(
        success=False,
        intent=classification.intent,
        message="Unknown error occurred",
    )


def _get_help_message() -> str:
    """Return help message for users."""
    return """
Smart Logger - AI-powered work logging

Usage: python main.py "<natural language input>"

Examples:
  Log work:
    "2h on GBI-123 implementing feature"
    "30m team standup meeting"
    "1h reviewing PRs"
  
  List tasks:
    "my tasks"
    "show in progress issues"
    "my GBI bugs grouped by status"
    "high priority tasks with time"
  
  Task details:
    "what is GBI-123"
    "details on KFS-456"
  
  Work summary:
    "summary my work last week"
    "report my worklogs this month"
    "how much did I work on GBI last week"

Commands:
  log         - Explicitly log work (e.g., log "2h on GBI-123")
  tasks       - Explicitly query tasks
  notion-login - Login to Notion
  notion-status - Show Notion status
""".strip()
