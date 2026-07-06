# Smart Logger 🤖

An AI-powered CLI tool to log your work to **Jira** and **Notion** using natural language.

```bash
# Smart mode - AI figures out what you want
smart-log "2h on GBI-645 implementing Redis Sentinel"
smart-log "my tasks"
smart-log "show in progress bugs"
```

**What happens:**
1. 🤖 AI classifies your intent (log work, query tasks, get details, etc.)
2. 🧠 Context from memory enhances AI understanding
3. ✅ Automatically routes to the right action

## Features

- **Smart Intent Detection** - Just describe what you want, AI routes to the right action
- **Multi-Provider AI** - Gemini (default), OpenAI, or Anthropic — switch via `AI_PROVIDER`
- **Context-Aware Memory** - Learns your projects, issues, and patterns over time
- **Natural Language Parsing** - AI extracts ticket, time, and description
- **Smart Task Classification** - Auto-detects: Development, Design, Meeting, Documentation, Research, Planning
- **Jira Integration** - Logs worklogs to Jira tickets
- **Notion Form Submission** - Submits to Notion via internal form API (no integration setup needed)
- **Multi-Project Support** - Configure multiple Notion projects
- **Browser-Based Auth** - Login to Notion via browser, tokens cached automatically

## Installation

```bash
# Clone the repo
git clone <repo-url>
cd smart-logger

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browser
playwright install chromium
```

## Global Setup (Run from Anywhere)

Install as a global command so you can run `smart-log` from any directory:

```bash
# From the project directory (with venv activated)
pip install -e .
```

### Option 1: Use with venv activated

```bash
# Activate venv first, then run from anywhere
source /path/to/smart-logger/venv/bin/activate
smart-log log -p DF "2h on GBI-645 implementing feature"
```

### Option 2: Shell alias (recommended)

Add to your `~/.zshrc` or `~/.bashrc`:

```bash
alias smart-log="/path/to/smart-logger/venv/bin/smart-log"
```

Then reload your shell:

```bash
source ~/.zshrc  # or ~/.bashrc
```

Now you can run from anywhere without activating venv:

```bash
smart-log log -p DF "2h on GBI-645 implementing feature"
smart-log tasks "in progress"
smart-log notion-status
```

## Configuration

Create a `.env` file:

```bash
# Jira Configuration
JIRA_SERVER=https://your-company.atlassian.net
JIRA_EMAIL=your@email.com
JIRA_API_TOKEN=your-jira-api-token

# AI Provider (gemini | openai | anthropic) — defaults to gemini
AI_PROVIDER=gemini
# Optional override; otherwise a sane per-provider default is used
# AI_MODEL=gemini-3.5-flash

# Gemini (default)
GEMINI_API_KEY=your-gemini-api-key
# OpenAI (when AI_PROVIDER=openai)
# OPENAI_API_KEY=sk-...
# Anthropic (when AI_PROVIDER=anthropic)
# ANTHROPIC_API_KEY=sk-ant-...

# Notion Form Configuration
NOTION_FORM_ID=2cc64b29-b84c-8090-8765-c0d8656e212f
NOTION_SPACE_ID=498ebd7b-383c-459f-a9ad-b74073208ddd

# Notion Projects (page IDs for project relations)
# Option 1: JSON mapping
NOTION_PROJECTS={"DF": "page-id-1", "HF": "page-id-2"}

# Option 2: Individual vars
NOTION_PROJECT_DF=1f464b29-b84c-809f-a3da-dc5d5f75fbb7
NOTION_PROJECT_HF=another-page-id

# Default project when -p is not specified
NOTION_PROJECT_DEFAULT_NAME=DF

# Optional: Pre-fill email on Notion login
NOTION_EMAIL=your@email.com
```

### Getting Notion Form IDs

1. Open your Notion form in a browser
2. Open DevTools → Network tab
3. Submit the form manually
4. Find the `submitForm` request
5. Copy `formId` and `spaceId` from the request payload

## Usage

### Smart Mode (Recommended)

Just describe what you want - AI figures out the rest:

```bash
# Log work (AI detects time indicator)
smart-log "2h on GBI-645 implementing Redis Sentinel"
smart-log "30m team standup meeting"
smart-log "1h reviewing PRs"

# Query tasks (AI detects query intent)
smart-log "my tasks"
smart-log "show in progress bugs"
smart-log "my GBI issues"
smart-log "high priority tasks"

# Task details
smart-log "what is GBI-123"
smart-log "details on KFS-456"

# Help
smart-log "help"
```

### Explicit Commands

You can also use explicit commands if you prefer:

