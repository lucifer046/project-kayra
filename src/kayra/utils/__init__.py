"""
Shared helpers, re-exported as one flat facade.

The implementation is split by responsibility (`console`, `timing`, `text`) because the old
single `utils.py` did five unrelated jobs in 600 lines. The facade exists so call sites keep
reading `from kayra.utils import print_info, speech_safe_text` instead of memorising which of
three submodules a helper lives in.

Modules INSIDE `kayra.utils`, `kayra.core` and `kayra.memory` must import from the submodules
directly (`from kayra.utils.console import ...`) rather than from this facade — importing the
package from one of its own members is how import cycles start.
"""

from kayra.core.paths import (project_root, get_project_root, data_dir, models_dir,
                              logs_dir, reports_dir, data_path, model_path)
from kayra.utils.console import (console, kayra_theme, setup_logger, print_banner,
                                 print_section, safe_print, print_info, print_success,
                                 print_warning, print_error, print_critical, print_system)
from kayra.utils.timing import StageTimer, now_ms
from kayra.utils.text import (answer_modifier, real_time_info, SentenceStreamer,
                              speech_safe_text)

__all__ = [
    "project_root", "get_project_root", "data_dir", "models_dir", "logs_dir",
    "reports_dir", "data_path", "model_path",
    "console", "kayra_theme", "setup_logger", "print_banner", "print_section",
    "safe_print", "print_info", "print_success", "print_warning", "print_error",
    "print_critical", "print_system",
    "StageTimer", "now_ms",
    "answer_modifier", "real_time_info", "SentenceStreamer", "speech_safe_text",
]
