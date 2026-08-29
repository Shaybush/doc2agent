from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from doc2agent.analyzer import StructuralAnalyzer
from doc2agent.semantic import (
    OpenAICompatibleEnrichmentProvider,
    OpenAICompatibleSettings,
)
from doc2agent.source_manifest import SourceManifest

SAMPLE = Path(__file__).parent.parent / "examples" / "sample.md"


class FakeTransport:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    def post(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((url, headers, body))
        return self.response


def _document():
    return StructuralAnalyzer().analyze(
        SAMPLE,
        source_manifest=SourceManifest.from_path(SAMPLE),
    )


def _provider(transport: FakeTransport) -> OpenAICompatibleEnrichmentProvider:
    return OpenAICompatibleEnrichmentProvider(
        OpenAICompatibleSettings(
            name="test-compatible",
            api_key="test-key",
            base_url="https://example.test/v1",
            model="test-model",
        ),
        transport=transport,
    )


def test_openai_compatible_provider_creates_valid_patch() -> None:
    document = _document()
    block_id = document.sections[0].blocks[0].id
    patch_json = {
        "schema_version": "1.0",
        "source_id": document.source_manifest.source_id,
        "structural_sha256": document.structural_sha256,
        "scope_block_ids": [block_id],
        "facts": [
            {
                "id": "fact:requirement",
                "kind": "requirement",
                "value": "General English paragraph.",
                "evidence": [
                    {
                        "source_id": document.source_manifest.source_id,
                        "block_id": block_id,
                        "structural_sha256": document.structural_sha256,
                        "support": "explicit",
                    }
                ],
            }
        ],
    }
    transport = FakeTransport(
        {"choices": [{"message": {"content": json.dumps(patch_json)}}]}
    )

    patch = _provider(transport).create_patch(document)

    assert patch.facts[0].id == "fact:requirement"
    url, headers, body = transport.requests[0]
    assert url == "https://example.test/v1/chat/completions"
    assert headers["Authorization"] == "Bearer test-key"
    assert body["response_format"] == {"type": "json_object"}
    assert body["model"] == "test-model"
    assert "thinking" not in body


def test_deepseek_profile_disables_thinking_for_json_extraction() -> None:
    document = _document()
    block_id = document.sections[0].blocks[0].id
    transport = FakeTransport(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "source_id": document.source_manifest.source_id,
                                "structural_sha256": document.structural_sha256,
                                "scope_block_ids": [block_id],
                                "facts": [],
                            }
                        )
                    }
                }
            ]
        }
    )
    provider = OpenAICompatibleEnrichmentProvider(
        OpenAICompatibleSettings(
            name="deepseek",
            api_key="test-key",
            base_url="https://example.test",
            model="deepseek-v4-pro",
        ),
        transport=transport,
    )

    provider.create_patch(document)

    assert transport.requests[0][2]["thinking"] == {"type": "disabled"}


def test_enrichment_prompt_defines_explicit_goal_and_decision_extraction() -> None:
    prompt = OpenAICompatibleEnrichmentProvider._system_prompt(_document())

    assert "expected results" in prompt
    assert "never translate, paraphrase" in prompt
    assert "not needed as a decision" in prompt
    assert "is a requirement, never an action_item" in prompt
    assert "Hebrew RTL text" in prompt


def test_openai_compatible_provider_rejects_invalid_model_patch() -> None:
    document = _document()
    transport = FakeTransport(
        {"choices": [{"message": {"content": "{\"facts\": []}"}}]}
    )

    with pytest.raises(ValueError):
        _provider(transport).create_patch(document)


def test_provider_uses_document_scope_when_model_omits_evidence_block() -> None:
    document = _document()
    scope_block_id = document.preamble[0].id
    evidence_block_id = document.sections[0].blocks[0].id
    transport = FakeTransport(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "source_id": document.source_manifest.source_id,
                                "structural_sha256": document.structural_sha256,
                                "scope_block_ids": [scope_block_id],
                                "facts": [
                                    {
                                        "id": "fact:requirement",
                                        "kind": "requirement",
                                        "value": document.sections[0].blocks[0].text,
                                        "evidence": [
                                            {
                                                "source_id": document.source_manifest.source_id,
                                                "block_id": evidence_block_id,
                                                "structural_sha256": document.structural_sha256,
                                                "support": "explicit",
                                            }
                                        ],
                                    }
                                ],
                            }
                        )
                    }
                }
            ]
        }
    )

    patch = _provider(transport).create_patch(document)

    assert evidence_block_id in patch.scope_block_ids
    assert scope_block_id in patch.scope_block_ids


def test_openai_compatible_provider_rejects_empty_response_content() -> None:
    with pytest.raises(ValueError, match="empty JSON content"):
        provider = _provider(
            FakeTransport({"choices": [{"message": {"content": ""}}]})
        )
        provider.create_patch(_document())
