"""Session-scoped image preparation and compatibility-adapter behavior."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from kohakuterrarium.core.controller_messages import prepare_session_media
from kohakuterrarium.errors import MediaReferenceError
from kohakuterrarium.llm import artifact_resolve
from kohakuterrarium.llm.media_reference import resolve_session_image_urls


async def test_session_root_preparation_preserves_parts_and_rejects_namespace_escape(
    tmp_path, monkeypatch
):
    default_root = tmp_path / "default"
    default_root.mkdir()
    monkeypatch.setattr(artifact_resolve, "_session_dir", lambda: default_root)
    local = tmp_path / "other" / "own session.kohakutr"
    artifacts = local.with_suffix(".artifacts")
    artifacts.mkdir(parents=True)
    (artifacts / "pic.png").write_bytes(b"OWN")
    url = "/api/sessions/own%20session/artifacts/pic.png"
    messages = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": url, "vendor": "keep"},
                    "extension": ["keep"],
                },
                {"type": "image_url", "image_url": url},
            ],
        }
    ]
    original = deepcopy(messages)
    prepared = resolve_session_image_urls(messages, session_path=local)
    expected = deepcopy(messages)
    expected[0]["content"][0]["image_url"]["url"] = "data:image/png;base64,T1dO"
    expected[0]["content"][1]["image_url"] = "data:image/png;base64,T1dO"
    assert prepared == expected
    controller = SimpleNamespace(session_store=SimpleNamespace(path=local))
    shorthand = [{"role": "assistant", "content": messages[0]["content"][1:]}]
    assert await prepare_session_media(controller, shorthand) == [
        {"role": "assistant", "content": expected[0]["content"][1:]}
    ]
    assert (
        artifact_resolve.resolve_message_image_urls(messages, session_path=local)
        == expected
    )
    assert (
        artifact_resolve.resolve_artifact_url(url, session_path=local)
        == "data:image/png;base64,T1dO"
    )
    assert messages == original
    with pytest.raises(MediaReferenceError, match="unavailable"):
        artifact_resolve.resolve_message_image_urls(messages)
    outside = tmp_path / "outside.artifacts"
    outside.mkdir()
    (outside / "pic.png").write_bytes(b"OUTSIDE")
    escaped = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "/api/sessions/%2E%2E%2Foutside/artifacts/pic.png"
                    },
                }
            ],
        }
    ]
    with pytest.raises(MediaReferenceError, match="unavailable"):
        artifact_resolve.resolve_message_image_urls(escaped)
    with pytest.raises(MediaReferenceError, match="unavailable"):
        resolve_session_image_urls(escaped, session_path=local)
