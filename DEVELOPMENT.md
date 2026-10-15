# FreeCAD AI Studio — Development

## Baseline

- V8: Tkinter UI, dark theme, history, streaming, MCP interaction and bounded geometry verification.
- Source entry point: `freecad_ollama_tk_v8.py`.
- Environment: FreeCAD MCP bridge on macOS; Ollama accessible via HTTP.

## Current priorities

1. Validate V8 against the live FreeCAD bridge and record reproducible acceptance tests.
2. Strengthen rollback: independent saved-document checkpoints and restoration after failed geometry generation, rather than relying solely on undo transactions.
3. Introduce an allowlist for generated Python operations and prevent destructive document closures without approval.
4. Verify engineering thread annotations by standard, size, pitch, depth and correct feature linkage.
5. Treat physical thread generation as experimental; verify helical geometry and final positive-volume solid before PASS.
6. Detect repeated unsuccessful modelling strategies and stop without accumulating duplicate or invalid objects.
7. Add automated tests and GitHub Actions CI, including mocked MCP inspection results.
8. Move endpoint/model preferences into user configuration rather than code constants.

## Known failure case

A 100 × 60 × 40 mm block with four corner M10×1.5 threaded holes experienced repeated failures when attempting physical helical threads. Initial plain 10 mm cylinders were incorrectly treated as threads. Later attempts created invalid negative-volume or null final solids. Preserve the last valid model and explicitly distinguish tap-drill geometry, thread metadata and physical threads.
