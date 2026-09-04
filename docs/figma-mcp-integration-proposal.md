# Figma MCP integration proposal

**Status:** Proposed
**Scope:** optional, read-only design context for one logical technical specification

## Purpose

Doc2Agent already turns a versioned technical specification into reviewed and
approved context for Copilot. This proposal adds a separately reviewed link
between approved requirements and selected Figma screens. It must make the
implementation context more precise without treating a design as a replacement
for the specification.

The first scenario is deliberately small: a Hebrew specification for a
registration form and two Figma frames, `Registration` and `Registration —
invalid email`.

## Non-goals

- Do not change `Converter`, `StructuralAnalyzer`, `document.json`, or the
  meaning of `EvidenceRef`.
- Do not infer a requirement from a visual design.
- Do not write to Figma, create Figma files, or modify a canvas.
- Do not automatically approve a requirement-to-design relationship.
- Do not put raw Figma MCP responses, tokens, or a full design export into
  Copilot Markdown.

## Architecture boundary

Figma MCP is a transport to obtain design data. It is not a pipeline stage that
rewrites DOCX artifacts.

```text
DOCX -> ingest -> deterministic analysis -> candidate -> knowledge approval
                                           |
                                           | optional
                                           v
Figma link -> read-only adapter -> immutable design snapshot
                                           |
                                           v
                            design-alignment review and approval
                                           |
                                           v
                    approved knowledge + approved design alignment
                                           |
                                           v
                              generated Copilot Markdown
```

An MCP connection available to a Codex agent is not automatically available to
the Doc2Agent FastAPI process. The design domain therefore consumes a normalized
snapshot through an interface. A fixture or explicit imported snapshot can be
used for the proof of concept; a server-side MCP adapter is a later deployment
decision.

## Versioned artifacts

The proposed design domain has four artifacts.

### Figma reference

A human-selected, live pointer scoped to a logical document:

```json
{
  "document_id": "document:registration",
  "figma_url": "https://www.figma.com/design/...",
  "figma_file_key": "abc123",
  "selected_node_ids": ["12:34", "12:56"]
}
```

The person using the UI selects a frame by name or pastes its ordinary Figma
link. The system extracts and stores technical node IDs; the user never enters
them manually.

### Figma snapshot

An immutable normalized result for the selected Figma nodes. It contains the
file key, names, node IDs, hierarchy, source links, optional previews, capture
time, and an SHA-256 of canonical content. It is not a mutable copy of the
whole Figma file.

### Design-alignment candidate

A proposed mapping from one semantic annotation in a particular knowledge
candidate to one selected Figma node. It always records:

- `source_id` and `candidate_sha256` of the requirements being reviewed;
- `figma_snapshot_sha256`;
- annotation ID and source evidence block ID;
- selected Figma node ID;
- one explicit review status.

### Approved design alignment

An immutable, reviewer-approved record of the candidate mapping. It has its own
approval actor and time. It does not modify `knowledge.json`.

## Review workflow

After candidate generation, the UI asks one optional question:

```text
Does this specification have Figma designs?
[Connect] [No design] [Later]
```

When a design is connected, the reviewer works through small cards, not an
entire document beside an entire Figma project.

```text
Requirement
כתובת אימייל לא תקינה מציגה הודעת שגיאה.

Proof
Validation, lines 18–19.

Design
Registration — invalid email.

[Matches] [Partial] [No design] [Needs clarification]
```

`No design` and `Later` are explicit decisions. They do not block the existing
document-only workflow.

## Approval and publication

There are two approvals, not one combined decision:

1. `ApprovalService` approves the semantic knowledge candidate from the DOCX.
2. A design-alignment approval approves the mapping between that exact candidate
   and that exact Figma snapshot.

Copilot Markdown is published from approved knowledge plus, when present, an
approved design alignment. The Figma section is compact:

```md
## Figma designs

### Registration — invalid email

- Frame: [Registration — invalid email](https://www.figma.com/design/...)
- Snapshot: `sha256:...`
- Covers: invalid-email validation requirement.
```

Only approved mappings render. Proposed, rejected, or stale mappings never
render as confirmed design guidance.

## Change handling

- A new DOCX source version does not overwrite older design records. Mappings
  to changed requirements become `stale` and require review.
- A newly captured Figma snapshot does not overwrite the earlier snapshot.
  Mappings to the previous snapshot become `stale`.
- Existing published Markdown remains unchanged until a reviewer explicitly
  approves and publishes a new pair of artifacts.

## Security and privacy

- Accept only an explicitly supplied Figma URL and validate its format before
  contacting an adapter.
- Use read-only Figma operations in the initial implementation.
- Keep MCP credentials, session data, and raw adapter responses out of storage,
  generated Markdown, and logs.
- Treat snapshots and previews as sensitive project artifacts; exclude them
  from content-safe support exports.
- Include design artifacts in the existing reversible logical-document deletion
  workflow before production rollout.

## MVP acceptance scenario

The first end-to-end fixture contains:

```text
Figma frames
- Registration
- Registration — invalid email

Hebrew requirements
- שם מלא, אימייל וסיסמה
- אימייל הוא שדה חובה
- אימייל לא תקין מציג הודעת שגיאה
- לא ניתן לשלוח כאשר יש שדות חובה ריקים
```

The MVP succeeds when:

1. two selected frames are represented by an immutable snapshot;
2. every in-scope requirement has a visible review decision;
3. Hebrew remains readable in review, JSON, and Markdown;
4. only approved mappings appear in the rendered Copilot context;
5. snapshot or specification changes mark affected mappings stale;
6. document-only processing continues unchanged.

## Implementation sequence

1. Validate the available Figma MCP access path and capture a sample snapshot.
2. Add isolated design models, canonical hashing, and fixture-based tests.
3. Add design-alignment validation and separate approval storage.
4. Extend the renderer with approved design references only.
5. Add the thin HTMX flow: connect design, choose frames, review cards, and
   explicit publication.
6. Add a server-side MCP adapter only after its supported deployment and
   credential boundary are confirmed.

## Open decisions

- Which supported Figma MCP access path can the local web server use without
  relying on an interactive Codex session?
- Is a preview image required for MVP, or are names plus Figma links sufficient?
- Which requirement annotations are in the design-review scope by default?
- Should an approved design alignment be mandatory before publication when a
  user has explicitly attached a Figma reference?
