# ┌────────────────────────────────────────────────────────────────────────┐
# │                             console.py                                 │
# │                 Themed Console Output & File Logging                   │
# └────────────────────────────────────────────────────────────────────────┘
"""
Every line Kayra prints goes through here.

One themed `rich` Console for the whole process, plus the `print_*` helpers the rest of the
codebase uses instead of bare `print()`. Centralising it means the assistant's output has one
visual identity, and it means a terminal that has died (a closed console raises `ValueError`
on write) is handled in one place rather than thirty.
"""

import os
import sys
import logging

from kayra.core.paths import logs_dir

from rich.console import Console


# Console encoding. Kayra prints emoji, box drawing and Devanagari; a Windows console on a
# legacy code page (cp1252) raises UnicodeEncodeError on all three, deep inside Rich's
# legacy-Windows renderer where the traceback names none of the above.
#
# `app.py` reconfigures its own streams, but the test suites and the standalone gesture engine
# do not — and they crashed on the very first `print_success`. Doing it HERE covers every entry
# point, because everything that prints imports this module. Guarded, because a redirected or
# already-wrapped stream may refuse.
if sys.platform.startswith("win"):
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
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

def setup_logger(name, log_filename="kayra.log", level=logging.INFO):
    """
    Configures and returns a robust logger instance writing structured logs to the 'logs/' folder.
    
    Parameters:
        name (str): Unique name of the module generating logs.
        log_filename (str): Target filename inside the 'logs/' directory.
        level (logging level): Minimum threshold level for logged events.
    """
    # logs_dir() resolves from the project root and creates the folder if needed, so a
    # logger works no matter which directory Kayra was launched from.
    log_path = os.path.join(logs_dir(), log_filename)
    
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
    # Imported lazily: core.config imports core.paths, which this module also imports —
    # a module-level import here would be a cycle for no benefit, since the banner is
    # drawn a handful of times per run.
    from kayra.core.config import env

    assistant_name = (env("ASSISTANT_NAME") or "").strip()
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


def safe_print(msg_format: str, **kwargs):
    """
    The one write to the console.

    `**kwargs` is forwarded to `Console.print`. The structured logger in `core/logbus.py`
    passes `soft_wrap=True`: a log line word-wrapped at the terminal width breaks the column
    alignment that makes the output scannable in the first place, and a truncated-looking
    second line reads as a different message.
    """
    try:
        console.print(msg_format, **kwargs)
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
