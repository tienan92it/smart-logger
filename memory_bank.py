"""
Memory Bank for Smart Logger

Persistent storage for context that helps AI make better decisions:
- Recent issues worked on
- Known project codes
- Common patterns (e.g., "standup" -> Meeting)
- Query history for learning
"""

import os
import json
from datetime import datetime
from pathlib import Path
from typing import Optional
from collections import Counter


# Storage location
MEMORY_DIR = Path.home() / ".smart-logger"
MEMORY_FILE = MEMORY_DIR / "memory.json"

# Limits to prevent unbounded growth
MAX_RECENT_ISSUES = 50
MAX_LAST_QUERIES = 20
MAX_PROJECTS = 20


def _get_default_memory() -> dict:
    """Return empty memory structure."""
    return {
        "recent_issues": [],  # [{key, title, project, last_used, use_count}]
        "projects": [],  # Auto-learned project codes
        "patterns": {  # keyword -> task_type mapping
            "standup": "Meeting",
            "daily": "Meeting",
            "sync": "Meeting",
            "review": "Development",
            "pr review": "Development",
            "code review": "Development",
            "debug": "Development",
            "fix": "Development",
            "implement": "Development",
            "research": "Research",
            "investigate": "Research",
            "docs": "Documentation",
            "document": "Documentation",
            "planning": "Planning",
            "design": "Design",
        },
        "last_queries": [],  # [{query, intent, timestamp}]
        "stats": {
            "total_logs": 0,
            "total_queries": 0,
        },
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }


def load_memory() -> dict:
    """Load memory from disk, or create default if not exists."""
    try:
        if MEMORY_FILE.exists():
            with open(MEMORY_FILE, "r") as f:
                memory = json.load(f)
                # Ensure all keys exist (for upgrades)
                default = _get_default_memory()
                for key in default:
                    if key not in memory:
                        memory[key] = default[key]
                return memory
    except (json.JSONDecodeError, IOError):
        pass
    return _get_default_memory()


def save_memory(memory: dict) -> None:
    """Save memory to disk."""
    memory["updated_at"] = datetime.now().isoformat()
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f, indent=2)


def add_issue(memory: dict, key: str, title: str = "", project: str = "") -> dict:
    """
    Track an issue that was used. Updates use_count if already exists.
    Auto-learns project code from issue key.
    """
    if not key:
        return memory

    # Extract project from key if not provided (e.g., "GBI-123" -> "GBI")
    if not project and "-" in key:
        project = key.split("-")[0].upper()

    # Learn the project
    if project:
        memory = learn_project(memory, project)

    now = datetime.now().isoformat()

    # Check if issue already exists
    for issue in memory["recent_issues"]:
        if issue["key"].upper() == key.upper():
            issue["last_used"] = now
            issue["use_count"] = issue.get("use_count", 1) + 1
            if title:
                issue["title"] = title
            if project:
                issue["project"] = project
            return memory

    # Add new issue
    memory["recent_issues"].insert(0, {
        "key": key.upper(),
        "title": title,
        "project": project,
        "last_used": now,
        "use_count": 1,
    })

    # Trim to max size (keep most recently used)
    if len(memory["recent_issues"]) > MAX_RECENT_ISSUES:
        memory["recent_issues"] = memory["recent_issues"][:MAX_RECENT_ISSUES]

    return memory


def learn_project(memory: dict, project: str) -> dict:
    """Auto-learn a project code from usage."""
    if not project:
        return memory

    project = project.upper()
    if project not in memory["projects"]:
        memory["projects"].append(project)
        # Trim to max
        if len(memory["projects"]) > MAX_PROJECTS:
            memory["projects"] = memory["projects"][-MAX_PROJECTS:]

    return memory


def add_query(memory: dict, query: str, intent: str) -> dict:
    """Track a query for learning patterns."""
    memory["last_queries"].insert(0, {
        "query": query,
        "intent": intent,
        "timestamp": datetime.now().isoformat(),
    })

    # Trim to max
    if len(memory["last_queries"]) > MAX_LAST_QUERIES:
        memory["last_queries"] = memory["last_queries"][:MAX_LAST_QUERIES]

    return memory


def increment_stat(memory: dict, stat_name: str) -> dict:
    """Increment a stat counter."""
    if stat_name in memory["stats"]:
        memory["stats"][stat_name] += 1
    return memory


def get_recent_issues(memory: dict, limit: int = 10) -> list:
    """Get most recently used issues."""
    return memory["recent_issues"][:limit]


def get_issue_by_key(memory: dict, key: str) -> Optional[dict]:
    """Find an issue by key in memory."""
    key_upper = key.upper()
    for issue in memory["recent_issues"]:
        if issue["key"] == key_upper:
            return issue
    return None


def get_projects(memory: dict) -> list:
    """Get known project codes."""
    return memory["projects"]


def get_task_type_hint(memory: dict, text: str) -> Optional[str]:
    """Get task type hint from patterns."""
    text_lower = text.lower()
    for pattern, task_type in memory["patterns"].items():
        if pattern in text_lower:
            return task_type
    return None


def get_most_used_project(memory: dict) -> Optional[str]:
    """Get the most frequently used project."""
    if not memory["recent_issues"]:
        return None

    projects = [issue.get("project") for issue in memory["recent_issues"] if issue.get("project")]
    if not projects:
        return None

    return Counter(projects).most_common(1)[0][0]


def build_context_for_ai(memory: dict) -> str:
    """
    Build a context string to inject into AI prompts.
    This helps the AI understand user's context better.
    """
    context_parts = []

    # Recent issues
    recent = get_recent_issues(memory, limit=5)
    if recent:
        issues_str = ", ".join([f"{i['key']}" + (f" ({i['title'][:30]})" if i.get('title') else "") for i in recent])
        context_parts.append(f"Recent issues worked on: {issues_str}")

    # Known projects
    projects = get_projects(memory)
    if projects:
        context_parts.append(f"Known project codes: {', '.join(projects)}")

    # Most used project
    most_used = get_most_used_project(memory)
    if most_used:
        context_parts.append(f"Most frequently used project: {most_used}")

    if not context_parts:
        return ""

    return "User context:\n" + "\n".join(f"- {p}" for p in context_parts)


def clear_memory() -> None:
    """Clear all memory (for testing/reset)."""
    if MEMORY_FILE.exists():
        MEMORY_FILE.unlink()
