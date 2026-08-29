from __future__ import annotations

import json
import os
import shutil
import uuid
import zipfile
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import quote
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field

from doc2agent.source_manifest import SourceManifest


class IngestDecision(StrEnum):
    PASS = "pass"
    REQUIRES_REVIEW = "requires_review"
    BLOCK = "block"


class IngestDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class DocxPreflightResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: IngestDecision
    diagnostics: list[IngestDiagnostic] = Field(default_factory=list)


@dataclass(frozen=True)
class DocxPreflightLimits:
    max_entries: int = 10_000
    max_uncompressed_bytes: int = 100 * 1024 * 1024
    max_compression_ratio: float = 100.0


class SourceCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    document_id: str
    filename: str
    source_path: str
    intake_path: str | None = None
    artifact_root: str | None = None
    preflight_decision: IngestDecision = IngestDecision.PASS
    review_approved: bool = False
    received_at: str


class SourceCatalogDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_source_id: str
    source_ids: list[str] = Field(default_factory=list)


class SourceCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    sources: dict[str, SourceCatalogEntry] = Field(default_factory=dict)
    documents: dict[str, SourceCatalogDocument] = Field(default_factory=dict)


@dataclass(frozen=True)
class IngestResult:
    decision: IngestDecision
    manifest: SourceManifest
    diagnostics: tuple[IngestDiagnostic, ...]
    document_id: str | None
    source_path: Path | None
    manifest_path: Path | None
    duplicate: bool
    review_approved: bool = False

    @property
    def accepted(self) -> bool:
        return self.decision == IngestDecision.PASS


@dataclass(frozen=True)
class DeleteResult:
    document_id: str
    source_ids: tuple[str, ...]
    paths: tuple[Path, ...]
    deleted: bool
    trash_path: Path | None


class DocxPreflight:
    """Inspect a DOCX package without extracting or resolving its contents."""

    def __init__(
        self,
        limits: DocxPreflightLimits | None = None,
    ) -> None:
        self.limits = limits or DocxPreflightLimits()

    def inspect(self, source: str | Path) -> DocxPreflightResult:
        source = Path(source)
        if source.suffix.lower() != ".docx":
            return self._result(
                IngestDecision.REQUIRES_REVIEW,
                "UNSUPPORTED_PREFLIGHT_FORMAT",
                "Ingest preflight currently supports DOCX only.",
            )

        try:
            with source.open("rb") as file:
                if file.read(4) != b"PK\x03\x04":
                    return self._result(
                        IngestDecision.BLOCK,
                        "INVALID_DOCX_SIGNATURE",
                        "DOCX must be a ZIP package.",
                    )

            with zipfile.ZipFile(source) as archive:
                entries = archive.infolist()
                if len(entries) > self.limits.max_entries:
                    return self._result(
                        IngestDecision.BLOCK,
                        "ARCHIVE_ENTRY_LIMIT_EXCEEDED",
                        "DOCX contains too many archive entries.",
                    )

                total_size = sum(entry.file_size for entry in entries)
                if total_size > self.limits.max_uncompressed_bytes:
                    return self._result(
                        IngestDecision.BLOCK,
                        "ARCHIVE_SIZE_LIMIT_EXCEEDED",
                        "DOCX exceeds the configured uncompressed size limit.",
                    )

                for entry in entries:
                    violation = self._entry_violation(entry)
                    if violation is not None:
                        return violation

                if archive.testzip() is not None:
                    return self._result(
                        IngestDecision.BLOCK,
                        "CORRUPT_DOCX_PACKAGE",
                        "DOCX package cannot be read safely.",
                    )

                names = {entry.filename for entry in entries}
                if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                    return self._result(
                        IngestDecision.BLOCK,
                        "INVALID_DOCX_STRUCTURE",
                        "DOCX is missing required package parts.",
                    )

                if any(name.lower().endswith("vbaproject.bin") for name in names):
                    return self._result(
                        IngestDecision.BLOCK,
                        "MACROS_NOT_ALLOWED",
                        "DOCX macros are not accepted.",
                    )

                for name in names:
                    if not name.endswith(".rels"):
                        continue
                    review = self._external_relationship(archive, name)
                    if review is not None:
                        return review
        except (OSError, zipfile.BadZipFile, ElementTree.ParseError):
            return self._result(
                IngestDecision.BLOCK,
                "CORRUPT_DOCX_PACKAGE",
                "DOCX package cannot be read safely.",
            )

        return DocxPreflightResult(decision=IngestDecision.PASS)

    def _entry_violation(
        self,
        entry: zipfile.ZipInfo,
    ) -> DocxPreflightResult | None:
        path = PurePosixPath(entry.filename)
        if (
            entry.filename.startswith(("/", "\\"))
            or "\\" in entry.filename
            or ".." in path.parts
        ):
            return self._result(
                IngestDecision.BLOCK,
                "UNSAFE_ARCHIVE_PATH",
                "DOCX contains an unsafe archive path.",
            )
        if entry.flag_bits & 0x1:
            return self._result(
                IngestDecision.BLOCK,
                "ENCRYPTED_ARCHIVE_ENTRY",
                "Encrypted DOCX entries are not supported.",
            )
        if entry.compress_size and (
            entry.file_size / entry.compress_size
            > self.limits.max_compression_ratio
        ):
            return self._result(
                IngestDecision.BLOCK,
                "ARCHIVE_COMPRESSION_RATIO_EXCEEDED",
                "DOCX archive compression ratio exceeds the configured limit.",
            )
        return None

    def _external_relationship(
        self,
        archive: zipfile.ZipFile,
        name: str,
    ) -> DocxPreflightResult | None:
        root = ElementTree.fromstring(archive.read(name))
        for relationship in root:
            if relationship.attrib.get("TargetMode") == "External":
                return self._result(
                    IngestDecision.REQUIRES_REVIEW,
                    "EXTERNAL_RELATIONSHIP",
                    "DOCX contains an external relationship that will not be fetched.",
                )
        return None

    @staticmethod
    def _result(
        decision: IngestDecision,
        code: str,
        message: str,
    ) -> DocxPreflightResult:
        return DocxPreflightResult(
            decision=decision,
            diagnostics=[IngestDiagnostic(code=code, message=message)],
        )


