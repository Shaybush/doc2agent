from __future__ import annotations

from pathlib import Path


def artifact_root_for_document(document_path: str | Path, source_id: str) -> Path:
    """Return the application-owned artifact root for one document revision."""

    document_path = Path(document_path)
    if document_path.parent.name == "structured":
        return document_path.parent.parent
    return Path("storage/artifacts") / source_id.replace(":", "-")


def document_artifact_name(document_path: str | Path) -> str:
    """Return a stable artifact basename without the document JSON suffix."""

    name = Path(document_path).name
    if name.endswith(".document.json"):
        return name.removesuffix(".document.json")
    return Path(name).stem
