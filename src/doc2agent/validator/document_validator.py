from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from doc2agent.models.document import AnalyzedDocument, ContentBlock, Section

Severity = Literal["error", "warning"]


class ValidationIssue(BaseModel):
    """A single deterministic validation issue."""

    model_config = ConfigDict(extra="forbid")

    severity: Severity
    code: str
    message: str

    object_id: str | None = None
    path: str | None = None


class ValidationReport(BaseModel):
    """Validation result for canonical document.json."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    errors: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)


class DocumentValidator:
    """
    Validate deterministic document.json before any LLM enrichment.

    The validator is rule-based and performs no semantic inference.
    """

    def validate(
        self,
        document: AnalyzedDocument,
        *,
        markdown_path: str | Path | None = None,
    ) -> ValidationReport:
        errors: list[ValidationIssue] = []
        warnings: list[ValidationIssue] = []

        self._validate_document_info(
            document,
            errors=errors,
            warnings=warnings,
            markdown_path=markdown_path,
        )

        self._validate_unique_ids(
            document,
            errors=errors,
        )

        for index, block in enumerate(document.preamble):
            self._validate_block(
                block,
                path=f"preamble[{index}]",
                errors=errors,
                warnings=warnings,
                markdown_path=markdown_path,
            )

        for index, section in enumerate(document.sections):
            self._validate_section(
                section,
                path=f"sections[{index}]",
                errors=errors,
                warnings=warnings,
                markdown_path=markdown_path,
                parent=None,
            )

        return ValidationReport(
            valid=not errors,
            errors=errors,
            warnings=warnings,
        )

    def validate_json(
        self,
        json_path: str | Path,
    ) -> ValidationReport:
        """
        Load and validate a document.json file.

        Schema/Pydantic failures are converted into a normal validation report
        instead of raising to the CLI.
        """

        json_path = Path(json_path)

        try:
            document = AnalyzedDocument.model_validate_json(
                json_path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError, ValueError) as exc:
            return ValidationReport(
                valid=False,
                errors=[
                    ValidationIssue(
                        severity="error",
                        code="INVALID_DOCUMENT_SCHEMA",
                        message=str(exc),
                        path=str(json_path),
                    )
                ],
            )

        markdown_path = Path(document.document.markdown)

        return self.validate(
            document,
            markdown_path=markdown_path,
        )

    def write_json(
        self,
        report: ValidationReport,
        output_path: str | Path,
    ) -> Path:
        output_path = Path(output_path)

        output_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        output_path.write_text(
            report.model_dump_json(
                indent=2,
                exclude_none=True,
            ),
            encoding="utf-8",
        )

        return output_path

    def _validate_document_info(
        self,
        document: AnalyzedDocument,
        *,
        errors: list[ValidationIssue],
        warnings: list[ValidationIssue],
        markdown_path: str | Path | None,
    ) -> None:
        info = document.document

        if not info.id.strip():
            errors.append(
                ValidationIssue(
                    severity="error",
                    code="EMPTY_DOCUMENT_ID",
                    message="Document id must not be empty.",
                    path="document.id",
                )
            )

        if not info.title.strip():
            warnings.append(
                ValidationIssue(
                    severity="warning",
                    code="EMPTY_DOCUMENT_TITLE",
                    message="Document title is empty.",
                    object_id=info.id,
                    path="document.title",
                )
            )

        effective_markdown = (
            Path(markdown_path)
            if markdown_path is not None
            else Path(info.markdown)
        )

        if not effective_markdown.exists():
            errors.append(
                ValidationIssue(
                    severity="error",
                    code="MARKDOWN_NOT_FOUND",
                    message=f"Analyzed Markdown file does not exist: {effective_markdown}",
                    object_id=info.id,
                    path="document.markdown",
                )
            )

        if "normalized" not in effective_markdown.parts:
            warnings.append(
                ValidationIssue(
                    severity="warning",
                    code="MARKDOWN_NOT_NORMALIZED_PATH",
                    message=(
                        "document.markdown does not appear to point to the normalized "
                        "Markdown layer."
                    ),
                    object_id=info.id,
                    path="document.markdown",
                )
            )

    def _validate_unique_ids(
        self,
        document: AnalyzedDocument,
        *,
        errors: list[ValidationIssue],
    ) -> None:
        seen: dict[str, str] = {}

        def register(object_id: str, path: str) -> None:
            existing = seen.get(object_id)

            if existing is not None:
                errors.append(
                    ValidationIssue(
                        severity="error",
                        code="DUPLICATE_ID",
                        message=(
                            f"Duplicate id '{object_id}' found at {path}; "
                            f"first seen at {existing}."
                        ),
                        object_id=object_id,
                        path=path,
                    )
                )
                return

            seen[object_id] = path

        register(
            document.document.id,
            "document.id",
        )

        for index, block in enumerate(document.preamble):
            register(
                block.id,
                f"preamble[{index}].id",
            )

        def walk_section(section: Section, path: str) -> None:
            register(
                section.id,
                f"{path}.id",
            )

            for block_index, block in enumerate(section.blocks):
                register(
                    block.id,
                    f"{path}.blocks[{block_index}].id",
                )

            for child_index, child in enumerate(section.children):
                walk_section(
                    child,
                    f"{path}.children[{child_index}]",
                )

        for section_index, section in enumerate(document.sections):
            walk_section(
                section,
                f"sections[{section_index}]",
            )

    def _validate_section(
        self,
        section: Section,
        *,
        path: str,
        errors: list[ValidationIssue],
        warnings: list[ValidationIssue],
        markdown_path: str | Path | None,
        parent: Section | None,
    ) -> None:
        self._validate_line_range(
            section.start_line,
            section.end_line,
            object_id=section.id,
            path=path,
            errors=errors,
        )

        if not section.title.strip():
            errors.append(
                ValidationIssue(
                    severity="error",
                    code="EMPTY_SECTION_TITLE",
                    message="Section title must not be empty.",
                    object_id=section.id,
                    path=f"{path}.title",
                )
            )

        if not section.path:
            errors.append(
                ValidationIssue(
                    severity="error",
                    code="EMPTY_SECTION_PATH",
                    message="Section path must not be empty.",
                    object_id=section.id,
                    path=f"{path}.path",
                )
            )
        elif section.path[-1] != section.title:
            errors.append(
                ValidationIssue(
                    severity="error",
                    code="SECTION_PATH_TITLE_MISMATCH",
                    message="The last path item must equal the section title.",
                    object_id=section.id,
                    path=f"{path}.path",
                )
            )

        if parent is not None:
            if section.level <= parent.level:
                errors.append(
                    ValidationIssue(
                        severity="error",
                        code="INVALID_SECTION_LEVEL",
                        message=(
                            f"Child section level {section.level} must be deeper "
                            f"than parent level {parent.level}."
                        ),
                        object_id=section.id,
                        path=f"{path}.level",
                    )
                )

            if section.level > parent.level + 1:
                warnings.append(
                    ValidationIssue(
                        severity="warning",
                        code="SKIPPED_HEADING_LEVEL",
                        message=(
                            f"Heading level jumps from {parent.level} "
                            f"to {section.level}."
                        ),
                        object_id=section.id,
                        path=f"{path}.level",
                    )
                )

            if (
                section.start_line < parent.start_line
                or section.end_line > parent.end_line
            ):
                errors.append(
                    ValidationIssue(
                        severity="error",
                        code="SECTION_OUTSIDE_PARENT_RANGE",
                        message=(
                            "Child section source range must be inside "
                            "its parent source range."
                        ),
                        object_id=section.id,
                        path=path,
                    )
                )

            expected_prefix = parent.path

            if section.path[:-1] != expected_prefix:
                errors.append(
                    ValidationIssue(
                        severity="error",
                        code="SECTION_PATH_PARENT_MISMATCH",
                        message=(
                            "Child section path does not match its parent path."
                        ),
                        object_id=section.id,
                        path=f"{path}.path",
                    )
                )

        for block_index, block in enumerate(section.blocks):
            block_path = f"{path}.blocks[{block_index}]"

            self._validate_block(
                block,
                path=block_path,
                errors=errors,
                warnings=warnings,
                markdown_path=markdown_path,
            )

            if (
                block.start_line < section.start_line
                or block.end_line > section.end_line
            ):
                errors.append(
                    ValidationIssue(
                        severity="error",
                        code="BLOCK_OUTSIDE_SECTION_RANGE",
                        message=(
                            "Block source range must be inside "
                            "its owning section source range."
                        ),
                        object_id=block.id,
                        path=block_path,
                    )
                )

        for child_index, child in enumerate(section.children):
            self._validate_section(
                child,
                path=f"{path}.children[{child_index}]",
                errors=errors,
                warnings=warnings,
                markdown_path=markdown_path,
                parent=section,
            )

    def _validate_block(
        self,
        block: ContentBlock,
        *,
        path: str,
        errors: list[ValidationIssue],
        warnings: list[ValidationIssue],
        markdown_path: str | Path | None,
    ) -> None:
        self._validate_line_range(
            block.start_line,
            block.end_line,
            object_id=block.id,
            path=path,
            errors=errors,
        )

        if block.type == "paragraph":
            if not (block.text or "").strip():
                errors.append(
                    ValidationIssue(
                        severity="error",
                        code="EMPTY_PARAGRAPH",
                        message="Paragraph block must contain text.",
                        object_id=block.id,
                        path=path,
                    )
                )

        elif block.type in {"unordered_list", "ordered_list"}:
            if not block.items:
                errors.append(
                    ValidationIssue(
                        severity="error",
                        code="EMPTY_LIST",
                        message="List block must contain at least one item.",
                        object_id=block.id,
                        path=path,
                    )
                )

            if any(not item.strip() for item in block.items):
                warnings.append(
                    ValidationIssue(
                        severity="warning",
                        code="EMPTY_LIST_ITEM",
                        message="List contains an empty item.",
                        object_id=block.id,
                        path=path,
                    )
                )

        elif block.type == "code":
            if block.text is None:
                errors.append(
                    ValidationIssue(
                        severity="error",
                        code="CODE_WITHOUT_TEXT",
                        message="Code block must contain a text value.",
                        object_id=block.id,
                        path=path,
                    )
                )

        elif block.type == "table":
            self._validate_table(
                block,
                path=path,
                errors=errors,
                warnings=warnings,
            )

        elif block.type == "image":
            self._validate_image(
                block,
                path=path,
                errors=errors,
                warnings=warnings,
                markdown_path=markdown_path,
            )

    def _validate_table(
        self,
        block: ContentBlock,
        *,
        path: str,
        errors: list[ValidationIssue],
        warnings: list[ValidationIssue],
    ) -> None:
        if not block.headers:
            errors.append(
                ValidationIssue(
                    severity="error",
                    code="TABLE_WITHOUT_HEADERS",
                    message="Table must contain at least one header cell.",
                    object_id=block.id,
                    path=path,
                )
            )
            return

        expected_columns = len(block.headers)

        if len(block.rows) == 0:
            warnings.append(
                ValidationIssue(
                    severity="warning",
                    code="TABLE_WITHOUT_ROWS",
                    message="Table has headers but no data rows.",
                    object_id=block.id,
                    path=path,
                )
            )

        for row_index, row in enumerate(block.rows):
            if len(row) != expected_columns:
                errors.append(
                    ValidationIssue(
                        severity="error",
                        code="TABLE_COLUMN_COUNT_MISMATCH",
                        message=(
                            f"Table row {row_index} has {len(row)} columns; "
                            f"expected {expected_columns}."
                        ),
                        object_id=block.id,
                        path=f"{path}.rows[{row_index}]",
                    )
                )

    def _validate_image(
        self,
        block: ContentBlock,
        *,
        path: str,
        errors: list[ValidationIssue],
        warnings: list[ValidationIssue],
        markdown_path: str | Path | None,
    ) -> None:
        if not (block.src or "").strip():
            errors.append(
                ValidationIssue(
                    severity="error",
                    code="IMAGE_WITHOUT_SRC",
                    message="Image block must contain src.",
                    object_id=block.id,
                    path=f"{path}.src",
                )
            )
            return

        src = block.src or ""

        if self._is_remote_uri(src):
            return

        if markdown_path is None:
            warnings.append(
                ValidationIssue(
                    severity="warning",
                    code="IMAGE_PATH_NOT_CHECKED",
                    message=(
                        "Cannot verify local image because Markdown path "
                        "was not provided."
                    ),
                    object_id=block.id,
                    path=f"{path}.src",
                )
            )
            return

        markdown_path = Path(markdown_path)

        image_path = (
            markdown_path.parent
            / src
        ).resolve()

        if not image_path.exists():
            errors.append(
                ValidationIssue(
                    severity="error",
                    code="IMAGE_FILE_NOT_FOUND",
                    message=f"Referenced image does not exist: {image_path}",
                    object_id=block.id,
                    path=f"{path}.src",
                )
            )

    @staticmethod
    def _is_remote_uri(value: str) -> bool:
        lowered = value.lower()

        return lowered.startswith(
            (
                "http://",
                "https://",
                "data:",
            )
        )

    @staticmethod
    def _validate_line_range(
        start_line: int,
        end_line: int,
        *,
        object_id: str,
        path: str,
        errors: list[ValidationIssue],
    ) -> None:
        if start_line < 1:
            errors.append(
                ValidationIssue(
                    severity="error",
                    code="INVALID_START_LINE",
                    message="start_line must be >= 1.",
                    object_id=object_id,
                    path=f"{path}.start_line",
                )
            )

        if end_line < start_line:
            errors.append(
                ValidationIssue(
                    severity="error",
                    code="INVALID_LINE_RANGE",
                    message="end_line must be >= start_line.",
                    object_id=object_id,
                    path=path,
                )
            )
