"""Fail the build when a retailer identity or a widget media API leaks.

The deny-list is the ``NEUTRALITY_DENYLIST`` environment variable, one token
per line or comma. It is a deployment secret, so this file does not contain
a retailer name. With no list configured the text scan has nothing to find
and still checks that the Release 1 widget cannot ask for a microphone,
camera, speaker or speech synthesis.

Audio transcription and image OCR are not run here: they need models this
process does not ship. The text, SVG and widget-script checks are the ones
that can fail a commit.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEXT_ROOTS = ("src", "docs", "tests", ".env.example")
WIDGET = ROOT / "src" / "web" / "static" / "widget"
# Release 1 must not be able to play or record, even if a later edit tries.
FORBIDDEN_WIDGET = (
    "getUserMedia",
    "AudioContext",
    "webkitAudioContext",
    "speechSynthesis",
    "new Audio",
    "HTMLMediaElement",
)
# The loader is on the host page before a tap. It must not open a socket either.
FORBIDDEN_LOADER = FORBIDDEN_WIDGET + ("WebSocket", "fetch(")


def denylist_from_env(raw: str | None = None) -> tuple[str, ...]:
    source = os.environ.get("NEUTRALITY_DENYLIST", "") if raw is None else raw
    tokens = []
    for line in source.replace(",", "\n").splitlines():
        token = line.strip().casefold()
        if token:
            tokens.append(token)
    return tuple(dict.fromkeys(tokens))


def _iter_text_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    found: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(
            part in {".git", ".venv", "recordings", "__pycache__"}
            for part in path.parts
        ):
            continue
        if path.suffix.lower() in {
            ".png",
            ".avif",
            ".wav",
            ".pcm",
            ".mp3",
            ".jpg",
            ".webp",
            ".gif",
        }:
            continue
        found.append(path)
    return found


def scan_text(paths: list[Path], denylist: tuple[str, ...]) -> list[str]:
    hits: list[str] = []
    if not denylist:
        return hits
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        folded = text.casefold()
        for token in denylist:
            if token in folded:
                hits.append(f"{path}: {token}")
    return hits


def scan_widget(widget: Path = WIDGET) -> list[str]:
    hits: list[str] = []
    loader = widget / "sa-loader.js"
    if loader.exists():
        text = loader.read_text(encoding="utf-8")
        for token in FORBIDDEN_LOADER:
            if token in text:
                hits.append(f"{loader.name}: {token}")
    for path in widget.glob("*.js"):
        if path.name == "sa-loader.js":
            continue
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_WIDGET:
            if token in text:
                hits.append(f"{path.name}: {token}")
    html = widget / "index.html"
    if html.exists():
        page = html.read_text(encoding="utf-8")
        for token in FORBIDDEN_WIDGET:
            if token in page:
                hits.append(f"index.html: {token}")
    return hits


def collect(root: Path = ROOT, denylist: tuple[str, ...] | None = None) -> list[str]:
    tokens = denylist_from_env() if denylist is None else denylist
    files: list[Path] = []
    for name in TEXT_ROOTS:
        target = root / name
        if target.exists():
            files.extend(_iter_text_files(target))
    return scan_text(files, tokens) + scan_widget(
        root / "src" / "web" / "static" / "widget"
    )


def main() -> int:
    hits = collect()
    if not hits:
        print("neutrality scan: no hits")
        return 0
    print("neutrality scan failed:", file=sys.stderr)
    for hit in hits:
        print(f"  {hit}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
