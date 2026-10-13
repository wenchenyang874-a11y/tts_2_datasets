import importlib
import json
from pathlib import Path

import pytest


def publisher(monkeypatch, pages):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    module = importlib.import_module("publish_release")
    monkeypatch.setattr(module, "output", lambda *command: json.dumps(pages))
    return module


def test_draft_found_by_tag_on_later_page_despite_placeholder_url(monkeypatch):
    draft = {"id": 123, "draft": True, "tag_name": "v2.0.0-beta.1", "html_url": "https://github.com/example/repo/releases/tag/untagged-example"}
    module = publisher(monkeypatch, [[{"tag_name": "v1.0.0"}], [draft]])
    assert module.release_for_tag("v2.0.0-beta.1") == draft


def test_absent_release_is_not_confused_with_draft(monkeypatch):
    module = publisher(monkeypatch, [[{"tag_name": "v1.0.0"}]])
    assert module.release_for_tag("v2.0.0-beta.1") is None


def test_duplicate_drafts_stop_before_upload(monkeypatch):
    draft = {"tag_name": "v2.0.0-beta.1", "draft": True}
    module = publisher(monkeypatch, [[draft], [draft]])
    with pytest.raises(RuntimeError, match="Multiple releases"):
        module.release_for_tag("v2.0.0-beta.1")
