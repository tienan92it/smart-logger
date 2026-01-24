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
   
4. "help" - User is asking how to use the tool or what it can do.
   Extract: nothing
   
5. "clarify" - Input is ambiguous or doesn't fit other categories. Need more information.
   Extract: clarification_message (what to ask the user)

IMPORTANT RULES:
- If there's a time indicator (2h, 30m, 1 hour, etc.), it's almost always "log_work"
- "my tasks", "my issues", "show tasks" without time = "query_tasks"
- Just an issue key with "what is" or "details" = "task_detail"
- If unsure between query_tasks and log_work, check for time indicator

Return ONLY valid JSON:
{{"intent": "...", "confidence": 0.0-1.0, "extracted_data": {{...}}, "message": "optional message for clarify/help"}}

Examples:
- "2h on GBI-123 fixing bugs" -> {{"intent": "log_work", "confidence": 0.95, "extracted_data": {{"time_jira": "2h", "time_hours": 2.0, "issue_key": "GBI-123", "description": "fixing bugs"}}, "message": null}}
- "my GBI tasks" -> {{"intent": "query_tasks", "confidence": 0.9, "extracted_data": {{"filters": {{"project": "GBI"}}}}, "message": null}}
- "show in progress tasks" -> {{"intent": "query_tasks", "confidence": 0.9, "extracted_data": {{"filters": {{"status": "In Progress"}}}}, "message": null}}
- "what is GBI-123" -> {{"intent": "task_detail", "confidence": 0.9, "extracted_data": {{"issue_key": "GBI-123"}}, "message": null}}
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


def parse_task_query(user_input: str, context: str = "") -> dict:
    """
    Parse task query filters and display options from natural language.
    Used when intent is QUERY_TASKS.
    """
    client = get_genai_client()
    
    prompt = f"""
Convert this request into Jira search filters and display options: "{user_input}"

{context}

Extract FILTERS if mentioned:
- status: "To Do", "In Progress", "Done", "Blocked", "Testing", "Done UAT"
- priority: "Highest", "High", "Medium", "Low", "Lowest"  
- issue_type: "Bug", "Task", "Story", "Epic"
- project: project key like "GBI", "KFS", "KBI"
- updated: relative time "-1w", "-1d", "-1m"
- text_search: keywords to search

Extract DISPLAY OPTIONS:
- group_by: How to group results. Options: "project" (by ticket prefix like GBI, KFS), "status", "priority", "type", null (no grouping)
- sort_by: How to sort. Options: "updated", "priority", "status", "key", null (default)
- show_description: true if user wants to see descriptions, false otherwise
- show_time_spent: true if user wants to see time logged/spent on tasks. Keywords: "time spent", "time logged", "hours", "how much time"

IMPORTANT: 
- If user says "group by project", "group by ticker", "group by prefix", "organize by project" -> set group_by to "project"
- If user mentions "time", "hours", "time spent", "time logged", "how long" -> set show_time_spent to true

Return ONLY JSON:
{{"filters": {{"status": null, "priority": null, "issue_type": null, "project": null, "updated": null, "text_search": null}}, "display": {{"group_by": null, "sort_by": null, "show_description": false, "show_time_spent": false}}}}

Examples:
- "my tasks grouped by project" -> {{"filters": {{}}, "display": {{"group_by": "project", "sort_by": null, "show_description": false, "show_time_spent": false}}}}
- "show in progress tasks by priority" -> {{"filters": {{"status": "In Progress"}}, "display": {{"group_by": "priority", "sort_by": null, "show_description": false, "show_time_spent": false}}}}
- "GBI tasks grouped by status" -> {{"filters": {{"project": "GBI"}}, "display": {{"group_by": "status", "sort_by": null, "show_description": false, "show_time_spent": false}}}}
- "show my tasks with time spent" -> {{"filters": {{}}, "display": {{"group_by": null, "sort_by": null, "show_description": false, "show_time_spent": true}}}}
- "how much time on GBI tasks" -> {{"filters": {{"project": "GBI"}}, "display": {{"group_by": null, "sort_by": null, "show_description": false, "show_time_spent": true}}}}
"""
    
    response = client.models.generate_content(
        model='gemini-2.0-flash',
        contents=prompt
    )
    
    clean_json = response.text.replace('```json', '').replace('```', '').strip()
    return json.loads(clean_json)


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
    "my GBI bugs"
    "high priority tasks"
  
  Task details:
    "what is GBI-123"
    "details on KFS-456"

Commands:
  log         - Explicitly log work (e.g., log "2h on GBI-123")
  tasks       - Explicitly query tasks
  notion-login - Login to Notion
  notion-status - Show Notion status
""".strip()
