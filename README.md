# Smart Logger 🤖

An AI-powered CLI tool to log your work to **Jira** and **Notion** using natural language.

```bash
smart-log log -p DF "2h on GBI-645 implementing Redis Sentinel"
```

**What happens:**
1. 🤖 AI parses your natural language input
2. ✅ Logs worklog to Jira (if valid ticket)
3. ✅ Submits to Notion form (with auto-classified task type)

## Features

- **Natural Language Parsing** - Just describe your work, AI extracts ticket, time, and description
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

# Gemini AI (for natural language parsing)
GEMINI_API_KEY=your-gemini-api-key

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

### Log Work

```bash
# With project specified
smart-log log -p DF "2h on GBI-645 implementing Redis Sentinel"

# Uses default project (NOTION_PROJECT_DEFAULT_NAME)
smart-log log "30m on KFS-123 fixing bug"

# Meetings (auto-classified)
smart-log log -p DF "1h team sync meeting"

# Documentation
smart-log log -p DF "1h on GBI-645 writing API docs"

# No Jira ticket (Notion only)
smart-log log -p DF "1h sprint planning session"
```

### View Jira Tasks

```bash
# Show all your tasks
smart-log tasks

# Natural language queries
smart-log tasks "in progress"
smart-log tasks "high priority bugs"
smart-log tasks "updated this week"

# Filter by status
smart-log tasks --status "In Progress"
smart-log tasks -s "To Do" -n 10
```

### Notion Authentication

```bash
# Login to Notion (opens browser)
smart-log notion-login

# Check auth status
smart-log notion-status

# Logout (clear cached token)
smart-log notion-logout
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
├── main.py           # CLI commands and main logic
├── notion_auth.py    # Playwright-based Notion authentication
├── notion_form.py    # Notion form submission via internal API
├── pyproject.toml    # Package config for global install
├── requirements.txt  # Python dependencies
├── .env.example      # Configuration template
├── .env              # Your configuration (not in git)
└── README.md
```

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
