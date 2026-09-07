from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from doc2agent.design.alignment import ApprovedDesignAlignment, DesignAlignmentBuilder
from doc2agent.semantic.candidate import KnowledgeCandidate
from doc2agent.semantic.candidate_review import CandidateReviewer
from doc2agent.semantic.models import (
    COPILOT_CONTEXT_KINDS,
    COPILOT_CONTEXT_LABELS,
    SemanticAnnotation,
    copilot_fact_value,
)


def copilot_context_output_path(
    approved: KnowledgeCandidate,
    storage_root: str | Path,
    document_id: str,
) -> Path:
    """Return the stable published context location for an approved document."""

    manifest = approved.document.source_manifest
    if manifest is None:
        raise ValueError("Approved knowledge is missing its source manifest")
    storage_root = Path(storage_root)
    source_stem = _safe_path_component(Path(manifest.filename).stem)
    return storage_root / "copilot-context" / (
        f"{source_stem}--{_safe_path_component(document_id)}"
    )


def _safe_path_component(value: str) -> str:
    compact = re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE).strip(".-")
    return compact or "document"


@dataclass(frozen=True)
class CopilotContextResult:
    output_root: Path
    context_path: Path
    manifest_path: Path
    approved_candidate_sha256: str


@dataclass(frozen=True)
class PreparedCopilotContext:
    """An immutable context version that has not yet been made current."""

    output_root: Path
    version_path: Path
    context_filename: str
    approved_candidate_sha256: str