```bash
# Log work explicitly
smart-log log -p DF "2h on GBI-645 implementing Redis Sentinel"
smart-log log "30m on KFS-123 fixing bug"

# Query tasks explicitly
smart-log tasks
smart-log tasks "in progress"
smart-log tasks --status "In Progress" -n 10
```

### AI Providers

Smart Logger speaks to any of three LLM backends through a thin abstraction
in `ai_client.py`. Pick one with `AI_PROVIDER` and supply the matching API key.

| Provider    | `AI_PROVIDER` | Auth env                                  | Default model               | Install                                  |
|-------------|---------------|-------------------------------------------|------------------------------|------------------------------------------|
| Gemini      | `gemini`      | `GEMINI_API_KEY` *(or Vertex ADC)*        | `gemini-3.5-flash`           | included by default                       |
| OpenAI      | `openai`      | `OPENAI_API_KEY` *(opt. `OPENAI_BASE_URL`)* | `gpt-4o-mini`              | `pip install 'smart-logger[openai]'`     |
| Anthropic   | `anthropic`   | `ANTHROPIC_API_KEY`                       | `claude-3-5-haiku-latest`    | `pip install 'smart-logger[anthropic]'`  |

Override the model with `AI_MODEL=...`. The OpenAI provider also accepts
`OPENAI_BASE_URL` for any OpenAI-compatible endpoint (Azure OpenAI, OpenRouter,
local servers, etc.). SDKs for non-default providers are lazy-imported, so you
only need to install what you actually use.

### Notion Authentication

```bash
# Login to Notion (opens browser)
smart-log notion-login

# Check auth status
smart-log notion-status

# Logout (clear cached token)
smart-log notion-logout
```

## Local Jira Cache

Pull Jira tickets (with descriptions, comments, relationships, worklogs, attachment metadata) into a local SQLite store at `~/.smart-logger/jira_cache.db`. Use it for offline lookups, full-text search, and analytical reports — manually or via the MCP tools below.

### Why

- **Fast reads**: SQLite + FTS5 beats round-trips to Jira for repeated lookups.
- **Stable surface**: Reports run against a known schema, not the live Jira API.
- **Agent-friendly**: MCP tools let an agent sync, search, and reason over tickets without making the user re-run queries.

### Commands

```bash
# Pull tickets — default: your work, last 180 days
smart-log jira-sync

# Scope to a project
smart-log jira-sync --project GBI

# Incremental sync from a date
smart-log jira-sync --since 2025-01-01

# Refresh a single ticket
smart-log jira-sync --key GBI-645

# Custom JQL (overrides other flags)
smart-log jira-sync --jql 'project = GBI AND status != Done'

# Skip per-ticket worklog calls (faster, but no time data)
smart-log jira-sync --no-worklogs

# Inspect cached ticket
smart-log jira-show GBI-645
smart-log jira-show GBI-645 --worklogs --no-comments

# Full-text search (FTS5: phrases, AND/OR, NEAR/N, prefix*)
smart-log jira-search "redis sentinel"
smart-log jira-search "auth NEAR/5 oauth"
smart-log jira-search "payment*"

# Cache stats + analytical breakdown
smart-log jira-stats
smart-log jira-stats --project GBI --stale-days 21

# Analytical reports (workload, daily plan, epic rollup, time spent, ...)
smart-log jira-report digest --days 7
smart-log jira-report daily
smart-log jira-report epics --project GBI
smart-log jira-report scope --field labels
smart-log jira-report stale --days 21
smart-log jira-report worklog --days 14
```

### Reports (`smart-log jira-report`)

Single command, several "kinds". All offline, all scoped to `--mine` (current user) by default — pass `--all` for project-wide views.

| Kind        | What it shows                                                                                       |
|-------------|-----------------------------------------------------------------------------------------------------|
| `digest`    | Workload overview — open totals, by status / priority / project / type, top priorities, recent activity, time spent in the last `--days` vs. the prior window. |
| `daily`     | Focus list — In Progress, High/Highest queue, stale In Progress (idle ≥ 7d), blocked (status name contains "block" or has an inward "blocks" link). |
| `epics`     | Group by `epic_key` with ticket count, done count, % complete, time invested, last activity. Epics not yet in cache show blank summaries — sync the epic key to fill them in. |
| `scope`     | Distribution by `--field components` / `labels` / `fix_versions`. Useful for "where is my effort going". |
| `stale`     | Tickets idle ≥ `--days`, ordered oldest first.                                                      |
| `worklog`   | Time-spent breakdown — by date / project / top tickets in the last `--days`.                        |

### What gets captured

