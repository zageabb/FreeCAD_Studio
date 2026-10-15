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