class IngestService:
    """Preflight and persist immutable DOCX sources by content hash."""

    def __init__(
        self,
        storage_root: str | Path = "storage",
        preflight: DocxPreflight | None = None,
    ) -> None:
        self.storage_root = Path(storage_root)
        self.preflight = preflight or DocxPreflight()

    def ingest(
        self,
        source: str | Path,
        *,
        document_id: str | None = None,
        approve_review: bool = False,
    ) -> IngestResult:
        source = Path(source)
        manifest = SourceManifest.from_path(source)
        catalog = self._read_catalog()
        preflight = self.preflight.inspect(source)
        if preflight.decision == IngestDecision.BLOCK:
            return IngestResult(
                decision=preflight.decision,
                manifest=manifest,
                diagnostics=tuple(preflight.diagnostics),
                document_id=document_id,
                source_path=None,
                manifest_path=None,
                duplicate=False,
            )

        existing = catalog.sources.get(manifest.source_id)
        if (
            existing is not None
            and (
                preflight.decision == IngestDecision.PASS
                or existing.review_approved
            )
        ):
            source_key = manifest.source_id.replace(":", "-")
            return IngestResult(
                decision=IngestDecision.PASS,
                manifest=manifest,
                diagnostics=tuple(preflight.diagnostics),
                document_id=existing.document_id,
                source_path=Path(existing.source_path),
                manifest_path=(
                    self.storage_root
                    / "sources"
                    / source_key
                    / "source-manifest.json"
                ),
                duplicate=True,
                review_approved=existing.review_approved,
            )

        if (
            preflight.decision == IngestDecision.REQUIRES_REVIEW
            and not approve_review
        ):
            return IngestResult(
                decision=preflight.decision,
                manifest=manifest,
                diagnostics=tuple(preflight.diagnostics),
                document_id=document_id,
                source_path=None,
                manifest_path=None,
                duplicate=False,
            )

        source_key = manifest.source_id.replace(":", "-")
        source_dir = self.storage_root / "sources" / source_key
        stored_source = source_dir / manifest.filename
        manifest_path = source_dir / "source-manifest.json"
        resolved_document_id = (
            document_id
            or self._document_id_for_intake_path(catalog, source)
            or f"document:{uuid.uuid4()}"
        )
        self._store_source(source, stored_source)
        self._write_json_atomic(manifest_path, manifest.model_dump(mode="json"))

        catalog.sources[manifest.source_id] = SourceCatalogEntry(
            source_id=manifest.source_id,
            document_id=resolved_document_id,
            filename=manifest.filename,
            source_path=str(stored_source),
            intake_path=str(source.resolve()),
            preflight_decision=preflight.decision,
            review_approved=(
                preflight.decision == IngestDecision.REQUIRES_REVIEW
            ),
            received_at=manifest.received_at.isoformat(),
        )
        document = catalog.documents.get(resolved_document_id)
        if document is None:
            document = SourceCatalogDocument(
                current_source_id=manifest.source_id,
                source_ids=[manifest.source_id],
            )
        else:
            document.source_ids.append(manifest.source_id)
            document.current_source_id = manifest.source_id
        catalog.documents[resolved_document_id] = document
        self._write_json_atomic(
            self.storage_root / "source-catalog.json",
            catalog.model_dump(mode="json"),
        )

        return IngestResult(
            decision=IngestDecision.PASS,
            manifest=manifest,
            diagnostics=tuple(preflight.diagnostics),
            document_id=resolved_document_id,
            source_path=stored_source,
            manifest_path=manifest_path,
            duplicate=False,
            review_approved=(
                preflight.decision == IngestDecision.REQUIRES_REVIEW
            ),
        )

    def _read_catalog(self) -> SourceCatalog:
        path = self.storage_root / "source-catalog.json"
        if not path.exists():
            return SourceCatalog()
        return SourceCatalog.model_validate_json(path.read_text(encoding="utf-8"))

    def record_artifact_root(
        self,
        source_id: str,
        artifact_root: str | Path,
    ) -> None:
        catalog = self._read_catalog()
        entry = catalog.sources.get(source_id)
        if entry is None:
            raise ValueError(f"Unknown source id: {source_id}")

        expected_root = self._artifact_root_for_source(source_id)
        if Path(artifact_root).resolve() != expected_root.resolve():
            raise ValueError(
                "Artifact root must be the isolated storage directory for its source"
            )
        entry.artifact_root = str(expected_root)
        self._write_json_atomic(
            self.storage_root / "source-catalog.json",
            catalog.model_dump(mode="json"),
        )

    def document_id_for_source(self, source_id: str) -> str | None:
        """Return the stable logical-document ID for an accepted source revision."""

        entry = self._read_catalog().sources.get(source_id)
        return entry.document_id if entry is not None else None

    def delete_document(
        self,
        document_id: str,
        *,
        confirm: bool = False,
    ) -> DeleteResult:
        catalog = self._read_catalog()
        document = catalog.documents.get(document_id)
        if document is None:
            raise ValueError(f"Unknown document id: {document_id}")

        source_ids = tuple(document.source_ids)
        paths = self._document_paths(catalog, document_id, source_ids)
        approval_log_path = self.storage_root / "approval-log.jsonl"
        approval_log = self._split_approval_log(
            approval_log_path,
            document_id,
            source_ids,
        )
        result = DeleteResult(
            document_id=document_id,
            source_ids=source_ids,
            paths=paths,
            deleted=False,
            trash_path=None,
        )
        if not confirm:
            return result

        trash_path = self.storage_root / ".trash" / uuid.uuid4().hex
        moved: list[tuple[Path, Path]] = []
        try:
            for index, path in enumerate(paths):
                if not path.exists() and not path.is_symlink():
                    continue
                destination = trash_path / f"{index}-{path.name}"
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, destination)
                moved.append((path, destination))

            if approval_log is not None:
                original_log, retained_log, deleted_log = approval_log
                (trash_path / "approval-log.jsonl").write_text(
                    deleted_log,
                    encoding="utf-8",
                )
                self._write_text_atomic(approval_log_path, retained_log)

            for source_id in source_ids:
                del catalog.sources[source_id]
            del catalog.documents[document_id]
            self._write_json_atomic(
                self.storage_root / "source-catalog.json",
                catalog.model_dump(mode="json"),
            )
        except Exception:
            for original, moved_path in reversed(moved):
                if moved_path.exists():
                    original.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(moved_path, original)
            if approval_log is not None:
                self._write_text_atomic(approval_log_path, approval_log[0])
            raise

        return DeleteResult(
            document_id=document_id,
            source_ids=source_ids,
            paths=paths,
            deleted=True,
            trash_path=trash_path,
        )

    @staticmethod
    def _document_id_for_intake_path(
        catalog: SourceCatalog,
        source: Path,
    ) -> str | None:
        intake_path = str(source.resolve())
        for document_id, document in catalog.documents.items():
            current = catalog.sources[document.current_source_id]
            if current.intake_path == intake_path:
                return document_id
        return None

    def _document_paths(
        self,
        catalog: SourceCatalog,
        document_id: str,
        source_ids: tuple[str, ...],
    ) -> tuple[Path, ...]:
        paths: list[Path] = []
        for source_id in source_ids:
            entry = catalog.sources.get(source_id)
            if entry is None:
                raise ValueError(f"Missing catalog entry for source id: {source_id}")
            source_dir = self._source_dir_for_source(source_id)
            if Path(entry.source_path).parent.resolve() != source_dir.resolve():
                raise ValueError("Source path is not in the isolated source directory")
            paths.append(source_dir)
            if entry.artifact_root is not None:
                artifact_root = self._artifact_root_for_source(source_id)
                if Path(entry.artifact_root).resolve() != artifact_root.resolve():
                    raise ValueError(
                        "Artifact root is not the isolated storage directory for its source"
                    )
                paths.append(artifact_root)

        paths.append(self._approved_document_dir(document_id))
        paths.extend(self._copilot_context_paths(source_ids))

        unique_paths = tuple(dict.fromkeys(paths))
        storage_root = self.storage_root.resolve()
        for path in unique_paths:
            try:
                path.resolve().relative_to(storage_root)
            except ValueError as exc:
                raise ValueError(
                    f"Refusing to delete a path outside storage: {path}"
                ) from exc
        return unique_paths

    def _approved_document_dir(self, document_id: str) -> Path:
        readable_id = quote(document_id, safe="-._")
        suffix = sha256(document_id.encode("utf-8")).hexdigest()[:12]
        return self.storage_root / "approved" / f"{readable_id}--{suffix}"

    def _source_dir_for_source(self, source_id: str) -> Path:
        return self.storage_root / "sources" / source_id.replace(":", "-")

    def _artifact_root_for_source(self, source_id: str) -> Path:
        return self.storage_root / "artifacts" / source_id.replace(":", "-")

    def _copilot_context_paths(self, source_ids: tuple[str, ...]) -> tuple[Path, ...]:
        context_root = self.storage_root / "copilot-context"
        paths: list[Path] = []
        for manifest_path in context_root.glob(".*-versions/*/manifest.json"):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if manifest.get("source_id") not in source_ids:
                continue
            version_root = manifest_path.parent.parent
            name = version_root.name
            if not name.startswith(".") or not name.endswith("-versions"):
                continue
            output_root = context_root / name[1:-len("-versions")]
            paths.extend((output_root, version_root))
        return tuple(paths)

    @staticmethod
    def _split_approval_log(
        path: Path,
        document_id: str,
        source_ids: tuple[str, ...],
    ) -> tuple[str, str, str] | None:
        if not path.exists():
            return None
        original = path.read_text(encoding="utf-8")
        retained_lines: list[str] = []
        deleted_lines: list[str] = []
        for line in original.splitlines(keepends=True):
            if not line.strip():
                retained_lines.append(line)
                continue
            entry = json.loads(line)
            if (
                entry.get("document_id") == document_id
                or entry.get("source_id") in source_ids
            ):
                deleted_lines.append(line)
            else:
                retained_lines.append(line)
        if not deleted_lines:
            return None
        return original, "".join(retained_lines), "".join(deleted_lines)

    @staticmethod
    def _store_source(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}")
        try:
            shutil.copyfile(source, temporary)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _write_json_atomic(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _write_text_atomic(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
        try:
            temporary.write_text(value, encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
