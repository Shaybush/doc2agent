# `EnrichmentPatch` contract

`EnrichmentPatch` is the only future LLM/provider output accepted after the
deterministic pipeline. It is not a replacement for `document.json` and cannot
contain structural blocks, raw Markdown, or source-file content.

## Required identity and scope

Every patch contains the exact `source_id`, `structural_sha256`, and a non-empty
list of allowed content-block IDs. Each proposed fact includes at least one
`EvidenceRef`. The validator rejects patches when their source, structural
revision, scope, evidence, or local fact IDs are invalid.

```json
{
  "schema_version": "1.0",
  "source_id": "sha256:...",
  "structural_sha256": "...",
  "scope_block_ids": ["block:..."],
  "facts": [
    {
      "id": "fact:requirement-1",
      "kind": "requirement",
      "value": "A normalized statement.",
      "evidence": [{
        "source_id": "sha256:...",
        "block_id": "block:...",
        "structural_sha256": "...",
        "support": "explicit"
      }]
    }
  ]
}
```

Validate a patch without calling an LLM:

```bash
doc2agent validate-patch patch.json --document structured.document.json
```

A valid patch is still only a proposal. Candidate creation, approval, and
publication are separate future stages.