class TopicRenderer:
    """Render a deterministic Copilot context only from an approved snapshot."""

    renderer_version = "8"

    def render(
        self,
        approved: KnowledgeCandidate,
        output_root: str | Path = "storage/copilot-context",
        design_alignment: ApprovedDesignAlignment | None = None,
    ) -> CopilotContextResult:
        prepared = self.prepare(approved, output_root, design_alignment)
        return self.activate_prepared(prepared)

    def prepare(
        self,
        approved: KnowledgeCandidate,
        output_root: str | Path = "storage/copilot-context",
        design_alignment: ApprovedDesignAlignment | None = None,
    ) -> PreparedCopilotContext:
        """Write or verify an immutable context version without activating it."""

        CandidateReviewer().review(approved)
        self._validate_design_alignment(approved, design_alignment)
        output_root = Path(output_root)
        version_root = output_root.parent / f".{output_root.name}-versions"
        version_path = version_root / self._version_name(approved, design_alignment)
        context_filename = self._context_filename(approved)
        self._ensure_version(version_path, approved, context_filename, design_alignment)
        return PreparedCopilotContext(
            output_root=output_root,
            version_path=version_path,
            context_filename=context_filename,
            approved_candidate_sha256=approved.candidate_sha256,
        )

    def activate_prepared(self, prepared: PreparedCopilotContext) -> CopilotContextResult:
        """Atomically make a previously prepared immutable version current."""

        self._activate_version(prepared.output_root, prepared.version_path)
        return CopilotContextResult(
            output_root=prepared.output_root,
            context_path=prepared.output_root / prepared.context_filename,
            manifest_path=prepared.output_root / "manifest.json",
            approved_candidate_sha256=prepared.approved_candidate_sha256,
        )

    def _ensure_version(
        self,
        version_path: Path,
        approved: KnowledgeCandidate,
        context_filename: str,
        design_alignment: ApprovedDesignAlignment | None,
    ) -> None:
        if version_path.exists():
            manifest_path = version_path / "manifest.json"
            if not manifest_path.is_file():
                raise ValueError("Existing Copilot context version is incomplete")
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("Existing Copilot context version is incomplete") from exc
            if not isinstance(manifest, dict):
                raise ValueError("Existing Copilot context version is incomplete")
            if (
                manifest.get("approved_candidate_sha256") != approved.candidate_sha256
                or manifest.get("renderer_version") != self.renderer_version
                or manifest.get("design_alignment_sha256")
                != self._design_alignment_sha256(design_alignment)
            ):
                raise ValueError("Existing Copilot context version has a mismatched hash")
            self._validate_version_files(version_path, manifest, context_filename)
            return

        version_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = version_path.with_name(f".{version_path.name}.{uuid4().hex}.tmp")
        try:
            temporary_path.mkdir()
            agent_content = self._agent_markdown(approved, design_alignment)
            index_content = self._index_markdown(approved, design_alignment)
            (temporary_path / context_filename).write_text(agent_content, encoding="utf-8")
            (temporary_path / "INDEX.md").write_text(index_content, encoding="utf-8")
            manifest = self._manifest(
                approved,
                index_content,
                agent_content,
                context_filename,
                design_alignment,
            )
            (temporary_path / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary_path, version_path)
        finally:
            if temporary_path.exists():
                shutil.rmtree(temporary_path)

    @staticmethod
    def _activate_version(output_root: Path, version_path: Path) -> None:
        if output_root.exists() and not output_root.is_symlink():
            raise ValueError(
                "Copilot context output already exists and is not generated by Doc2Agent"
            )
        output_root.parent.mkdir(parents=True, exist_ok=True)
        temporary_link = output_root.with_name(f".{output_root.name}.{uuid4().hex}.tmp")
        try:
            relative_version = os.path.relpath(version_path, output_root.parent)
            temporary_link.symlink_to(relative_version, target_is_directory=True)
            os.replace(temporary_link, output_root)
        finally:
            if temporary_link.exists() or temporary_link.is_symlink():
                temporary_link.unlink()

    @staticmethod
    def _header(
        approved: KnowledgeCandidate,
        design_alignment: ApprovedDesignAlignment | None = None,
    ) -> list[str]:
        header = [
            "<!-- GENERATED — DO NOT EDIT -->",
            f"<!-- approved_candidate_sha256: {approved.candidate_sha256} -->",
            f"<!-- source_id: {approved.source_id} -->",
        ]
        if design_alignment is not None:
            header.append(
                "<!-- design_alignment_sha256: "
                f"{design_alignment.alignment.alignment_sha256} -->"
            )
        return header

    def _index_markdown(
        self,
        approved: KnowledgeCandidate,
        design_alignment: ApprovedDesignAlignment | None,
    ) -> str:
        counts = {kind: 0 for kind in COPILOT_CONTEXT_KINDS}
        for annotation in approved.annotations:
            counts[annotation.kind] += 1
        return "\n".join(
            [
                *self._header(approved, design_alignment),
                "",
                "# Copilot context index",
                "",
                "This context is generated only from the approved knowledge snapshot.",
                "",
                "## Available context",
                "",
                f"- [Approved knowledge]({self._context_filename(approved)})",
                f"- Purpose: {counts['purpose']}",
                f"- Requirements: {counts['requirement']}",
                f"- Decisions: {counts['decision']}",
                f"- Architecture and pipeline: {counts['architecture_pipeline']}",
                f"- Action items: {counts['action_item']}",
                *(
                    ["- [Approved Figma designs](#approved-figma-designs)"]
                    if design_alignment is not None
                    else []
                ),
                "",
            ]
        )

    def _agent_markdown(
        self,
        approved: KnowledgeCandidate,
        design_alignment: ApprovedDesignAlignment | None,
    ) -> str:
        lines = [*self._header(approved, design_alignment), "", "# Approved knowledge", ""]
        annotations = sorted(
            approved.annotations,
            key=lambda item: (item.kind, item.id, item.value),
        )
        for kind in COPILOT_CONTEXT_KINDS:
            facts = [item for item in annotations if item.kind == kind]
            if not facts:
                continue
            lines.extend([f"## {COPILOT_CONTEXT_LABELS[kind]}", ""])
            for fact in facts:
                lines.extend(self.fact_markdown_lines(fact))
            lines.append("")
        if design_alignment is not None:
            lines.extend(self._design_markdown(design_alignment))
        return "\n".join(lines)

    @staticmethod
    def _design_markdown(design_alignment: ApprovedDesignAlignment) -> list[str]:
        nodes = {node.node_id: node for node in design_alignment.snapshot.nodes}
        bindings = sorted(
            (
                binding
                for binding in design_alignment.alignment.bindings
                if binding.status in {"matches", "partial"} and binding.figma_node_id
            ),
            key=lambda binding: (
                binding.figma_node_id or "",
                binding.annotation_id or "",
                binding.status,
            ),
        )
        if not bindings:
            return []
        lines = ["## Approved Figma designs", ""]
        for binding in bindings:
            node = nodes[binding.figma_node_id or ""]
            card = [
                f"### {node.name}",
                "",
                f"- Frame: [{node.name}]({node.figma_url})",
                f"- Figma node: `{node.node_id}`",
                "- Snapshot: " f"`sha256:{design_alignment.snapshot.snapshot_sha256}`",
                f"- Requirement: `{binding.annotation_id}`",
                f"- Review status: {binding.status}",
                "",
            ]
            if TopicRenderer._contains_hebrew(node.name):
                lines.extend(["<div dir=\"rtl\">", "", *card, "</div>", ""])
            else:
                lines.extend(card)
        return lines

    @staticmethod
    def _fact_value_markdown(
        fact: SemanticAnnotation,
    ) -> str:
        return copilot_fact_value(fact)

    @staticmethod
    def fact_markdown_lines(fact: SemanticAnnotation) -> list[str]:
        """Return one fact in the canonical Copilot Markdown representation."""

        value = TopicRenderer._fact_value_markdown(fact)
        if not TopicRenderer._contains_hebrew(value):
            return [f"- {value}"]
        return ["<div dir=\"rtl\">", "", f"- {value}", "", "</div>"]

    @staticmethod
    def _contains_hebrew(value: str) -> bool:
        return any("\u0590" <= character <= "\u05ff" for character in value)

    @staticmethod
    def _context_filename(approved: KnowledgeCandidate) -> str:
        manifest = approved.document.source_manifest
        if manifest is None:
            raise ValueError("Approved knowledge is missing its source manifest")
        filename = Path(manifest.filename).name
        context_filename = f"{Path(filename).stem}.md"
        if context_filename.casefold() == "index.md":
            return "INDEX.approved-knowledge.md"
        return context_filename

    @staticmethod
    def _validate_version_files(
        version_path: Path,
        manifest: dict[str, object],
        context_filename: str,
    ) -> None:
        files = manifest.get("files")
        if not isinstance(files, list):
            raise ValueError("Existing Copilot context version is incomplete")
        expected_paths = {"INDEX.md", context_filename}
        hashes: dict[str, str] = {}
        for file_entry in files:
            if not isinstance(file_entry, dict):
                raise ValueError("Existing Copilot context version is incomplete")
            path = file_entry.get("path")
            digest = file_entry.get("sha256")
            if (
                not isinstance(path, str)
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or path in hashes
            ):
                raise ValueError("Existing Copilot context version is incomplete")
            hashes[path] = digest
        if set(hashes) != expected_paths:
            raise ValueError("Existing Copilot context version is incomplete")
        for relative_path, expected_digest in hashes.items():
            artifact_path = version_path / relative_path
            if not artifact_path.is_file():
                raise ValueError("Existing Copilot context version is incomplete")
            if sha256(artifact_path.read_bytes()).hexdigest() != expected_digest:
                raise ValueError("Existing Copilot context version has a mismatched hash")

    def _manifest(
        self,
        approved: KnowledgeCandidate,
        index_content: str,
        agent_content: str,
        context_filename: str,
        design_alignment: ApprovedDesignAlignment | None,
    ) -> dict[str, object]:
        files = [
            {"path": "INDEX.md", "sha256": sha256(index_content.encode("utf-8")).hexdigest()},
            {
                "path": context_filename,
                "sha256": sha256(agent_content.encode("utf-8")).hexdigest(),
            },
        ]
        return {
            "schema_version": "1.0",
            "renderer_version": self.renderer_version,
            "approved_candidate_sha256": approved.candidate_sha256,
            "source_id": approved.source_id,
            "design_alignment_sha256": self._design_alignment_sha256(design_alignment),
            "files": files,
        }

    @staticmethod
    def _design_alignment_sha256(
        design_alignment: ApprovedDesignAlignment | None,
    ) -> str | None:
        if design_alignment is None:
            return None
        return design_alignment.alignment.alignment_sha256

    @staticmethod
    def _version_name(
        approved: KnowledgeCandidate,
        design_alignment: ApprovedDesignAlignment | None,
    ) -> str:
        if design_alignment is None:
            return f"{approved.candidate_sha256}-v{TopicRenderer.renderer_version}"
        return (
            f"{approved.candidate_sha256}-design-"
            f"{design_alignment.alignment.alignment_sha256}-v{TopicRenderer.renderer_version}"
        )

    @staticmethod
    def _validate_design_alignment(
        approved: KnowledgeCandidate,
        design_alignment: ApprovedDesignAlignment | None,
    ) -> None:
        if design_alignment is None:
            return
        alignment = design_alignment.alignment
        if alignment.source_id != approved.source_id:
            raise ValueError("Design alignment belongs to a different source")
        if alignment.structural_sha256 != approved.structural_sha256:
            raise ValueError("Design alignment belongs to a different structural revision")
        if alignment.candidate_sha256 != approved.candidate_sha256:
            raise ValueError("Design alignment belongs to a different knowledge candidate")
        if alignment.figma_snapshot_sha256 != design_alignment.snapshot.snapshot_sha256:
            raise ValueError("Design alignment belongs to a different Figma snapshot")
        DesignAlignmentBuilder._validate_bindings(
            approved,
            design_alignment.snapshot,
            alignment.bindings,
        )
