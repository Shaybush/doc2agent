from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from doc2agent.models.document import AnalyzedDocument, Section
from doc2agent.semantic.enrichment_patch import (
    EnrichmentPatch,
    EnrichmentPatchValidator,
)

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-v4-pro"


class EnrichmentProvider(Protocol):
    """A model adapter that proposes one constrained enrichment patch."""

    name: str

    def create_patch(self, document: AnalyzedDocument) -> EnrichmentPatch: ...


class JsonTransport(Protocol):
    def post(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]: ...


class UrllibJsonTransport:
    """Small standard-library transport for OpenAI-compatible chat endpoints."""

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds

    def post(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"Model API returned HTTP {exc.code}") from exc
        except TimeoutError as exc:
            raise RuntimeError("Model API request timed out") from exc
        except URLError as exc:
            raise RuntimeError("Model API connection failed") from exc


@dataclass(frozen=True)
class OpenAICompatibleSettings:
    """Configuration for OpenAI-compatible chat-completions APIs."""

    name: str
    api_key: str
    base_url: str
    model: str
    timeout_seconds: float = 60.0


class OpenAICompatibleEnrichmentProvider:
    """Provider-neutral adapter for APIs returning OpenAI chat completions JSON."""

    def __init__(
        self,
        settings: OpenAICompatibleSettings,
        *,
        transport: JsonTransport | None = None,
        patch_validator: EnrichmentPatchValidator | None = None,
    ) -> None:
        if not settings.api_key.strip():
            raise ValueError("Model API key must not be empty")
        if not settings.base_url.startswith(("https://", "http://")):
            raise ValueError("Model base URL must start with http:// or https://")
        self.settings = settings
        self.name = settings.name
        self.transport = transport or UrllibJsonTransport(settings.timeout_seconds)
        self.patch_validator = patch_validator or EnrichmentPatchValidator()

    def create_patch(self, document: AnalyzedDocument) -> EnrichmentPatch:
        if document.source_manifest is None or not document.structural_sha256:
            raise ValueError("Enrichment requires source manifest and structural hash")

        request_body: dict[str, Any] = {
            "model": self.settings.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": self._system_prompt(document)},
                {
                    "role": "user",
                    "content": document.model_dump_json(exclude_none=True),
                },
            ],
        }
        if self.settings.name == "deepseek":
            request_body["thinking"] = {"type": "disabled"}

        response = self.transport.post(
            f"{self.settings.base_url.rstrip('/')}/chat/completions",
            {
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json",
            },
            request_body,
        )
        content = self._response_content(response)
        patch = EnrichmentPatch.model_validate_json(content).model_copy(
            update={"scope_block_ids": self._block_ids(document)}
        )
        report = self.patch_validator.validate(patch, document)
        if not report.valid:
            codes = ", ".join(issue.code for issue in report.errors)
            raise ValueError(f"Model returned an invalid enrichment patch: {codes}")
        return patch

    @staticmethod
    def _response_content(response: dict[str, Any]) -> str:
        try:
            content = response["choices"][0]["message"]["content"]
        except (IndexError, KeyError, TypeError) as exc:
            raise ValueError("Model response does not contain chat completion content") from exc
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Model response contains empty JSON content")
        return content

    @staticmethod
    def _system_prompt(document: AnalyzedDocument) -> str:
        block_ids = OpenAICompatibleEnrichmentProvider._block_ids(document)
        return (
            "Return only one JSON object matching this exact shape: "
            '{"schema_version":"2.0","source_id":"...",'
            '"structural_sha256":"...","scope_block_ids":["..."],'
            '"reviewed_block_ids":["..."],'
            '"facts":[{"id":"...","kind":"purpose|requirement|decision|action_item|architecture_pipeline",'
            '"value":"...","pipeline_step":{"component":"...","input":"...","output":"..."},'
            '"evidence":[{"source_id":"...","block_id":"...",'
            '"structural_sha256":"...","support":"explicit"}]}]}. '
            "Exhaustively review every supplied content block and extract all relevant "
            "explicit requirements, decisions, constraints, and architecture or pipeline "
            "steps. The value must "
            "be an exact phrase from its evidence block, in the source language and script; "
            "never translate, paraphrase, or summarize it. Do not infer or use facts without "
            "evidence. Exclude test, example, and fixture details. Treat explicit "
            "goals and purpose statements as purpose. Treat expected results, constraints, "
            "and required capabilities as requirements. Treat an explicit choice, prohibition, "
            "or statement that a "
            "component is not needed as a decision. Treat an explicitly assigned future "
            "task as an action_item only when it tells a person or team to do something. "
            "A description of an existing or expected system architecture or pipeline step, "
            "such as 'Converter creates Markdown', is architecture_pipeline, never an "
            "action_item. Inspect every table. For every non-header row whose headers mean "
            "Component, Input, and Output (including translated headers), produce one "
            "architecture_pipeline fact with a "
            "pipeline_step whose component, input, and output exactly match those cells, "
            "using the exact cell text joined in reading order with single spaces as its "
            "value and the table block as evidence (for example, 'Component Input Output'). "
            "Do not include pipeline_step for ordinary architecture_pipeline facts. "
            "Split independent explicit "
            "claims in the same evidence block into separate facts; when a combined "
            "paragraph contains an explicit normative sentence and a decision, extract the "
            "normative sentence as a requirement separately from the decision, preserving "
            "each sentence's exact wording. "
            "Before output, self-check that every table was inspected, every applicable "
            "Component/Input/Output row has an architecture_pipeline fact, and independent "
            "claims were split. Documents may be "
            "multilingual, including Hebrew RTL text. "
            "Use the exact "
            f"source_id {document.source_manifest.source_id}, structural_sha256 "
            f"{document.structural_sha256}, and only these content block IDs: {block_ids}. "
            "reviewed_block_ids must contain every listed content block ID exactly once; it "
            "declares coverage and is not evidence scope. If there are no supported facts, "
            "return an empty facts array while still listing every content block ID in "
            "reviewed_block_ids."
        )

    @staticmethod
    def _block_ids(document: AnalyzedDocument) -> list[str]:
        block_ids = [block.id for block in document.preamble]

        def collect(section: Section) -> None:
            block_ids.extend(block.id for block in section.blocks)
            for child in section.children:
                collect(child)

        for section in document.sections:
            collect(section)
        if not block_ids:
            raise ValueError("Enrichment requires at least one content block")
        return block_ids


def deepseek_settings_from_environment(
    *,
    model: str | None = None,
    base_url: str | None = None,
) -> OpenAICompatibleSettings:
    """Build DeepSeek settings without exposing the API key in CLI arguments."""

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY is not configured")
    return OpenAICompatibleSettings(
        name="deepseek",
        api_key=api_key,
        base_url=base_url or os.environ.get("DOC2AGENT_LLM_BASE_URL", DEEPSEEK_BASE_URL),
        model=model or os.environ.get("DOC2AGENT_LLM_MODEL", DEEPSEEK_MODEL),
    )


def openai_compatible_settings_from_environment(
    *,
    name: str,
    api_key_environment: str,
    base_url: str,
    model: str,
) -> OpenAICompatibleSettings:
    """Build settings for any compatible endpoint from a named environment key."""

    api_key = os.environ.get(api_key_environment, "")
    if not api_key:
        raise ValueError(f"{api_key_environment} is not configured")
    return OpenAICompatibleSettings(
        name=name,
        api_key=api_key,
        base_url=base_url,
        model=model,
    )
