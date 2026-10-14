"""Publication policy regressions; never call GitHub or mutate real releases."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import publish_release


def publication_stub(monkeypatch, published, latest_id=42):
    calls = []
    monkeypatch.setattr(publish_release, "execute", lambda *args: calls.append(args))

    def output(*args):
        if args[-1].endswith("/latest"):
            return json.dumps({"id": latest_id})
        return json.dumps(published)

    monkeypatch.setattr(publish_release, "output", output)
    return calls


def test_beta_publication_sets_latest_without_changing_beta_tag(monkeypatch):
    release = {"id": 42, "tag_name": "v2.0.0-beta.6", "draft": False, "prerelease": False}
    calls = publication_stub(monkeypatch, release)
    assert publish_release.publish_as_latest(release["tag_name"], 42) == release
    assert len(calls) == 1
    assert calls[0][3] == release["tag_name"]
    assert set(calls[0][-3:]) == {"--draft=false", "--prerelease=false", "--latest=true"}


@pytest.mark.parametrize("change,latest_id", [({"draft": True}, 42), ({"prerelease": True}, 42),
                                           ({"tag_name": "v2.0.0-beta.5"}, 42), ({}, 41)])
def test_publication_rejects_wrong_state_or_wrong_latest(monkeypatch, change, latest_id):
    release = {"id": 42, "tag_name": "v2.0.0-beta.6", "draft": False, "prerelease": False, **change}
    publication_stub(monkeypatch, release, latest_id)
    with pytest.raises(RuntimeError, match="Latest readback"):
        publish_release.publish_as_latest("v2.0.0-beta.6", 42)
