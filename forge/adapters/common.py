"""Shared emission helpers for adapters."""

from __future__ import annotations

from pathlib import Path


class Emitter:
    """Writes adapter output files under out_dir and tracks relative paths."""

    def __init__(self, out_dir):
        self.out = Path(out_dir)
        self.written: list[str] = []

    def write(self, rel: str, content: str, executable: bool = False) -> None:
        path = self.out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if executable:
            path.chmod(0o755)
        self.written.append(rel)
