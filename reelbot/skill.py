"""Register vendor/video-use as a skill, the way its install.md describes.

Claude Code:  ~/.claude/skills/video-use  -> vendor/video-use
Codex:        $CODEX_HOME/skills/video-use (default ~/.codex) -> vendor/video-use

The whole directory is linked (helpers/ must stay next to SKILL.md). On Windows,
where symlinks need Developer Mode, a directory junction is used instead.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from . import VIDEO_USE_DIR

log = logging.getLogger(__name__)


def skills_dir(backend: str, home: Path | None = None) -> Path:
    home = home or Path.home()
    if backend == "claude":
        return home / ".claude" / "skills"
    if backend == "codex":
        codex_home = os.environ.get("CODEX_HOME")
        return (Path(codex_home) if codex_home else home / ".codex") / "skills"
    raise ValueError(backend)


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def ensure_skill_registered(backend: str, *, home: Path | None = None,
                            source: Path = VIDEO_USE_DIR) -> Path:
    """Create the skill link if missing. Never clobbers someone else's install."""
    if not (source / "SKILL.md").exists():
        raise FileNotFoundError(
            f"{source}/SKILL.md missing - run `git submodule update --init --recursive`")
    target = skills_dir(backend, home) / "video-use"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        if _same(target, source):
            return target
        log.warning("%s already exists and points elsewhere; leaving it alone", target)
        return target
    try:
        target.symlink_to(source, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(source)],
                       check=True, capture_output=True)
    log.info("registered video-use skill at %s", target)
    return target
