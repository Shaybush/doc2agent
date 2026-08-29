from __future__ import annotations

import json
import re
import shutil
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree

SUPPORTED_EXTENSIONS = {".doc", ".docx", ".rtf"}
_IMAGE_LINK_RE = re.compile(r"(!\[[^\]]*\]\()([^\s)]+)([^)]*\))")
_WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


@dataclass(frozen=True)
class ConverterResult:
    source: Path
    markdown: Path
    media_dir: Path
    metadata: Path


def safe_name(value: str) -> str:
    """Create a filesystem-safe name without stripping Hebrew/Unicode letters."""

    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    return normalized or "document"


class Converter:
    """Faithful DOC/DOCX/RTF -> raw Markdown converter.

    This stage intentionally does not detect language, infer requirements, translate,
    repair RTL/BIDI, or restructure business meaning.
    """

    def convert(self, source: str | Path, output_root: str | Path = "output") -> ConverterResult:
        source = Path(source)
        if not source.exists():
            raise FileNotFoundError(source)
        if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ValueError(
                f"Unsupported source format: {source.suffix}. "
                f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            )

        try:
            import aspose.words_foss as aw
        except ImportError as exc:  # pragma: no cover - depends on external package
            raise RuntimeError(
                "Aspose.Words FOSS is not installed. Run: pip install -e ."
            ) from exc

        output_root = Path(output_root)
        raw_dir = output_root / "raw"
        metadata_dir = output_root / "metadata"
        media_root = output_root / "media"
        raw_dir.mkdir(parents=True, exist_ok=True)
        metadata_dir.mkdir(parents=True, exist_ok=True)
        media_root.mkdir(parents=True, exist_ok=True)

        stem = safe_name(source.stem)
        markdown_path = raw_dir / f"{stem}.md"
        media_dir = media_root / stem
        media_dir.mkdir(parents=True, exist_ok=True)

        before_raw_files = {p.resolve() for p in raw_dir.rglob("*") if p.is_file()}

        doc = aw.Document(str(source))
        save_options = aw.saving.MarkdownSaveOptions()
        # These settings are supported by the API and keep the desired contract.
        # Current FOSS builds may still place media next to the Markdown; we normalize that below.
        save_options.images_folder = str(media_dir)
        save_options.images_folder_alias = f"../media/{stem}"
        doc.save(str(markdown_path), save_options)

        self._normalize_media(raw_dir, markdown_path, media_dir, before_raw_files)

        metadata_path = metadata_dir / f"{stem}.json"
        metadata = {
            "schema_version": "1.0",
            "source": str(source),
            "source_format": source.suffix.lower().lstrip("."),
            "markdown": str(markdown_path),
            "media": str(media_dir),
            "language": "und",
            "direction": "auto",
            "paragraph_layout": self._docx_paragraph_layout(source),
            "converter": "aspose-words-foss",
        }
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        return ConverterResult(
            source=source,
            markdown=markdown_path,
            media_dir=media_dir,
            metadata=metadata_path,
        )

    @staticmethod
    def _docx_paragraph_layout(source: Path) -> list[dict[str, str]]:
        """Preserve DOCX paragraph direction that Markdown cannot encode."""

        if source.suffix.lower() != ".docx":
            return []

        try:
            with zipfile.ZipFile(source) as archive:
                document_xml = archive.read("word/document.xml")
        except (KeyError, zipfile.BadZipFile):
            return []

        namespace = f"{{{_WORD_NAMESPACE}}}"
        root = ElementTree.fromstring(document_xml)
        layouts: list[dict[str, str]] = []
        for paragraph in root.iter(f"{namespace}p"):
            properties = paragraph.find(f"{namespace}pPr")
            if properties is None or properties.find(f"{namespace}bidi") is None:
                continue
            text = "".join(
                node.text or ""
                for node in paragraph.iter(f"{namespace}t")
            ).strip()
            if not text:
                continue
            alignment = properties.find(f"{namespace}jc")
            layouts.append(
                {
                    "text": text,
                    "direction": "rtl",
                    "alignment": (
                        alignment.get(f"{namespace}val", "")
                        if alignment is not None
                        else ""
                    ),
                }
            )
        return layouts

    def _normalize_media(
        self,
        raw_dir: Path,
        markdown_path: Path,
        media_dir: Path,
        before_raw_files: set[Path],
    ) -> None:
        """Move any converter-emitted media beside raw Markdown into output/media/<doc>."""

        emitted: dict[str, str] = {}
        after = [p for p in raw_dir.rglob("*") if p.is_file()]
        for path in after:
            resolved = path.resolve()
            if resolved in before_raw_files or resolved == markdown_path.resolve():
                continue
            target = media_dir / path.name
            if target.exists():
                target = self._deduplicate_path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target))
            emitted[path.name] = target.name

        if not emitted or not markdown_path.exists():
            return

        markdown = markdown_path.read_text(encoding="utf-8")
        relative_prefix = f"../media/{media_dir.name}/"

        def replace(match: re.Match[str]) -> str:
            target = unquote(match.group(2)).replace("\\", "/")
            base = Path(target).name
            if base not in emitted:
                return match.group(0)
            return f"{match.group(1)}{relative_prefix}{emitted[base]}{match.group(3)}"

        markdown_path.write_text(_IMAGE_LINK_RE.sub(replace, markdown), encoding="utf-8")

    @staticmethod
    def _deduplicate_path(path: Path) -> Path:
        index = 2
        while True:
            candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
            if not candidate.exists():
                return candidate
            index += 1
