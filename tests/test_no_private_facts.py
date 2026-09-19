"""R5: no private facts in the public repo.

Scans every text file git would publish — tracked, plus untracked-but-not-ignored
— for markers of the author's private fleet, credential, or state machinery. The
GitHub *owner name in repo URLs* is fine (the repo is public by design); local
machine state and private tooling are not.

Ignored paths are deliberately out of scope: they never reach the public repo,
so scanning them yields only false positives (the local fleet state dir, the
embedded issue database, and caches all legitimately hold absolute paths).
"""

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

FORBIDDEN = [
    (re.compile(r"\.local/state"), "local state dir"),
    (re.compile(r"op://"), "1Password secret reference"),
    (re.compile(r"/Users/"), "absolute home path"),
    (re.compile(r"1[Pp]assword"), "credential tooling"),
    (re.compile(r"\borca terminal\b"), "private fleet CLI"),
    (re.compile(r"pinned-projects"), "private fleet tooling"),
    (re.compile(r"sitterlib|pi-sitter"), "private sibling repo"),
]

TEXT_SUFFIXES = {
    ".py", ".json", ".md", ".txt", ".sh", ".plist", ".toml", ".yaml",
    ".yml", ".cfg", ".example", ".ts",
}
TEXT_NAMES = {"LICENSE", ".gitignore"}


def iter_text_files():
    """Text files git would publish: tracked, plus untracked and not ignored."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    self_path = Path(__file__).resolve()
    for rel in listed.split("\0"):
        if not rel:
            continue
        path = REPO / rel
        if path.resolve() == self_path:
            continue  # this file literally contains the forbidden patterns
        if not path.is_file():
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in TEXT_NAMES:
            yield path


def test_no_private_facts():
    hits = []
    for path in iter_text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern, label in FORBIDDEN:
            for lineno, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    hits.append(
                        f"{path.relative_to(REPO)}:{lineno}: {label}: {line.strip()[:80]}"
                    )
    assert not hits, "private facts found:\n" + "\n".join(hits)
