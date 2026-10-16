# FreeCAD AI Studio — Development

## V10 — current

- Permanent executable `freecad_studio.py`; version shown in title and header.
- External `config.json` loaded on startup; sample `config.example.json` committed; local settings ignored by Git.
- Native physical threads integrated via a dedicated new-block modelling tool (multiple holes, one common ISO metric size/pitch), not arbitrary LLM-authored helices.
- No verified conversion of existing holes; avoid claiming it works until separately tested.
- Validate against real FreeCAD 1.1.4 before calling physical threads production-ready.


## Baseline

- V9: Native PartDesign::Hole M10 thread test and safeguard against unverified physical thread operations (requires live testing).
- V8: Tkinter UI, dark theme, history, streaming, MCP interaction and bounded geometry verification.
- Source entry point: `freecad_studio.py` (V8 and V9 remain available as historical versions).
- Environment: FreeCAD MCP bridge on macOS; Ollama accessible via HTTP.

## Current priorities

1. Validate V9 native M10 modelled-thread test and then V8 against the live FreeCAD bridge and record reproducible acceptance tests.
2. Strengthen rollback: independent saved-document checkpoints and restoration after failed geometry generation, rather than relying solely on undo transactions.
3. Introduce an allowlist for generated Python operations and prevent destructive document closures without approval.
4. Verify engineering thread annotations by standard, size, pitch, depth and correct feature linkage.
5. Treat physical thread generation as experimental; verify helical geometry and final positive-volume solid before PASS.
6. Detect repeated unsuccessful modelling strategies and stop without accumulating duplicate or invalid objects.
7. Add automated tests and GitHub Actions CI, including mocked MCP inspection results.
8. Move endpoint/model preferences into user configuration rather than code constants.

## Known failure case

A 100 × 60 × 40 mm block with four corner M10×1.5 threaded holes experienced repeated failures when attempting physical helical threads. Initial plain 10 mm cylinders were incorrectly treated as threads. Later attempts created invalid negative-volume or null final solids. Preserve the last valid model and explicitly distinguish tap-drill geometry, thread metadata and physical threads.

## Planned feature — CAD Knowledge & Learning Engine

**Status:** Proposed / not implemented. **Scope:** Local-first knowledge base for the FreeCAD Studio Ollama + MCP workflow. Keep separate from hard-coded demonstration geometry: the selected LLM should retrieve guidance and generate/adapt operations, while MCP performs execution.

### Objectives

- Give the LLM relevant FreeCAD Python/API and MCP examples, modelling techniques, design rules, repairs, and known pitfalls before each modelling stage.
- Avoid repeating previously encountered mistakes; reuse validated, parameterised operations (e.g. Boolean window cuts, gable roofs, staircase openings, native thread handling).
- Record successful and unsuccessful tool executions and provide a reviewable feedback loop; **never interpret an LLM's claim of success as proof of valid geometry**.
- Support smaller coder models with targeted, concise retrieval and model-specific execution guidance without assuming they natively support Ollama tool calls.
- Preserve portability to other CAD/MCP integrations while keeping FreeCAD-specific examples and validators isolated.

### Knowledge section — proposed UI

Add a dedicated **Knowledge & Learning** tab with searchable/filterable views:

1. **Reference Library:** FreeCAD API notes, FreeCAD 1.1.x compatibility, Part/PartDesign recipes, MCP tool contracts, modelling standards and user-supplied documents.
2. **Proven Examples:** working parameterised Python snippets, expected output geometry, example FreeCAD files and validation evidence.
3. **Known Issues:** symptoms, root causes, failure signatures, repair strategies and reproduction cases.
4. **Lessons Learned:** validated corrective actions and manual annotations.
5. **Build History:** per-stage prompts, selected model, MCP calls/results, errors, object snapshots, checkpoints and validation outcome.
6. **Model Profiles:** model-specific context budget, tool-call support, prompt templates, recommended operation size and observed reliability.

Provide create/edit/archive, tags, source/provenance, version, FreeCAD compatibility, status (`draft`, `unverified`, `validated`, `deprecated`) and inspection of source code and supporting evidence. Make it clear when an item is guidance versus a tested working example.

### Storage and retrieval

