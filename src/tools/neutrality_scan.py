"""Fail the build when a retailer identity or a widget media API leaks.

The built-in fingerprints cover retailer names and domain labels even in a
clean checkout. NEUTRALITY_DENYLIST adds deployment-specific strings. Both
checks apply to repository text and OCR output.

Raster images are read with tesseract when that binary is installed. CI
installs it, so a word painted into a PNG fails the build the same way a
word in a source file does. Without the binary the scan says it skipped
OCR instead of pretending it looked.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess  # nosec B404
import sys
import unicodedata
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Fingerprints of case-folded names, common spoken spellings and domains.
_FORBIDDEN = frozenset(
    {
        "1a1c5924714f78b15c8ac403352b8deb0063b97a70e2233e890a92829d42a77c",
        "b0a60e13f5f4f35f2c6cb97cac6db0a28e27f18ba7bd8cdc31809b5cbc1af026",
        "6a94a53f6132b1154388c5d8219eca983cef9929283a3b8bce92cd201dbae96d",
        "643c6a2472fca16a4499931f0288ab2edfa69777da8d5d17c10c68a39eb7e8ec",
        "2ad7bbf80f49ddb1d2999026a2a0a75ba0b758a41adc5a846ae7f7354e37ed7d",
        "072440ee4984df07cc5871b40f1dd5c244c193d9f6e92f0316c7c19a02eb8c8c",
        "77e10d23c0f6ecdac828fb455c962237c2c16b82b6bd06307294c3040c1931a9",
    }
)
_TOKEN = re.compile(r"[a-z0-9]+(?:\.[a-z0-9]+)*|[\u0b80-\u0bff]+")
_SKIP = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".aws",
    ".codex",
    ".agents",
    "node_modules",
    "out",
    "attic",
}
_RASTER = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff"}
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


def _repository_files(root: Path) -> list[Path]:
    paths: Iterator[Path]
    if (root / ".git").exists():
        # Include unstaged additions so the same check works before committing.
        git = shutil.which("git")
        if git is None:
            raise RuntimeError("git is required to list checkout files")
        listed = subprocess.check_output(  # nosec B603
            [git, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
        )
        paths = (root / os.fsdecode(name) for name in listed.split(b"\0") if name)
    else:
        # Source archives and the small standalone trees used in tests.
        files: list[Path] = []
        for directory, dirs, names in os.walk(root):
            dirs[:] = [name for name in dirs if name not in _SKIP]
            files.extend(Path(directory) / name for name in names)
        paths = iter(files)
    return [
        p
        for p in paths
        if p.is_file()
        and not p.is_symlink()
        and (p.name == ".env.example" or not p.name.startswith(".env"))
    ]


def _matches(text: str, denylist: tuple[str, ...]) -> list[str]:
    folded = unicodedata.normalize("NFKC", text).casefold()
    hits = [name for name in denylist if name in folded]
    tokens = set(_TOKEN.findall(folded))
    for host in tuple(tokens):
        if "." in host:
            labels = host.split(".")
            tokens.update(labels)
            tokens.update(".".join(pair) for pair in zip(labels, labels[1:]))
    if any(hashlib.sha256(word.encode()).hexdigest() in _FORBIDDEN for word in tokens):
        hits.append("forbidden retailer identity")
    return hits


def scan_text(paths: list[Path], denylist: tuple[str, ...]) -> list[str]:
    hits: list[str] = []
    for path in paths:
        if path.suffix.lower() in _RASTER | {".wav", ".pcm", ".mp3"}:
            continue
        raw = path.read_bytes()
        if b"\0" in raw:
            continue
        text = raw.decode("utf-8", errors="replace")
        if path.name == "manifest.json":
            # Decode JSON escapes in spoken text and read-back transcripts.
            text = json.dumps(json.loads(text), ensure_ascii=False)
        hits.extend(f"{path}: {hit}" for hit in _matches(text, denylist))
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


def scan_svg(root: Path) -> list[str]:
    """Executable content inside an SVG. Text deny-list hits are caught by scan_text."""
    if not root.exists():
        return []
    hits: list[str] = []
    for path in root.rglob("*.svg"):
        if any(part in {".git", ".venv"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        if "<script" in text:
            hits.append(f"{path.name}: script element")
    return hits


def scan_audio_metadata(root: Path) -> list[str]:
    """A URL stuffed into a RIFF or ID3 header. Recorded prompts are skipped."""
    if not root.exists():
        return []
    hits: list[str] = []
    for pattern in ("*.wav", "*.mp3"):
        for path in root.rglob(pattern):
            if any(part in {".git", ".venv", "recordings"} for part in path.parts):
                continue
            try:
                blob = path.read_bytes()[:4096]
            except OSError:
                continue
            decoded = blob.decode("latin-1", errors="ignore")
            if "http://" in decoded or "https://" in decoded:
                hits.append(f"{path.name}: url in metadata")
    return hits


def _raster_files(root: Path) -> list[Path]:
    return [p for p in _repository_files(root) if p.suffix.lower() in _RASTER]


def _ocr(path: Path) -> tuple[str, str | None]:
    """Read one raster. The second value is set when tesseract itself failed."""
    # Absolute path from PATH, and a fixed argument list. No shell.
    binary = shutil.which("tesseract")
    if binary is None:
        return "", "tesseract is not installed"
    try:
        completed = subprocess.run(  # nosec B603
            [binary, str(path), "stdout", "-l", "eng"],
            check=False,
            capture_output=True,
            timeout=30,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as failed:
        return "", str(failed)
    if completed.returncode != 0 and not (completed.stdout or "").strip():
        err = (completed.stderr or "").strip().splitlines()
        return "", err[0] if err else f"tesseract exited {completed.returncode}"
    return completed.stdout or "", None


def scan_images(root: Path, denylist: tuple[str, ...]) -> list[str]:
    """OCR every raster under the scanned roots. No binary means no hits and a skip note."""
    if shutil.which("tesseract") is None:
        return []
    hits: list[str] = []
    for path in _raster_files(root):
        text, failed = _ocr(path)
        if failed:
            hits.append(f"{path}: ocr failed ({failed})")
            continue
        hits.extend(f"{path}: ocr {hit}" for hit in _matches(text, denylist))
    return hits


def optional_tools(root: Path = ROOT) -> list[str]:
    """OCR and audio decode are used when the binary is installed, and named when it is not."""
    notes = []
    if shutil.which("tesseract") is None:
        notes.append("skipped: tesseract is not installed (image OCR)")
    else:
        count = len(_raster_files(root))
        noun = "file" if count == 1 else "files"
        notes.append(f"ran: tesseract (image OCR, {count} raster {noun})")
    if shutil.which("ffmpeg") is None:
        notes.append("skipped: ffmpeg is not installed (audio decode)")
    return notes


def collect(root: Path = ROOT, denylist: tuple[str, ...] | None = None) -> list[str]:
    tokens = denylist_from_env() if denylist is None else denylist
    files = _repository_files(root)
    return (
        scan_text(files, tokens)
        + scan_widget(root / "src" / "web" / "static" / "widget")
        + scan_svg(root / "src")
        + scan_audio_metadata(root / "src")
        + scan_images(root, tokens)
    )


def main() -> int:
    for note in optional_tools():
        print(note)
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
