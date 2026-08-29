from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from doc2agent.models.document import AnalyzedDocument
from doc2agent.semantic.models import SemanticAnnotation


class SemanticProvider(Protocol):
    """A provider that produces evidence-linked semantic annotations."""

    name: str

    def enrich(
        self,
        document: AnalyzedDocument,
    ) -> list[SemanticAnnotation]: ...


@dataclass(frozen=True)
class NoOpSemanticProvider:
    """Default provider that preserves the semantic boundary without inference."""

    name: str = "noop"

    def enrich(
        self,
        document: AnalyzedDocument,
    ) -> list[SemanticAnnotation]:
        return []


@dataclass(frozen=True)
class FixtureSemanticProvider:
    """Deterministic provider for contract tests and local examples."""

    annotations: tuple[SemanticAnnotation, ...]
    name: str = "fixture"

    def enrich(
        self,
        document: AnalyzedDocument,
    ) -> list[SemanticAnnotation]:
        return [
            annotation.model_copy(deep=True)
            for annotation in self.annotations
        ]