- Use **SQLite** for entries, metadata, provenance, tags, versioning, model profiles, execution history, validation results and document/checkpoint references; use **FTS5** for lexical keyword search.
- Store longer reference text, code examples and optional `.FCStd` artifacts as local files referenced from SQLite.
- Add optional Ollama **nomic-embed-text** embeddings for semantic lookup, with a local vector index. Keyword search must remain usable without embeddings/Ollama.
- Retrieve and rank only task-/stage-relevant passages using tags, FreeCAD version, object types, similarity, validation status and model context budget. Cite knowledge entry IDs internally in the LLM context and execution log. Prefer validated over unverified examples.
- Prevent prompt injection from imported documents and stored tool output: treat them as untrusted reference data, never as higher-priority operational instructions. Require explicit approval for dangerous operations.

### Capture, validation and promotion

- Capture **request → plan/stage → selected model/profile → retrieved knowledge IDs → proposed action → MCP execution → FreeCAD document snapshot → geometry validation → error/retry/repair → checkpoint**.
- Validate actual shape presence, non-null geometry, validity, dimensions, placement, bounding boxes, visibility where available, group membership, Boolean outcome, volume changes and requested stage acceptance criteria.
- Auto-record experiences as **unverified**. Provide **Promote to Knowledge** with default **queue for review**, plus explicit manual approval and optionally a future policy for sufficiently strong automated validation.
- Retain failures as searchable diagnostics, not reusable proven recipes. Keep source and revision history and allow correction or deprecation of stale guidance.
- Offer reusable tests for known failure cases, including missing roof/doors and null Boolean cuts.

### Required orchestration prerequisites

Fix the house-demo failures before claiming knowledge improves end-to-end success:

- Replace the fixed **six-tool-round, single-transaction** assumption with configurable, bounded round limits and a resumable, stage-based build plan.
- Validate and commit **each successful stage** (and save a durable document checkpoint), rather than failing/rolling back an entire valid partial build because the complete prompt is not yet satisfied.
- States: `PENDING`, `IN_PROGRESS`, `VALIDATED`, `FAILED`, `BLOCKED`, `COMPLETE`; represent partial completion explicitly.
- On failure or Ollama HTTP 500, retry transient problems with bounded backoff; roll back **only the active failed stage**; keep previously validated geometry/checkpoints and resume on user request or an approved bounded auto-continue.
- Make stages idempotent: inspect current document state, names, types, geometry and placements before creation; do not duplicate objects on resume.
- Distinguish tool rejection/permission failure (`Execution declined`), transport errors, tool-round exhaustion and geometry verification failure.
- Require real evidence for completion, not merely folders, proposed cuts, or tool-call count. Null `Part::Cut` output must fail that stage even if cutting boxes exist.

**Regression scenario:** The two-storey house attempt created an 18-object ground-floor baseline; six tool rounds prevented completion; later partial attempts generated roof/chimney or 44 objects with first-floor walls and invalid null cuts, but validation against the whole house discarded progress. New orchestration must retain valid stages, isolate faulty cuts and support safe continuation.

### Phased implementation roadmap

1. **Prerequisite — reliability:** stage planning, stage-scoped transactions, validation, durable checkpoints, resumability, tool-round configuration, differentiated failure reporting and regression tests.
2. **Knowledge MVP:** Knowledge tab, CRUD, SQLite schema, tags/status/source, FTS5 search, manual import and curated examples/issues.
3. **Experience capture:** structured per-operation history, diagnostics, geometry validation evidence and review queue.
4. **Contextual retrieval:** retrieve minimal verified snippets at planning and execution time; log which entries were used.
5. **Approval/promotion:** approve and version validated examples, retire stale lessons, add reusable validation tests.
6. **Semantic search:** optional Ollama embeddings with `nomic-embed-text`, local vector index, hybrid ranking and token-budget controls.
7. **Model adaptation and sharing:** empirical model profiles, quality/reliability reporting, import/export portable knowledge packs and future support for other modelling applications.

### Acceptance criteria

- A user can add, search, edit, review and deprecate knowledge entries without internet access.
- FreeCAD Studio retrieves a relevant validated example for a matching task, passes a bounded excerpt to the model and records the entry ID used.
- Invalid or unverified examples are never silently presented as successful validated operations.
- A failed construction stage does not discard earlier validated stages; restarting Studio can identify the last durable checkpoint and safely resume.
- A `shape_null=true` Boolean operation cannot be marked successful; model responses containing code but no MCP execution cannot count as completed modelling.
- Automated tests cover knowledge retrieval, status promotion, model profile fallback, transient Ollama errors, six-round interruption and the house-demo partial-build regression.
