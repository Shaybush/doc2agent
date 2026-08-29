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
`requires_review`, or `block`; only `pass` is stored. The source catalog is a
small JSON index for now. It records source versions by content hash and a
stable logical document ID; SQLite is deferred until concurrent jobs or more
durable state management are required.

`Doc2AgentPipeline` accepts only an already accepted ingested source. This
prevents programmatic callers from reaching Converter with a rejected or
unreviewed DOCX.

Isolated semantic contract (not part of the runtime pipeline):

```text
document.json
    ↓
SemanticAnalyzer (LLM/provider abstraction)
    ↓
knowledge.json
    ├── Copilot/agent-ready export
    ├── RAG/Search adapter
    └── Graph adapter
```

The current implementation supplies only deterministic no-op and fixture
providers. A future LLM provider must use this boundary rather than altering
conversion or structural analysis.

## Principles

- Canonical knowledge stays product-owned.
- Converter preserves source faithfully and is language-agnostic.
- Structural analysis is deterministic and testable.
- Hebrew/RTL is preserved; direction is metadata only.
- No LLM is used before the semantic stage.
- RAG and graph systems are replaceable consumers, not the source of truth.