| Table | Contents |
|-------|----------|
| `tickets` | key, project, summary, description (raw + rendered), status, priority, type, assignee/reporter, parent, epic, labels, components, fix versions, sprint, story points, time tracking, timestamps, full raw JSON |
| `comments` | id, author, body (raw + rendered), created/updated |
| `links` | source/target keys, link type, direction (`outward`/`inward`), human label |
| `worklogs` | id, author, seconds spent, started date, comment |
| `attachments` | id, filename, mime, size, author, URL (metadata only — no binaries) |
| `sync_runs` | audit trail of every sync execution |
| `tickets_fts` | FTS5 index over summary + description + comments |

Sync is **additive**: pulling a subset never deletes tickets outside the JQL scope. Comments / links / worklogs of *synced* tickets are replaced so edits and deletions inside Jira propagate.

### MCP tools (agent usage)

Run `python mcp_server.py` and the following tools become available to your agent:

| Tool | Purpose |
|------|---------|
| `jira_sync_local` | Trigger a sync. Accepts `jql`, `project`, `since`, `key`, `only_mine`, `full`, `max_tickets`, `fetch_worklogs`. |
| `jira_get_ticket_local` | Fetch a cached ticket as Markdown (description + comments + links + optional worklogs). |
| `jira_search_local` | FTS5 search across the cache. |
| `jira_local_stats` | Cache totals + status/assignee breakdown + stale tickets. |
| `jira_relationships_local` | Local relationship graph for a ticket (`depth=1` direct, `depth=2` neighbors-of-neighbors). |
| `jira_report_local` | Analytical reports: `digest`, `daily`, `epics`, `scope`, `stale`, `worklog`. Same surface as the CLI. |

### Typical agent flow

```text
1. jira_sync_local(project="GBI", since="2025-01-01")
2. jira_search_local("memory leak")
3. jira_get_ticket_local("GBI-645", include_worklogs=true)
4. jira_relationships_local("GBI-645", depth=2)
5. jira_local_stats(project="GBI", stale_days=14)
```

## Task Type Classification

The AI automatically classifies your work into these categories:

| Task Type | Examples |
|-----------|----------|
| **Development** | coding, implementing, fixing bugs, debugging |
| **Design** | UI/UX, wireframes, mockups, design review |
| **Meeting** | meetings, calls, sync-ups, standups |
| **Documentation** | writing docs, README, API docs |
| **Research** | investigating, POC, spike, learning |
| **Planning** | sprint planning, roadmap, estimation |
| **Other** | anything else |

## Project Structure

```
smart-logger/
├── main.py             # CLI commands and smart handler
├── ai_orchestrator.py  # AI intent classification and routing
├── ai_client.py        # Provider-agnostic LLM client (Gemini/OpenAI/Anthropic)
├── memory_bank.py      # Persistent context/memory storage
├── notion_auth.py      # Playwright-based Notion authentication
├── notion_form.py      # Notion form submission via internal API
├── jira_store.py       # Local Jira cache: SQLite schema + CRUD + FTS
├── jira_sync.py        # Local Jira cache: pull from Jira API into the store
├── jira_reports.py     # Local Jira cache: analytical queries
├── mcp_server.py       # MCP server exposing tools to agents (incl. jira cache)
├── pyproject.toml      # Package config for global install
├── requirements.txt    # Python dependencies
├── .env.example        # Configuration template
├── .env                # Your configuration (not in git)
└── README.md
```

### Memory Bank

The tool stores context in `~/.smart-logger/memory.json`:
- Recent issues you've worked on
- Auto-learned project codes
- Query history
- Usage stats

This context is injected into AI prompts to make responses smarter over time.

## How It Works

### Notion Authentication

Since you may not have permission to add integrations to Notion databases, this tool uses browser-based authentication:

1. `notion-login` opens a Chromium browser
2. You login to Notion normally
3. The tool extracts `token_v2` cookie
4. Token is cached in `~/.smart-logger/notion_session.json`
5. Token is reused until it expires

### Form Submission

Instead of using the official Notion API (which requires integration access), this tool submits directly to Notion's internal form API - the same API used when you submit a form in the browser.

## Troubleshooting

### Token Expired
```bash
smart-log notion-login
```

### Jira Connection Failed
- Check `JIRA_SERVER`, `JIRA_EMAIL`, `JIRA_API_TOKEN` in `.env`
- Ensure your Jira API token has worklog permissions

### Notion Form Submission Failed
- Verify `NOTION_FORM_ID` and `NOTION_SPACE_ID`
- Check project mapping (`NOTION_PROJECT_*`)
- Run `smart-log notion-login` to refresh token

## License

MIT
