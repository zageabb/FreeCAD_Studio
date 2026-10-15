# FreeCAD AI Studio

A Tkinter desktop assistant that connects Ollama to FreeCAD via a Streamable HTTP MCP bridge.

## Current baseline: V8

Features include dark-theme chat UI, conversation history, document inspection, model editing via FreeCAD Python, streaming agent output, verification, bounded self-correction, and experimental thread modes.

## Requirements

- Python 3.13 or compatible Python 3 release with Tkinter available
- FreeCAD with a running MCP bridge exposing `execute_python` and `get_output_page`
- Ollama endpoint with a model supporting tool calling

## Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python freecad_ollama_tk_v8.py
```

Edit `OLLAMA_URL`, `MODEL`, and `FREECAD_MCP_URL` at the top of the script for your environment. Default development values point to the MacBook's local FreeCAD bridge and the configured LAN Ollama service.

## Important safety notes

The `execute_python` MCP tool can run arbitrary code inside FreeCAD. Keep automatic execution off when working on important documents, maintain independent backups, and review proposed changes. Undo transactions are best-effort, not guaranteed data recovery.

Engineering/cosmetic threads are not the same as physical helical threads. Physical thread generation remains experimental. Geometry verification should never claim a thread exists solely because a tapping-hole cylinder exists.

## Status

V8 passed Python syntax checking but the latest thread-handling changes have not yet been validated against the live FreeCAD bridge. See `DEVELOPMENT.md` for planned work.
