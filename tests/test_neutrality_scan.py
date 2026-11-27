import hashlib
import json

import pytest

from src.tools import neutrality_scan as scan


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("README.md", "A NORTHWIND device"),
        ("vendor/store/client.py", 'URL = "https://api.northwind.example/v1"'),
        ("assets/logo.svg", "<svg><text>Northwind</text></svg>"),
        ("src/guided/recordings/manifest.json", '{"en":{"heard":"Northwind"}}'),
    ],
)
def test_builtin_fingerprint_catches_release_content(
    tmp_path, monkeypatch, path, content
):
    monkeypatch.delenv("NEUTRALITY_DENYLIST", raising=False)
    monkeypatch.setattr(scan, "_FORBIDDEN", {hashlib.sha256(b"northwind").hexdigest()})
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    assert scan.collect(tmp_path) == [f"{target}: forbidden retailer identity"]


def test_production_fingerprints_cover_names_and_domains():
    name = "poor" + "vika"
    for word in (name, name + ".com", name + ".in"):
        assert hashlib.sha256(word.encode()).hexdigest() in scan._FORBIDDEN
    assert scan._matches("https://stage-" + name.upper() + ".com", ())


def test_escaped_transcript_is_checked_but_local_files_are_skipped(
    tmp_path, monkeypatch
):
    word = "கடை"
    monkeypatch.setattr(scan, "_FORBIDDEN", {hashlib.sha256(word.encode()).hexdigest()})
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"step": {"text": word}}))
    (tmp_path / ".env").write_text(word)
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "notes.txt").write_text(word)
    assert scan.collect(tmp_path) == [f"{manifest}: forbidden retailer identity"]
