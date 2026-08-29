from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from doc2agent.source_manifest import SourceManifest

Direction = Literal["ltr", "rtl", "mixed", "auto"]

BlockType = Literal[
    "paragraph",
    "unordered_list",
    "ordered_list",
    "code",
    "table",
    "image",
]

Origin = Literal[
    "SOURCE",
    "VERIFIED",
    "DERIVED",
    "AI_INFERRED",
]


class ContentBlock(BaseModel):
    """A deterministic content block extracted from source Markdown."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: BlockType

    # Used by paragraphs and code blocks.
    text: str | None = None

    # Used by fenced code blocks, for example:
    # ```python
    info: str | None = None

    # Used by ordered/unordered lists.
    items: list[str] = Field(default_factory=list)

    # Used by images.
    alt: str | None = None
    src: str | None = None

    # Used by Markdown tables.
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)

    language: str = "und"
    direction: Direction = "auto"

    origin: Origin = "SOURCE"

    start_line: int
    end_line: int


class Section(BaseModel):
    """A Markdown heading and the content that belongs directly to it."""

    model_config = ConfigDict(extra="forbid")

    id: str

    level: int = Field(
        ge=1,
        le=6,
    )

    title: str

    path: list[str]

    language: str = "und"
    direction: Direction = "auto"

    origin: Origin = "SOURCE"

    start_line: int
    end_line: int

    blocks: list[ContentBlock] = Field(
        default_factory=list
    )

    children: list[Section] = Field(
        default_factory=list
    )


class DocumentInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str

    title: str

    source: str
    source_format: str

    markdown: str

    language: str = "und"
    direction: Direction = "auto"


class AnalyzedDocument(BaseModel):
    """
    Canonical deterministic structure produced
    before any LLM enrichment.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"

    source_manifest: SourceManifest | None = None

    structural_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    document: DocumentInfo

    preamble: list[ContentBlock] = Field(
        default_factory=list
    )

    sections: list[Section] = Field(
        default_factory=list
    )
