# Pipeline and artifact contract

`doc2agent pipeline <source>` accepts a DOCX and processes its immutable copy
into deterministic artifacts under `storage/artifacts/<source_id>/` by default.
The pipeline API accepts an explicit output root for programmatic use, but it
requires a successful `IngestResult`; an arbitrary DOCX path is rejected.

For DOCX, `doc2agent pipeline <source>` runs this ingest step automatically.
`doc2agent ingest <source>` is available when only acceptance and storage are
needed. Ingest validates the package without extracting it, stores an immutable
copy by content hash, and records a logical document ID in
`storage/source-catalog.json`. An edited DOCX accepted again from the same path
is automatically added as its next version. The CLI then rebuilds all
deterministic artifacts for that version and records the artifact root in the
catalog. `--document-id` is an advanced override for a renamed or moved source.

The lower-level `convert` CLI command also runs ingest first. This keeps DOCX
preflight as a domain boundary rather than a CLI-only convention.

If ingest reports `requires_review` for an external relationship, the CLI stops
before Converter. After inspecting the diagnostic, rerun `pipeline` with
`--approve-review` to record the explicit approval and process the DOCX. This
approval never fetches or opens the external relationship.

## Version history and deletion

The catalog keeps earlier source versions and their artifacts so that every
`document.json` remains traceable to its original DOCX. The latest accepted
version is marked as current. History is not an indefinite retention policy:
old versions are never removed automatically. `doc2agent delete --document-id
<id>` first shows the affected files. Adding `--yes` removes the document from
the active catalog and moves its source copies and artifacts to the internal
`storage/.trash/` directory. It also moves that document's approved snapshots,
Copilot context versions, and matching approval-log entries to the same trash
item, so deleted knowledge cannot remain publishable. A later retention or purge
policy may remove trash permanently.

## Processing sequence

```text
Office document
  -> source manifest
  -> Converter
  -> raw Markdown, media, metadata
  -> MarkdownNormalizer
  -> normalized Markdown
  -> StructuralAnalyzer
  -> document.json
  -> DocumentValidator
  -> validation.json
  -> SecurityGateway
  -> security.json and readiness status
```

The Converter preserves source content. The normalizer, structural analyzer,
and validator are deterministic and do not use an LLM. Semantic enrichment is
not part of this pipeline.

## Artifacts

| Path | Producer | Purpose |
| --- | --- | --- |
| `source-manifest/<document>.source-manifest.json` | Pipeline | Immutable source identity: SHA-256, file name, size, MIME type, and intake time. |
| `raw/<document>.md` | Converter | Faithful Markdown conversion of the source document. |
| `media/<document>/` | Converter | Media extracted from the source document. |
| `metadata/<document>.json` | Converter | Minimal conversion metadata used with structural analysis. |
| `normalized/<document>.md` | MarkdownNormalizer | Deterministically normalized Markdown used as the analyzer input. |
| `structured/<document>.document.json` | StructuralAnalyzer | Canonical structural representation; see [document-json.md](document-json.md). |
| `validation/<document>.validation.json` | DocumentValidator | Deterministic validation report with `valid`, `errors`, and `warnings`. |
| `security/<document>.security.json` | SecurityGateway | Security decision, findings, and `sanitized_document`. |

All JSON is UTF-8. `document.json` contains the matching `source_manifest` and
`structural_sha256`; the latter is computed from canonical JSON with its own
field and the intake timestamp omitted. Consumers should preserve the identifiers, source ranges,
Unicode text, and direction metadata in `document.json`.

## Readiness for an LLM consumer

The pipeline status combines validation and security decisions. A security
block takes precedence when both gates fail, while the reports preserve the
full detail from each gate.

| Validation | Security decision | Pipeline status | Consumer rule |
| --- | --- | --- | --- |
| invalid | `pass` or `redact` | `invalid` | Do not send the document to an LLM. |
| valid | `pass` | `ready` | The canonical document may be used. |
| valid | `redact` | `ready_with_redactions` | Use `sanitized_document` from `security.json`, not the original document. |
| any | `block` | `blocked` | Do not send the document to an LLM. |

`PipelineResult.validation` exposes the validation report to programmatic
consumers; the corresponding JSON artifact remains available for audit.

## Boundaries

- `Converter` must not translate, infer meaning, or alter RTL/BIDI text.
- `StructuralAnalyzer` defines structure only; it contains no LLM behavior.
- `DocumentValidator` reports deterministic contract issues and makes no
  semantic inference.
- Future semantic enrichment belongs in `SemanticAnalyzer`, after this
  deterministic and security-reviewed boundary.
