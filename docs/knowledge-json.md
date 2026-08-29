# `knowledge.json` contract

`knowledge.json` is an optional semantic wrapper around the canonical
`document.json`. It does not replace or alter the deterministic source
structure.

## Shape

```json
{
  "schema_version": "1.0",
  "document": { "...": "canonical document.json content" },
  "provider": "fixture",
  "annotations": [
    {
      "id": "annotation:purpose",
      "kind": "requirement",
      "value": "The service must preserve source text.",
      "evidence": [{
        "source_id": "sha256:...",
        "block_id": "block:example",
        "structural_sha256": "...",
        "support": "explicit"
      }],
      "origin": "VERIFIED"
    }
  ]
}
```

## Invariants

- `document` is a deep copy of the supplied canonical document.
- Every annotation has a unique `id`.
- Each `EvidenceRef` binds a fact to the source SHA-256, a content-block ID,
  and the exact structural revision of the embedded canonical document.
- `source_ids` remains temporarily supported for migration, but new producers
  must write `evidence`.
- `provider` identifies the component that produced the annotations.
- `origin` is `VERIFIED` for externally verified knowledge or `AI_INFERRED`
  for provider inference. Consumers can apply stricter policies to inferred
  annotations without discarding the source document.

## Annotation contract v1

| Field | Rule |
| --- | --- |
| `id` | Non-empty and unique within `annotations`; owned by the provider. |
| `kind` | One of `purpose`, `requirement`, `decision`, `action_item`, or `architecture_pipeline`. |
| `value` | Non-empty normalized statement, without inventing facts absent from its evidence. |
| `pipeline_step` | Optional exact `component`, `input`, and `output` from an evidence-table row; only for `architecture_pipeline`. |
| `evidence` | New contract: at least one `EvidenceRef` for a valid source block and structural revision. |
| `source_ids` | Legacy migration field; IDs must reference the embedded canonical document. |
| `origin` | `AI_INFERRED` for a provider's extraction; `VERIFIED` only after an external verification step. |

The initial evaluation set contains nine expected annotations: requirement,
decision, and action item in Russian, English, and Hebrew. It is stored in
`tests/fixtures/semantic-evaluation-cases.json` with corresponding source text
in `tests/fixtures/semantic-evaluation.md`. A future LLM provider must be
evaluated against this set before it is introduced into runtime processing.

`architecture_pipeline` is for explicit descriptions of the system architecture
or pipeline steps. It preserves the exact source wording and evidence like every
other annotation; it does not let the semantic layer infer new architecture.
`purpose` is reserved for explicit purpose or goal statements, rather than a
requirement that describes an expected capability or constraint.

## Offline evaluation

`SemanticEvaluationRunner` compares a provider's annotations with the fixture
set using exact `kind`, `value`, `origin`, and `source_ids` matches. Its JSON
report records matched cases, missing cases, unexpected annotations, and an
overall `passed` flag.

Run the deterministic fixture baseline with:

```bash
doc2agent evaluate-semantic tests/fixtures/semantic-evaluation.md \
  --cases tests/fixtures/semantic-evaluation-cases.json
```

This command intentionally uses the fixture provider, so it is a deterministic
contract smoke test—not a DeepSeek quality result. A real provider must be
evaluated programmatically on a versioned multilingual golden corpus with
recorded model, prompt, configuration, and corpus versions. The gate must fail
on invalid evidence, wrong source/structural hashes, unsupported facts, or
metrics below an explicit threshold.

## Providers

`SemanticAnalyzer` accepts a narrow `SemanticProvider` interface. The default
`NoOpSemanticProvider` returns no annotations. `FixtureSemanticProvider` is a
deterministic test utility. Neither makes a network call or uses an LLM.

A future LLM provider must produce annotations through this interface and must
not modify `document.json`, Converter output, or structural analysis.

## Candidate before approval

