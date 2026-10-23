"""Which commit of the code is checked out, read straight from .git (no git command, so it also works inside the
API's sandbox and costs a couple of small file reads).

A running process keeps the code it started with; `git pull` changes only the files. So the bot records the commit
it started on, and the app compares that with what is checked out now to say when a restart is needed.
"""
from __future__ import annotations

from pathlib import Path

from bot import config as C


def _git_dir(root: Path) -> Path | None:
    g = root / ".git"
    if g.is_file():                                   # a worktree: ".git" names the real folder
        text = g.read_text().strip()
        if text.startswith("gitdir:"):
            g = (root / text[len("gitdir:"):].strip()).resolve()
    return g if g.is_dir() else None


def head(root: Path | None = None) -> str | None:
    """The checked-out commit (40 hex characters), or None when it can't be read."""
    try:
        g = _git_dir(root or C.ROOT)
        if g is None:
            return None
        ref = (g / "HEAD").read_text().strip()
        if not ref.startswith("ref:"):
            return ref or None                        # a detached checkout names the commit itself
        name = ref[4:].strip()
        f = g / name
        if f.is_file():
            return f.read_text().strip() or None
        packed = g / "packed-refs"
        if packed.is_file():
            for line in packed.read_text().splitlines():
                if line.endswith(" " + name):
                    return line.split(" ", 1)[0]
    except OSError:
        return None
    return None


def short(commit: str | None) -> str:
    return (commit or "")[:7]
