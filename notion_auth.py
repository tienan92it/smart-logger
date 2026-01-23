"""
Notion Authentication Module

Handles authentication via Playwright browser automation and token storage.
Tokens are cached and only refreshed when expired or on submission failure.
"""

import os
import json
from pathlib import Path
from datetime import datetime
from typing import Optional
from rich.console import Console

console = Console()

# Token storage file
TOKEN_FILE = Path.home() / ".smart-logger" / "notion_session.json"


def ensure_token_dir():
    """Ensure the token storage directory exists."""
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)


def load_stored_token() -> Optional[dict]:
    """Load stored token from file if it exists."""
    if TOKEN_FILE.exists():
        try:
            with open(TOKEN_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None
    return None


def save_token(token_data: dict):
    """Save token data to file."""
    ensure_token_dir()
    token_data["saved_at"] = datetime.now().isoformat()
    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f, indent=2)
    console.print(f"[dim]Token saved to {TOKEN_FILE}[/dim]")


def clear_token():
    """Clear stored token."""
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
        console.print("[yellow]Token cleared.[/yellow]")


def get_token_via_playwright(email: Optional[str] = None) -> dict:
    """
    Open browser for Notion login and extract tokens after authentication.
    
    Notion uses magic link auth (email code), so this requires user interaction.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        console.print("[red]Playwright not installed. Run:[/red]")
        console.print("  pip install playwright")
        console.print("  playwright install chromium")
        raise SystemExit(1)
    
    console.print("[bold blue]🔐 Opening browser for Notion login...[/bold blue]")
    console.print("[dim]Please complete the login in the browser window.[/dim]")
    
    token_v2 = None
    user_id = None
    
    with sync_playwright() as p:
        # Launch browser (visible so user can interact)
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        
        # Navigate to Notion login
        page.goto("https://www.notion.so/login")
        
        # Pre-fill email if provided
        if email:
            try:
                page.wait_for_selector('input[type="email"]', timeout=5000)
                page.fill('input[type="email"]', email)
                console.print(f"[dim]Pre-filled email: {email}[/dim]")
            except Exception:
                pass
        
        console.print("\n[yellow]⏳ Waiting for you to complete login...[/yellow]")
        console.print("[dim]DO NOT close the browser - it will close automatically.[/dim]\n")
        
        try:
            # Poll for token_v2 cookie (appears after successful login)
            console.print("[dim]Waiting for authentication token...[/dim]")
            
            for attempt in range(180):  # Wait up to 3 minutes
                try:
                    page.wait_for_timeout(1000)
                    
                    # Check cookies
                    cookies = context.cookies()
                    for cookie in cookies:
                        if cookie["name"] == "token_v2":
                            token_v2 = cookie["value"]
                        elif cookie["name"] == "notion_user_id":
                            user_id = cookie["value"]
                    
                    # If we have the token, we're done
                    if token_v2:
                        console.print("[green]Token detected![/green]")
                        break
                        
                except Exception:
                    # Browser might be closed, check if we got token
                    break
            
            # Try to close browser gracefully
            try:
                browser.close()
            except Exception:
                pass  # Browser might already be closed
            
        except Exception as e:
            # Try to close browser
            try:
                browser.close()
            except Exception:
                pass
            
            # If we got the token before error, that's fine
            if not token_v2:
                console.print(f"[red]❌ Login failed: {e}[/red]")
                raise
    
    if not token_v2:
        console.print("[red]❌ Failed to get token. Please try again and don't close the browser.[/red]")
        raise SystemExit(1)
    
    token_data = {
        "token_v2": token_v2,
        "user_id": user_id,
    }
    
    console.print("[bold green]✔ Login successful![/bold green]")
    return token_data


def get_notion_credentials(force_login: bool = False) -> dict:
    """
    Get Notion credentials, using cached token if available.
    
    Args:
        force_login: If True, ignore cached token and force new login.
    
    Returns:
        dict with 'token_v2' and 'user_id'
    """
    if not force_login:
        # Try to load stored token
        stored = load_stored_token()
        if stored and stored.get("token_v2"):
            console.print("[dim]Using cached Notion token.[/dim]")
            return stored
    
    # Need to login
    email = os.getenv("NOTION_EMAIL")
    token_data = get_token_via_playwright(email)
    save_token(token_data)
    return token_data
