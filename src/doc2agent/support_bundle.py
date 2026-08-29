from __future__ import annotations

import json
import platform
import re
import sys
import zipfile
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from doc2agent import __version__
from doc2agent.ingest import SourceCatalog, SourceCatalogEntry
from doc2agent.security import SecurityResult
from doc2agent.validator import ValidationReport


class SupportBundleExporter:
    """Create a portable, content-free diagnostic bundle for one document."""

    def __init__(self, storage_root: str | Path = "storage") -> None:
        self.storage_root = Path(storage_root)

    def export(
        self,
        document_id: str,
        *,
        output: str | Path | None = None,
    ) -> Path:
        catalog = self._read_catalog()
        document = catalog.documents.get(document_id)
        if document is None:
            raise ValueError(f"Unknown document id: {document_id}")

        output_path = (
            Path(output) if output is not None else self._default_output(document_id)
        )
        if output_path.exists():
            raise ValueError(f"Support bundle already exists: {output_path}")

        diagnostics = [
            self._source_diagnostics(source_id, catalog.sources[source_id])
            for source_id in document.source_ids
        ]
        manifest = {
            "schema_version": "1.0",
            "bundle_kind": "doc2agent-support",
            "generated_at": datetime.now(UTC).isoformat(),
            "doc2agent_version": __version__,
            "python_version": sys.version.split()[0],
            "platform": platform.platform(),
            "document_id": document_id,
            "source_ids": document.source_ids,
            "privacy": {
                "includes_original_source": False,
                "includes_document_content": False,
                "includes_sanitized_document": False,
                "includes_error_logs": False,
            },
        }
        entries = {
            "support-manifest.json": self._json_bytes(manifest),
            "diagnostics.json": self._json_bytes({"sources": diagnostics}),
        }
        entries["checksums.json"] = self._json_bytes(
            {
                name: sha256(contents).hexdigest()
                for name, contents in sorted(entries.items())
            }
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(
            output_path,
            mode="x",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            for name, contents in sorted(entries.items()):
                archive.writestr(name, contents)
        return output_path

    def _source_diagnostics(
        self,
        source_id: str,
        entry: SourceCatalogEntry,
    ) -> dict[str, object]:
        artifact_root = self.storage_root / "artifacts" / source_id.replace(":", "-")
        validation = self._validation_summary(artifact_root)
        security = self._security_summary(artifact_root)
        return {
            "source_id": source_id,
            "received_at": entry.received_at,
            "preflight_decision": entry.preflight_decision,
            "review_approved": entry.review_approved,
            "artifacts_present": artifact_root.is_dir(),
            "validation": validation,
            "security": security,
        }

    @staticmethod
    def _validation_summary(artifact_root: Path) -> dict[str, object] | None:
        paths = sorted((artifact_root / "validation").glob("*.validation.json"))
        if not paths:
            return None
        try:
            report = ValidationReport.model_validate_json(paths[-1].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"status": "malformed"}
        return {
            "valid": report.valid,
            "errors": [issue.code for issue in report.errors],
            "warnings": [issue.code for issue in report.warnings],
        }

    @staticmethod
    def _security_summary(artifact_root: Path) -> dict[str, object] | None:
        paths = sorted((artifact_root / "security").glob("*.security.json"))
        if not paths:
            return None
        try:
            result = SecurityResult.model_validate_json(paths[-1].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"status": "malformed"}
        return {
            "decision": result.decision,
            "findings": [
                {"kind": finding.kind, "action": finding.action, "path": finding.path}
                for finding in result.findings
            ],
        }

    def _read_catalog(self) -> SourceCatalog:
        path = self.storage_root / "source-catalog.json"
        if not path.is_file():
            raise ValueError("Source catalog was not found")
        return SourceCatalog.model_validate_json(path.read_text(encoding="utf-8"))

    def _default_output(self, document_id: str) -> Path:
        document_key = re.sub(r"[^A-Za-z0-9._-]+", "-", document_id).strip(".-")
        document_key = document_key or "document"
        return Path("support-bundles") / (
            f"doc2agent-support-{document_key}-{uuid4().hex}.zip"
        )

    @staticmethod
    def _json_bytes(value: object) -> bytes:
        return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
