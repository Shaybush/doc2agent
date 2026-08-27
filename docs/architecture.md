# Architecture

Current implementation boundary:

```text
DOC / DOCX / RTF
      ↓
Converter
      ↓
output/raw/<document>.md
output/media/<document>/...
output/metadata/<document>.json
      ↓
StructuralAnalyzer  (rules only, no LLM)
      ↓
output/structured/<document>.document.json
```

Planned later stages:

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

## Principles

- Canonical knowledge stays product-owned.
- Converter preserves source faithfully and is language-agnostic.
- Structural analysis is deterministic and testable.
- Hebrew/RTL is preserved; direction is metadata only.
- No LLM is used before the semantic stage.
- RAG and graph systems are replaceable consumers, not the source of truth.
