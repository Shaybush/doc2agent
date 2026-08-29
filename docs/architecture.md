# Architecture

Current implementation boundary:

```text
DOCX
      ↓
DocxPreflight
      ↓
storage/sources/<source_id>/<original-file-name>.docx
storage/source-catalog.json
      ↓
DOC / DOCX / RTF
      ↓
Converter
      ↓
output/raw/<document>.md
output/media/<document>/...
output/metadata/<document>.json
      ↓
MarkdownNormalizer  (deterministic)
      ↓
output/normalized/<document>.md
      ↓
StructuralAnalyzer  (rules only, no LLM)
      ↓
output/structured/<document>.document.json
      ↓
DocumentValidator  (rules only, no semantic inference)
      ↓
output/validation/<document>.validation.json
      ↓
SecurityGateway
      ↓
output/security/<document>.security.json
```

The current pipeline returns a readiness status from both validation and
security. Documents with an `invalid` or `blocked` status must not be sent to
an LLM; a `ready_with_redactions` document must be read from the security
report's `sanitized_document` field.

`DocxPreflight` is a separate ingestion boundary. It does not extract package
contents or fetch external relationships. It classifies a DOCX as `pass`,
`requires_review`, or `block`; `pass` and an explicitly approved
`requires_review` result may be stored. The source catalog is a
small JSON index for now. It records source versions by content hash and a
stable logical document ID; SQLite is deferred until concurrent jobs or more
durable state management are required.

`Doc2AgentPipeline` accepts only an already accepted ingested source. This
prevents programmatic callers from reaching Converter with a rejected or
unreviewed DOCX.

Explicit semantic and publication workflow (not part of deterministic ingest):

```text
document.json
    ↓
sanitized_document + configured provider
    ↓
EnrichmentPatch → validation → candidate + EvidenceRef proof
    ↓
human approval
    ↓
approved knowledge.json → TopicRenderer → Copilot Markdown
```

The Web UI and CLI both invoke this workflow through domain services. A provider
can propose facts only; it cannot alter conversion, canonical `document.json`,
or the approved snapshot. It is an explicit configured action, not an automatic
pipeline stage.

## Principles

- Canonical knowledge stays product-owned.
- Converter preserves source faithfully and is language-agnostic.
- Structural analysis is deterministic and testable.
- Hebrew/RTL is preserved; direction is metadata only.
- No LLM is used before the semantic stage.
- RAG and graph systems are replaceable consumers, not the source of truth.
- The secure ingest/pipeline boundary is DOCX-only; DOC/RTF remain
  direct-converter formats until they receive equivalent preflight handling.
