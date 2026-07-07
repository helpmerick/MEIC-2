"""File integrity — NFR-05. Tolerate the known BOM case, refuse the rest.

All bot-read files are decoded BOM-tolerantly (utf-8-sig) — the host has
demonstrated a tool that silently prepends BOMs. Beyond that: at startup the
bot hashes its critical files against the hashes recorded at last shutdown; any
change not made by the bot itself ⇒ refuse to arm, name the file, require
operator confirmation. A machine that places orders never silently tolerates
unexplained file modification.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


def read_text_bom_tolerant(path: Path) -> str:
    """utf-8-sig strips a BOM if present, leaving key names intact (Bug #21/#25)."""
    return path.read_text(encoding="utf-8-sig")


def hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class IntegrityGuard:
    """Compares current file hashes against those recorded at last shutdown."""

    recorded: dict[str, str]  # path -> sha256 at last clean shutdown

    def changed_files(self, current: dict[str, str]) -> list[str]:
        """Files whose hash differs (or vanished) since last shutdown — a
        truncated file differs and so is caught, never silently defaulted."""
        return sorted(f for f, h in self.recorded.items() if current.get(f) != h)

    def may_arm(self, current: dict[str, str]) -> bool:
        """NFR-05: refuse to arm while any critical file changed outside the
        bot's own writes."""
        return not self.changed_files(current)
