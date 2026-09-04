"""Normalized hand-off contract from a Figma MCP capture to pipeline storage."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from doc2agent.design.alignment import FigmaNode, FigmaSnapshot, validate_figma_url
from doc2agent.design.import_request import figma_design_file_key


class FigmaCaptureNode(BaseModel):
    """One node selected by the MCP capture adapter, without semantic meaning."""

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: str = Field(min_length=1)


class FigmaMcpCaptureManifest(BaseModel):
    """Multi-node Figma MCP capture that can be turned into one immutable snapshot."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    figma_url: str = Field(min_length=1)
    captured_at: datetime
    nodes: list[FigmaCaptureNode] = Field(min_length=1)
    adapter: str = Field(default="figma-mcp:get_metadata", min_length=1)

    @model_validator(mode="after")
    def validate_capture(self) -> FigmaMcpCaptureManifest:
        validate_figma_url(self.figma_url)
        figma_design_file_key(self.figma_url)
        if len({node.node_id for node in self.nodes}) != len(self.nodes):
            raise ValueError("Figma capture manifest has duplicate node IDs")
        return self

    def build_snapshot(self) -> FigmaSnapshot:
        return FigmaSnapshot.build(
            figma_file_key=figma_design_file_key(self.figma_url),
            figma_url=self.figma_url,
            nodes=[
                FigmaNode(
                    node_id=node.node_id,
                    name=node.name,
                    kind=node.kind,
                    figma_url=_node_url(self.figma_url, node.node_id),
                )
                for node in self.nodes
            ],
            adapter=self.adapter,
            captured_at=self.captured_at,
        )


def _node_url(figma_url: str, node_id: str) -> str:
    """Return a canonical node link while preserving the selected design file."""

    parsed = urlparse(figma_url)
    query = [(key, value) for key, value in parse_qsl(parsed.query) if key != "node-id"]
    query.append(("node-id", node_id.replace(":", "-")))
    return urlunparse(parsed._replace(query=urlencode(query)))
