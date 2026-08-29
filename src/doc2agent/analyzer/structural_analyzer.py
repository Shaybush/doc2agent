from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from doc2agent.analyzer.builders import build_section_tree
from doc2agent.analyzer.language import detect_direction, detect_language
from doc2agent.artifacts import canonical_json_sha256
from doc2agent.models.document import (
    AnalyzedDocument,
    ContentBlock,
    Direction,
    DocumentInfo,
    Section,
)
from doc2agent.source_manifest import SourceManifest

_HEADING_RE = re.compile(
    r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$"
)

_UNORDERED_RE = re.compile(
    r"^[ \t]*[-+*][ \t]+(.+)$"
)

_ORDERED_RE = re.compile(
    r"^[ \t]*\d+[.)][ \t]+(.+)$"
)

_FENCE_RE = re.compile(
    r"^[ \t]*(```+|~~~+)(.*)$"
)

_IMAGE_RE = re.compile(
    r"^[ \t]*!\[(?P<alt>.*?)\]\((?P<src>.+?)\)[ \t]*$"
)


class StructuralAnalyzer:
    """
    Parse normalized Markdown into document.json.

    This analyzer is deterministic and rule-based.
    No LLM is used here.
    """

    def analyze(
        self,
        markdown_path: str | Path,
        *,
        metadata_path: str | Path | None = None,
        source_manifest: SourceManifest | None = None,
    ) -> AnalyzedDocument:
        markdown_path = Path(markdown_path)

        text = markdown_path.read_text(
            encoding="utf-8"
        )

        metadata = self._load_metadata(
            metadata_path
        )

        return self.analyze_text(
            text,
            markdown_path=markdown_path,
            metadata=metadata,
            source_manifest=source_manifest,
        )

    def analyze_text(
        self,
        text: str,
        *,
        markdown_path: str | Path = "document.md",
        metadata: dict[str, Any] | None = None,
        source_manifest: SourceManifest | None = None,
    ) -> AnalyzedDocument:
        markdown_path = Path(markdown_path)
        metadata = metadata or {}
        source_directions = self._source_directions(metadata)

        lines = text.splitlines()

        heading_rows = self._find_headings(lines)

        section_end_lines = self._section_end_lines(
            heading_rows,
            len(lines),
        )

        # Content before the first heading.
        preamble_end = (
            heading_rows[0][0] - 1
            if heading_rows
            else len(lines)
        )

        preamble = self._parse_blocks(
            lines,
            1,
            preamble_end,
            scope_id="preamble",
            source_directions=source_directions,
        )

        flat_sections: list[Section] = []
        stack_titles: list[tuple[int, str]] = []

        for index, (
            line_no,
            level,
            title,
        ) in enumerate(heading_rows):

            # Remove headings that are on the same
            # or deeper level than the current heading.
            while (
                stack_titles
                and stack_titles[-1][0] >= level
            ):
                stack_titles.pop()

            path = [
                item[1]
                for item in stack_titles
            ] + [title]

            stack_titles.append(
                (level, title)
            )

            section_id = self._stable_id(
                "section",
                markdown_path.as_posix(),
                str(line_no),
                str(level),
                "/".join(path),
            )

            end_line = section_end_lines[index]

            body_start = line_no + 1

            next_heading_line = (
                heading_rows[index + 1][0]
                if index + 1 < len(heading_rows)
                else len(lines) + 1
            )

            # Content directly belonging to this heading.
            direct_body_end = next_heading_line - 1

            blocks = self._parse_blocks(
                lines,
                body_start,
                direct_body_end,
                scope_id=section_id,
                source_directions=source_directions,
            )

            flat_sections.append(
                Section(
                    id=section_id,
                    level=level,
                    title=title,
                    path=path,
                    language=detect_language(title),
                    direction=(
                        source_directions.get(self._normalise_source_text(title))
                        or detect_direction(title)
                    ),
                    start_line=line_no,
                    end_line=end_line,
                    blocks=blocks,
                )
            )

        title = next(
            (
                title
                for _, level, title in heading_rows
                if level == 1
            ),
            markdown_path.stem,
        )

        source = str(
            metadata.get(
                "source",
                markdown_path,
            )
        )

        source_format = str(
            metadata.get(
                "source_format",
                markdown_path.suffix.lstrip(".") or "md",
            )
        )

        # Important:
        # point to the Markdown that was actually analyzed.
        # At this stage it is normalized Markdown.
        markdown_value = markdown_path.as_posix()

        document = DocumentInfo(
            id=self._stable_id(
                "document",
                source,
                markdown_path.as_posix(),
            ),
            title=title,
            source=source,
            source_format=source_format,
            markdown=markdown_value,
            language=detect_language(text),
            direction=detect_direction(text),
        )

        analyzed_document = AnalyzedDocument(
            source_manifest=source_manifest,
            document=document,
            preamble=preamble,
            sections=build_section_tree(
                flat_sections
            ),
        )
        return self._with_structural_hash(analyzed_document)

    @staticmethod
    def with_source_manifest(
        document: AnalyzedDocument,
        source_manifest: SourceManifest,
    ) -> AnalyzedDocument:
        """Bind an existing structural result to an immutable source."""

        return StructuralAnalyzer._with_structural_hash(
            document.model_copy(
                update={
                    "source_manifest": source_manifest,
                    "structural_sha256": None,
                }
            )
        )

    @staticmethod
    def _with_structural_hash(
        document: AnalyzedDocument,
    ) -> AnalyzedDocument:
        structural_payload = document.model_dump(
            mode="json",
            exclude_none=True,
            exclude={"structural_sha256"},
        )
        source_manifest = structural_payload.get("source_manifest")
        if source_manifest is not None:
            source_manifest.pop("received_at", None)
        return document.model_copy(
            update={
                "structural_sha256": canonical_json_sha256(
                    structural_payload
                )
            }
        )

    def write_json(
        self,
        document: AnalyzedDocument,
        output_path: str | Path,
    ) -> Path:
        output_path = Path(output_path)

        output_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        output_path.write_text(
            document.model_dump_json(
                indent=2,
                exclude_none=True,
            ),
            encoding="utf-8",
        )

        return output_path

    @staticmethod
    def _load_metadata(
        metadata_path: str | Path | None,
    ) -> dict[str, Any]:
        if metadata_path is None:
            return {}

        path = Path(metadata_path)

        if not path.exists():
            return {}

        return json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

    @staticmethod
    def _find_headings(
        lines: list[str],
    ) -> list[tuple[int, int, str]]:
        headings: list[
            tuple[int, int, str]
        ] = []

        fence: str | None = None

        for line_no, line in enumerate(
            lines,
            start=1,
        ):
            fence_match = _FENCE_RE.match(
                line
            )

            if fence_match:
                token = fence_match.group(1)

                if fence is None:
                    fence = token[0] * len(token)

                elif (
                    token[0] == fence[0]
                    and len(token) >= len(fence)
                ):
                    fence = None

                continue

            if fence is not None:
                continue

            match = _HEADING_RE.match(
                line
            )

            if match:
                headings.append(
                    (
                        line_no,
                        len(match.group(1)),
                        match.group(2).strip(),
                    )
                )

        return headings

    @staticmethod
    def _section_end_lines(
        headings: list[
            tuple[int, int, str]
        ],
        total_lines: int,
    ) -> list[int]:
        result: list[int] = []

        for index, (
            _,
            level,
            _,
        ) in enumerate(headings):

            end_line = total_lines

            for (
                later_line,
                later_level,
                _,
            ) in headings[index + 1 :]:

                if later_level <= level:
                    end_line = later_line - 1
                    break

            result.append(
                end_line
            )

        return result

    # =========================================================
    # TABLES
    # =========================================================

    @staticmethod
    def _split_table_row(
        line: str,
    ) -> list[str]:
        """
        Convert:

        | A | B | C |

        to:

        ["A", "B", "C"]
        """

        line = line.strip()

        if line.startswith("|"):
            line = line[1:]

        if line.endswith("|"):
            line = line[:-1]

        return [
            cell.strip()
            for cell in line.split("|")
        ]

    @classmethod
    def _is_table_separator(
        cls,
        line: str,
    ) -> bool:
        """
        Detect a Markdown table separator.

        Examples:

        | --- | --- |
        | ---: | :--- |
        | :---: | --- |
        """

        cells = cls._split_table_row(
            line
        )

        if not cells:
            return False

        for cell in cells:
            normalized = cell.replace(
                " ",
                "",
            )

            if not re.fullmatch(
                r":?-{3,}:?",
                normalized,
            ):
                return False

        return True

    @classmethod
    def _is_table_start(
        cls,
        lines: list[str],
        line_no: int,
        end_line: int,
    ) -> bool:
        """
        A table must contain a header row
        followed by a separator row.

        Example:

        | Name | Value |
        | ---- | ----- |
        """

        if line_no + 1 > end_line:
            return False

        header = lines[
            line_no - 1
        ]

        separator = lines[
            line_no
        ]

        if "|" not in header:
            return False

        return cls._is_table_separator(
            separator
        )

    # =========================================================
    # BLOCK PARSING
    # =========================================================

    def _parse_blocks(
        self,
        lines: list[str],
        start_line: int,
        end_line: int,
        *,
        scope_id: str,
        source_directions: dict[str, Direction],
    ) -> list[ContentBlock]:

        if (
            start_line > end_line
            or not lines
        ):
            return []

        blocks: list[ContentBlock] = []

        i = start_line
        block_index = 0

        while i <= end_line:
            raw = lines[
                i - 1
            ]

            # Skip empty lines.
            if not raw.strip():
                i += 1
                continue

            # =================================================
            # CODE BLOCK
            # =================================================

            fence_match = _FENCE_RE.match(
                raw
            )

            if fence_match:
                fence_token = (
                    fence_match.group(1)
                )

                language_hint = (
                    fence_match
                    .group(2)
                    .strip()
                )

                code_start = i

                i += 1

                code_lines: list[str] = []

                while i <= end_line:
                    candidate = lines[
                        i - 1
                    ]

                    candidate_match = (
                        _FENCE_RE.match(
                            candidate
                        )
                    )

                    if (
                        candidate_match
                        and candidate_match.group(1)[0]
                        == fence_token[0]
                        and len(
                            candidate_match.group(1)
                        )
                        >= len(fence_token)
                    ):
                        break

                    code_lines.append(
                        candidate
                    )

                    i += 1

                code_end = (
                    i
                    if i <= end_line
                    else end_line
                )

                text = "\n".join(
                    code_lines
                )

                blocks.append(
                    self._block(
                        scope_id,
                        block_index,
                        "code",
                        code_start,
                        code_end,
                        text=text,
                        info=(
                            language_hint
                            or None
                        ),
                        source_direction=self._source_direction_for(
                            source_directions,
                            text,
                        ),
                    )
                )

                block_index += 1
                i = code_end + 1

                continue

            # =================================================
            # IMAGE
            # =================================================

            image_match = _IMAGE_RE.match(
                raw
            )

            if image_match:
                alt = (
                    image_match
                    .group("alt")
                    .strip()
                )

                src = (
                    image_match
                    .group("src")
                    .strip()
                )

                blocks.append(
                    self._block(
                        scope_id,
                        block_index,
                        "image",
                        i,
                        i,
                        alt=alt,
                        src=src,
                        source_direction=self._source_direction_for(
                            source_directions,
                            alt,
                        ),
                    )
                )

                block_index += 1
                i += 1

                continue

            # =================================================
            # TABLE
            # =================================================

            if self._is_table_start(
                lines,
                i,
                end_line,
            ):
                table_start = i

                headers = (
                    self._split_table_row(
                        lines[i - 1]
                    )
                )

                # Skip:
                #
                # header
                # separator
                i += 2

                rows: list[
                    list[str]
                ] = []

                while i <= end_line:
                    candidate = lines[
                        i - 1
                    ]

                    if not candidate.strip():
                        break

                    if "|" not in candidate:
                        break

                    if _HEADING_RE.match(
                        candidate
                    ):
                        break

                    rows.append(
                        self._split_table_row(
                            candidate
                        )
                    )

                    i += 1

                blocks.append(
                    self._block(
                        scope_id,
                        block_index,
                        "table",
                        table_start,
                        i - 1,
                        headers=headers,
                        rows=rows,
                        source_direction=self._source_direction_for(
                            source_directions,
                            *headers,
                            *(cell for row in rows for cell in row),
                        ),
                    )
                )

                block_index += 1

                continue

            # =================================================
            # LIST
            # =================================================

            unordered = _UNORDERED_RE.match(
                raw
            )

            ordered = _ORDERED_RE.match(
                raw
            )

            if unordered or ordered:
                block_type = (
                    "unordered_list"
                    if unordered
                    else "ordered_list"
                )

                list_start = i

                items: list[str] = []

                matcher = (
                    _UNORDERED_RE
                    if unordered
                    else _ORDERED_RE
                )

                while i <= end_line:
                    match = matcher.match(
                        lines[i - 1]
                    )

                    if not match:
                        break

                    items.append(
                        match.group(1).strip()
                    )

                    i += 1

                blocks.append(
                    self._block(
                        scope_id,
                        block_index,
                        block_type,
                        list_start,
                        i - 1,
                        items=items,
                        source_direction=self._source_direction_for(
                            source_directions,
                            *items,
                        ),
                    )
                )

                block_index += 1

                continue

            # =================================================
            # PARAGRAPH
            # =================================================

            paragraph_start = i

            paragraph_lines: list[str] = []

            while i <= end_line:
                candidate = lines[
                    i - 1
                ]

                if not candidate.strip():
                    break

                # Stop paragraph when another
                # structural block begins.
                is_special_block = (
                    _FENCE_RE.match(
                        candidate
                    )
                    or _IMAGE_RE.match(
                        candidate
                    )
                    or _UNORDERED_RE.match(
                        candidate
                    )
                    or _ORDERED_RE.match(
                        candidate
                    )
                    or self._is_table_start(
                        lines,
                        i,
                        end_line,
                    )
                )

                if (
                    is_special_block
                    and paragraph_lines
                ):
                    break

                if _HEADING_RE.match(
                    candidate
                ):
                    break

                paragraph_lines.append(
                    candidate.strip()
                )

                i += 1

            if paragraph_lines:
                paragraph_text = (
                    "\n".join(
                        paragraph_lines
                    )
                )

                blocks.append(
                    self._block(
                        scope_id,
                        block_index,
                        "paragraph",
                        paragraph_start,
                        i - 1,
                        text=paragraph_text,
                        source_direction=self._source_direction_for(
                            source_directions,
                            paragraph_text,
                        ),
                    )
                )

                block_index += 1

            else:
                i += 1

        return blocks

    # =========================================================
    # CONTENT BLOCK BUILDER
    # =========================================================

    def _block(
        self,
        scope_id: str,
        index: int,
        block_type: str,
        start_line: int,
        end_line: int,
        *,
        text: str | None = None,
        info: str | None = None,
        items: list[str] | None = None,
        alt: str | None = None,
        src: str | None = None,
        headers: list[str] | None = None,
        rows: list[list[str]] | None = None,
        source_direction: Direction | None = None,
    ) -> ContentBlock:

        items = items or []
        headers = headers or []
        rows = rows or []

        # Collect textual content for
        # language/direction detection.
        language_parts: list[str] = []

        if text:
            language_parts.append(
                text
            )

        language_parts.extend(
            items
        )

        language_parts.extend(
            headers
        )

        for row in rows:
            language_parts.extend(
                row
            )

        if alt:
            language_parts.append(
                alt
            )

        language_text = "\n".join(
            language_parts
        )

        return ContentBlock(
            id=self._stable_id(
                "block",
                scope_id,
                str(index),
                block_type,
                str(start_line),
            ),
            type=block_type,
            text=text,
            info=info,
            items=items,
            alt=alt,
            src=src,
            headers=headers,
            rows=rows,
            language=detect_language(
                language_text
            ),
            direction=source_direction or detect_direction(language_text),
            start_line=start_line,
            end_line=end_line,
        )

    @staticmethod
    def _source_directions(metadata: dict[str, Any]) -> dict[str, Direction]:
        result: dict[str, Direction] = {}
        layout = metadata.get("paragraph_layout", [])
        if not isinstance(layout, list):
            return result
        for item in layout:
            if not isinstance(item, dict) or item.get("direction") != "rtl":
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                result[StructuralAnalyzer._normalise_source_text(text)] = "rtl"
        return result

    @staticmethod
    def _source_direction_for(
        source_directions: dict[str, Direction],
        *values: str,
    ) -> Direction | None:
        directions = {
            source_directions.get(StructuralAnalyzer._normalise_source_text(value))
            for value in values
            if value.strip()
        }
        directions.discard(None)
        return "rtl" if directions == {"rtl"} else None

    @staticmethod
    def _normalise_source_text(value: str) -> str:
        return " ".join(value.split())

    # =========================================================
    # STABLE IDS
    # =========================================================

    @staticmethod
    def _stable_id(
        *parts: str,
    ) -> str:
        digest = hashlib.sha1(
            "\x1f"
            .join(parts)
            .encode("utf-8")
        ).hexdigest()[:12]

        return digest
