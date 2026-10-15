# FreeCAD AI Studio

A Tkinter desktop assistant that connects Ollama to FreeCAD via a Streamable HTTP MCP bridge.

## Current baseline: V10

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

## V10 — stable entrypoint and configuration

The maintained application is `freecad_studio.py`; the interface displays `V10.0.0`.

Copy `config.example.json` to `config.json` and edit the Ollama URL/model and FreeCAD MCP endpoint. `config.json` is ignored by Git, so `git pull` will not replace local settings.

Physical thread mode now offers `create_native_threaded_block`, a FreeCAD-native PartDesign Hole implementation supporting 1–24 holes of a single metric size and pitch in a **new** block. The tool never modifies an existing drawing. It checks the final solid and Threaded/ModelThread settings, but does not independently measure the helical pitch. Production behaviour needs testing on your MacBook.

## V9 native thread test

Run `python freecad_studio.py` and use **Test M10 Thread** with the FreeCAD MCP bridge running. This creates an isolated Part Design threaded-hole test and attempts to enable modelled thread geometry; results are not yet validated against FreeCAD 1.1.4. V8 is retained for fallback.

## Status

V10 passed Python syntax checking but the integrated multiple-hole native thread tool has not yet been tested against your live FreeCAD instance against the live FreeCAD bridge. See `DEVELOPMENT.md` for planned work.
