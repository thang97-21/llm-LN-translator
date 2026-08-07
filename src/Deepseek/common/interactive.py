"""Non-blocking interactive prompts for unattended / TUI pipeline runs.

Phase code frequently asks the operator a yes/no or numbered-choice question
via ``input()`` (e.g. the Librarian's RTL kuchie-order choice). Under a TUI —
Textual or questionary owns the keyboard — or when the child's stdout is
captured into a panel/pipe, those keystrokes never reach the process, so
``input()`` blocks forever and the phase hangs with no way to answer.

``prompt_or_default()`` falls back to the prompt's own documented default
whenever no usable interactive keyboard is available — the same result as an
operator pressing Enter — so long/unattended/TUI runs proceed instead of
stalling. In a real terminal it behaves exactly like ``input()``.
"""

from __future__ import annotations

import os
import sys

_TRUTHY = {"1", "true", "yes", "on"}


def noninteractive() -> bool:
    """True when there is no usable interactive keyboard for ``input()``.

    Any one of these signals is sufficient:

    * ``MTL_NONINTERACTIVE`` env var is truthy — set by frontends/launchers that
      own the keyboard so children never try to prompt.
    * ``stdin`` is not a TTY — input is piped or redirected.
    * ``stdout`` is not a TTY — output is captured into a panel/pipe (as a TUI
      does), so a blocking prompt would be invisible and unanswerable.
    """
    if os.environ.get("MTL_NONINTERACTIVE", "").strip().lower() in _TRUTHY:
        return True
    for stream in (sys.stdin, sys.stdout):
        try:
            if stream is None or not stream.isatty():
                return True
        except (ValueError, OSError):
            return True
    return False


def prompt_or_default(prompt: str, default: str) -> str:
    """``input()`` that can never hang.

    Interactive terminal: identical to ``input(prompt)``; on EOF returns
    ``default``.

    Non-interactive (TUI / captured / piped): echoes the prompt with the
    auto-selected default and returns ``default`` immediately — the same choice
    an operator pressing Enter would make.

    Callers keep their existing ``.strip()`` / ``.lower()`` post-processing; this
    only replaces the blocking ``input()`` itself.
    """
    if noninteractive():
        try:
            print(f"{prompt}{default}   [auto — non-interactive default]")
        except Exception:
            pass
        return default
    try:
        return input(prompt)
    except EOFError:
        try:
            print()
        except Exception:
            pass
        return default
