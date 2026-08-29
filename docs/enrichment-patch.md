# `EnrichmentPatch` contract

`EnrichmentPatch` is the only future LLM/provider output accepted after the
deterministic pipeline. It is not a replacement for `document.json` and cannot
contain structural blocks, raw Markdown, or source-file content.

## Required identity and scope

Every patch contains the exact `source_id`, `structural_sha256`, a non-empty
list of allowed evidence-scope content-block IDs, and a non-empty
`reviewed_block_ids` list. `reviewed_block_ids` must contain every content block
in the structural document exactly once; it is an auditable coverage declaration,
not evidence scope. Each proposed fact includes at least one `EvidenceRef`. The
validator rejects patches when their source, structural revision, scope, review
coverage, evidence, or local fact IDs are invalid. An empty `facts` list remains
valid only after every content block was declared reviewed.

```json
{
  "schema_version": "2.0",
  "source_id": "sha256:...",
  "structural_sha256": "...",
  "scope_block_ids": ["block:..."],
  "reviewed_block_ids": ["block:...", "block:..."],
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

`schema_version` is required and must be exactly `"2.0"`. Earlier patch
versions are rejected, because they do not provide auditable review coverage.

`kind` is one of `purpose`, `requirement`, `decision`, `action_item`, or
`architecture_pipeline`. The last kind preserves explicit descriptions of system
architecture or pipeline steps; its value remains exact source wording and must
be linked to evidence.

An `architecture_pipeline` fact derived from a table row may additionally carry
an optional `pipeline_step` with exact `component`, `input`, and `output` cell
values. Its evidence must include that table block, whose unique `Component`,
`Input`, and `Output` headers identify a row matching all three values exactly.
Ordinary architecture facts remain value-and-evidence facts without this field.

Validate a patch without calling an LLM:

```bash
doc2agent validate-patch patch.json --document structured.document.json
```

A valid patch is still only a proposal. Candidate creation, approval, and
publication are separate future stages.