`create-candidate` converts only a validated `EnrichmentPatch` into a
hash-addressed `KnowledgeCandidate`. It deep-copies the canonical document and
marks proposed annotations as `AI_INFERRED`. The candidate is not published,
does not change an approved snapshot, and cannot be consumed as Copilot
context until a later approval step.

The candidate also persists the patch's complete `reviewed_block_ids` coverage
declaration. It is included in the candidate hash and shown in the human review
proof, so approval can audit that every structural content block was reviewed.
Candidates produced under this contract have required `schema_version: "2.0"`.
Earlier candidate snapshots are intentionally not read as approved candidates;
they must be regenerated and approved under the v2 contract.

```bash
doc2agent create-candidate patch.json --document structured.document.json
doc2agent review-candidate candidate.knowledge.json
```

`review-candidate` is read-only. It displays every proposed fact beside the
exact supporting source block, its section path, and source lines, so a reviewer
does not need to inspect JSON IDs before deciding whether to approve it.

When the document came from `pipeline`, `enrich` writes its patch to that
revision's `storage/artifacts/<source-version>/enrichment/` directory and
`create-candidate` writes its candidate to `candidate/`. These are internal
application artifacts, not files a user needs to browse. Existing history is
preserved; retention and automatic cleanup are a separate configured policy.

For `explicit` evidence, a fact value must preserve an exact phrase from its
source block, including the original language and script. Translations and
paraphrases are rejected by patch validation; any future translation belongs in
a separate derived display field, never in the source-linked fact.

## Approval and rollback

Only a candidate whose content hash, source revision, annotations, and evidence
pass validation may be approved. The first release accepts only `explicit`
evidence. Approval archives the immutable candidate in the folder of its logical
document, `storage/approved/<document-key>/<candidate_sha256>.knowledge.json`,
and atomically replaces only that document's
`storage/approved/<document-key>/current.knowledge.json`. `document-key` is a
filesystem-safe encoding of `document_id` with a short hash suffix; commands
resolve it automatically. It also appends the
logical document ID, human actor, and time to `storage/approval-log.jsonl`.

```bash
doc2agent approve candidate.knowledge.json --storage storage --by "george"
doc2agent rollback --candidate-hash <candidate_sha256> --document-id "document:..." --storage storage --by "george"
```

Rollback chooses a previously archived approved candidate and atomically makes
it current. It never edits the archive or rewrites the audit history.

## Model enrichment providers

`enrich` invokes a configured model only after deterministic validation passes
and Security Gateway has not blocked the revision. It sends the sanitized
document from `security.json`, not the original input artifact. The model may
return only an `EnrichmentPatch`; the existing patch validator still checks it
before it can become a candidate.

DeepSeek is the first profile, using `DEEPSEEK_API_KEY`, the default endpoint
`https://api.deepseek.com`, and `deepseek-v4-pro`. The underlying adapter uses
the OpenAI-compatible chat-completions format, so another compatible service
can be selected with `--provider`, `--model`, `--base-url`, and `--api-key-env`.
Secrets are read only from the named environment variable and are never CLI
arguments or stored in artifacts.

## Copilot context

`render-copilot` reads the current approved snapshot for one logical document and
generates `storage/copilot-context/<source-name>--<document-id>/INDEX.md`, a
Markdown file named after the source document, and `manifest.json`. The public
context path is atomically switched to a complete generated version.
The Markdown header identifies the approved candidate and source revision.
No wall-clock publication date is included: this keeps identical approved input
byte-for-byte deterministic.

An explicit `--approved` path is accepted only when it is that document's current
snapshot or an archived approved snapshot; a candidate file cannot bypass the
human-approval boundary. Fact values are rendered as their exact canonical text. The generated Markdown
does not add HTML or invisible direction-control characters, so mixed LTR/RTL
phrases remain unchanged for Copilot and other consumers.

```bash
doc2agent render-copilot --document-id "document:..."
```
