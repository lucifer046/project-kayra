# ┌────────────────────────────────────────────────────────────────────────┐
# │                                utils.py                                │
# │                        Shared Helper Utilities                         │
# └────────────────────────────────────────────────────────────────────────┘
"""
This module implements core, reusable utility tools shared across the KAYRA application.
It includes standardized logging setups, central project path resolution,
and premium terminal UI styling helpers utilizing the 'rich' library.
"""
import os
import sys
import logging

# Reconfigure stdout/stderr to support UTF-8 characters on Windows legacy consoles
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from rich.console import Console
from rich.theme import Theme
from rich.panel import Panel
from rich.text import Text

# Premium, modern cyberpunk theme for the KAYRA terminal UI
kayra_theme = Theme({
    "info": "bold cyan",
    "success": "bold green",
    "warning": "bold yellow",
    "error": "bold red",
    "critical": "bold red blink",
    "system": "bold magenta",
    "highlight": "bold violet",
    "text": "white",
    "dim": "dim",
})

console = Console(theme=kayra_theme)


def get_project_root():
    """
    Returns the absolute path to the project root directory.
    Guarantees stable resource lookup across different module subfolders.
    """
    # Moves up one level from 'modules/' directory to project root
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def setup_logger(name, log_filename="kayra.log", level=logging.INFO):
    """
    Configures and returns a robust logger instance writing structured logs to the 'logs/' folder.
    
    Parameters:
        name (str): Unique name of the module generating logs.
        log_filename (str): Target filename inside the 'logs/' directory.
        level (logging level): Minimum threshold level for logged events.
    """
    root = get_project_root()
    logs_dir = os.path.join(root, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    
    log_path = os.path.join(logs_dir, log_filename)
    
    # Structured format: Timestamp - Module - Level - Message
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    handler = logging.FileHandler(log_path, encoding='utf-8')
    handler.setFormatter(formatter)
    
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # Avoid duplicate handler registration on multiple setups
    if not logger.handlers:
        logger.addHandler(handler)
        
    return logger


# ┌────────────────────────────────────────────────────────────────────────┐
# │                   PREMIUM CONSOLE INTERFACE HELPERS                    │
# └────────────────────────────────────────────────────────────────────────┘

from rich.rule import Rule

def print_banner(title: str, subtitle: str = None):
    """
    Renders an elegant, premium panel banner for application entrypoints.
    """
    from dotenv import dotenv_values
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(root, ".env")
    env_vars = dotenv_values(env_path) or {}
    
    assistant_name = env_vars.get("ASSISTANT_NAME", "").strip()
    if not assistant_name:
        assistant_name = "Kayra"
        
    title = title.replace("KAYRA", assistant_name.upper())

    banner_text = Text()
    banner_text.append(title.upper(), style="bold white")
    if subtitle:
        banner_text.append(f"\n{subtitle}", style="dim cyan")
    
    panel = Panel(
        banner_text,
        border_style="magenta",
        expand=False,
        padding=(1, 4),
        subtitle=f"[dim]{assistant_name.upper()}[/dim]",
        subtitle_align="right"
    )
    console.print()
    console.print(panel)
    console.print()



def print_section(title: str):
    """
    Renders a section separator with a neat horizontal layout.
    """
    console.print()
    console.print(Rule(f"[bold white]{title.upper()}[/bold white]", style="dim magenta", align="left"))


import os

def safe_print(msg_format: str):
    try:
        console.print(msg_format)
    except ValueError as e:
        if "closed file" in str(e):
            # Terminal was abruptly closed (e.g., via Ctrl+W shortcut hitting the terminal)
            os._exit(1)
        raise

def print_info(msg: str):
    safe_print(f"[info][INFO][/info] [text]{msg}[/text]")

def print_success(msg: str):
    safe_print(f"[success][SUCCESS][/success] [text]{msg}[/text]")

def print_warning(msg: str):
    safe_print(f"[warning][WARNING][/warning] [text]{msg}[/text]")

def print_error(msg: str):
    safe_print(f"[error][ERROR][/error] [text]{msg}[/text]")

def print_critical(msg: str):
    safe_print(f"[critical][CRITICAL][/critical] [text]{msg}[/text]")

def print_system(msg: str):
    safe_print(f"[system][SYSTEM][/system] [text]{msg}[/text]")

