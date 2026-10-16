#!/usr/bin/env python3
"""FreeCAD Ollama Agent V10: Tkinter chat, streamed output and bounded verification.

Run on the computer hosting the FreeCAD MCP bridge:
  pip install --upgrade ollama mcp
  python freecad_studio.py
"""
import asyncio
import json
import queue
import re
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk, messagebox, filedialog

from ollama import AsyncClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

VERSION = "10.0.4"
CONFIG_FILE = Path(__file__).resolve().parent / "config.json"
DEFAULT_CONFIG = {
    "ollama": {"url": "http://192.168.1.249:11434", "model": "gpt-oss:120b-cloud", "timeout_seconds": 240},
    "freecad": {"mcp_url": "http://127.0.0.1:39280/mcp"},
    "agent": {"max_tool_rounds": 12, "max_corrections": 2, "max_output_chars": 20000}
}

def load_config():
    if not CONFIG_FILE.exists():
        raise RuntimeError(f"Missing configuration file: {CONFIG_FILE}. Copy config.example.json to config.json")
    data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    cfg = {key: dict(value) for key, value in DEFAULT_CONFIG.items()}
    for key in cfg:
        override = data.get(key, {})
        if not isinstance(override, dict):
            raise ValueError(f"config.json: {key} must be an object")
        cfg[key].update(override)
    for section, key in (("ollama", "url"), ("ollama", "model"), ("freecad", "mcp_url")):
        if not isinstance(cfg[section][key], str) or not cfg[section][key].strip():
            raise ValueError(f"config.json: {section}.{key} must be a non-empty string")
    return cfg

CONFIG = load_config()
OLLAMA_URL = CONFIG["ollama"]["url"]
MODEL = CONFIG["ollama"]["model"]
FREECAD_MCP_URL = CONFIG["freecad"]["mcp_url"]
MAX_TOOL_ROUNDS = int(CONFIG["agent"]["max_tool_rounds"])
MAX_CORRECTIONS = int(CONFIG["agent"]["max_corrections"])
MAX_OUTPUT_CHARS = int(CONFIG["agent"]["max_output_chars"])
HISTORY_FILE = Path.home() / ".freecad_ollama_tk_history.json"

# Read-only *by construction*: fixed client-owned code, not model-authored.
SNAPSHOT_CODE = r'''
import FreeCAD as App
import json

def _n(v):
    try: return round(float(v), 5)
    except Exception: return str(v)

def _snap():
    d = App.ActiveDocument
    if d is None: return {"document": None, "objects": [], "note": "No active document"}
    report = {"document": d.Name, "label": d.Label, "objects": []}
    for o in d.Objects:
        item = {"name": o.Name, "label": o.Label, "type": o.TypeId,
                "properties": {}, "errors": []}
        for p in ("Length", "Width", "Height", "Radius", "Diameter", "Thickness", "Angle", "ThreadStandard", "ThreadSize", "ThreadPitch", "ThreadType", "ThreadDepth", "ThreadRepresentation", "Threaded", "ModelThread", "ThreadSize", "ThreadType", "ThreadDepth", "Depth"):
            if p in o.PropertiesList:
                try:
                    raw = getattr(o, p)
                    item["properties"][p] = bool(raw) if isinstance(raw, bool) else (_n(raw) if isinstance(raw, (int, float)) or hasattr(raw, "Value") else str(raw))
                except Exception as e: item["errors"].append(str(e))
        if hasattr(o, "Placement"):
            try:
                v = o.Placement.Base
                item["position"] = [_n(v.x), _n(v.y), _n(v.z)]
            except Exception as e: item["errors"].append(str(e))
        if hasattr(o, "Shape"):
            try:
                s = o.Shape
                item["shape_null"] = bool(s.isNull())
                if not s.isNull():
                    b = s.BoundBox
                    item["shape"] = {"valid": bool(s.isValid()),
                        "volume_mm3": _n(s.Volume), "solids": len(s.Solids),
                        "bbox_mm": [_n(b.XLength), _n(b.YLength), _n(b.ZLength)]}
            except Exception as e: item["errors"].append(str(e))
        if hasattr(o, "Constraints"):
            try:
                item["constraints"] = [{"type": c.Type, "value": _n(c.Value)} for c in o.Constraints]
            except Exception as e: item["errors"].append(str(e))
        if o.TypeId == "Part::Cut":
            try:
                base = o.Base
                cutter_group = o.Tool
                cutters = list(getattr(cutter_group, "Links", []) or [cutter_group])
                audit = {"base": base.Name, "tool": cutter_group.Name, "holes": []}
                expected_removal = 0.0
                for cutter in cutters:
                    if cutter.TypeId != "Part::Cylinder":
                        continue
                    intersection = base.Shape.common(cutter.Shape).Volume
                    remainder = o.Shape.common(cutter.Shape).Volume
                    expected_removal += intersection
                    audit["holes"].append({
                        "name": cutter.Name,
                        "intersect_base_mm3": _n(intersection),
                        "material_remaining_mm3": _n(remainder)
                    })
                if audit["holes"]:
                    audit["expected_removed_mm3"] = _n(expected_removal)
                    audit["actual_removed_mm3"] = _n(base.Shape.Volume - o.Shape.Volume)
                    item["boolean_audit"] = audit
            except Exception as e:
                item["boolean_audit_error"] = str(e)
        report["objects"].append(item)
    return report
print("FREECAD_SNAPSHOT_JSON:" + json.dumps(_snap(), default=str))
'''

# Does not close existing documents or overwrite an existing filename.
NEW_DOCUMENT_CODE = '''
import FreeCAD as App
from datetime import datetime
name = "AI_Drawing_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
doc = App.newDocument(name)
App.setActiveDocument(doc.Name)
print("NEW_DOCUMENT:" + doc.Name)
'''

TRANSACTION_CODE = {
    "begin": """import FreeCAD as App
d=App.ActiveDocument
if d is not None: d.openTransaction('FreeCAD AI Studio operation')
print('STUDIO_TRANSACTION:begin' if d else 'STUDIO_TRANSACTION:no_document')""",
    "commit": """import FreeCAD as App
d=App.getDocument(__DOC_NAME__) if __DOC_NAME__ else None
if d is not None: d.commitTransaction()
print('STUDIO_TRANSACTION:commit' if d else 'STUDIO_TRANSACTION:missing_document')""",
    "rollback": """import FreeCAD as App
d=App.getDocument(__DOC_NAME__) if __DOC_NAME__ else None
if d is not None: d.abortTransaction(); d.recompute()
print('STUDIO_TRANSACTION:rollback' if d else 'STUDIO_TRANSACTION:missing_document')""",
}
# Fixed, isolated native FreeCAD Part Design thread test. The LLM does not generate
# this geometry. Run this before attempting production physical-thread modelling.
NATIVE_M10_TEST = r'''
import FreeCAD as App, Part, Sketcher, json
from datetime import datetime
report = {"engine": "PartDesign::Hole", "thread": "M10x1.5", "status": "fail", "checks": {}, "errors": []}
old = App.ActiveDocument.Name if App.ActiveDocument else None
name = "AI_ThreadTest_" + datetime.now().strftime("%H%M%S_%f")
doc = None
try:
    doc = App.newDocument(name)
    body = doc.addObject("PartDesign::Body", "TestBody")
    base = doc.addObject("PartDesign::Feature", "TestBlock")
    body.addObject(base)
    base.Shape = Part.makeBox(50, 50, 20)
    doc.recompute()
    sketch = doc.addObject("Sketcher::SketchObject", "HolePosition")
    body.addObject(sketch)
    sketch.Placement.Base = App.Vector(0, 0, 20)
    sketch.addGeometry(Part.Circle(App.Vector(25,25,0), App.Vector(0,0,1), 4.25), False)
    doc.recompute()
    hole = body.newObject("PartDesign::Hole", "NativeM10Hole")
    hole.Profile = sketch
    hole.Threaded = True
    def enums(prop):
        try: return list(hole.getEnumerationsOfProperty(prop))
        except Exception: return []
    report["available_enums"] = {p: enums(p) for p in ("ThreadType", "ThreadSize", "DepthType")}
    thread_types = enums("ThreadType")
    iso = next((x for x in thread_types if "ISO" in x.upper() and "METRIC" in x.upper()), None)
    if iso is None: raise RuntimeError("No ISO metric thread option in this FreeCAD Hole feature")
    hole.ThreadType = iso
    sizes = enums("ThreadSize")
    size = next((x for x in sizes if x.strip().upper().replace(" ", "") == "M10X1.5"), None)
    if size is None: raise RuntimeError("M10x1.5 not offered by native thread feature: " + str(sizes))
    hole.ThreadSize = size
    if "ThreadDepth" in hole.PropertiesList: hole.ThreadDepth = 20
    if "Depth" in hole.PropertiesList: hole.Depth = 20
    if "ModelThread" not in hole.PropertiesList:
        raise RuntimeError("FreeCAD PartDesign::Hole does not expose ModelThread")
    hole.ModelThread = True
    doc.recompute()
    shape = hole.Shape
    checks = {"threaded": bool(hole.Threaded),
              "model_thread": bool(hole.ModelThread),
              "thread_type": str(hole.ThreadType),
              "thread_size": str(hole.ThreadSize),
              "shape_valid": bool(not shape.isNull() and shape.isValid()),
              "solid_count": len(shape.Solids),
              "volume_mm3": float(shape.Volume) if not shape.isNull() else 0,
              "base_volume_mm3": 50000.0}
    report["checks"] = checks
    if not all([checks["threaded"], checks["model_thread"],
                checks["shape_valid"], checks["solid_count"] == 1,
                0 < checks["volume_mm3"] < checks["base_volume_mm3"]]):
        raise RuntimeError("Native modelled-thread feature failed geometric checks")
    if hasattr(base, "ViewObject") and base.ViewObject: base.ViewObject.Visibility = False
    if hasattr(sketch, "ViewObject") and sketch.ViewObject: sketch.ViewObject.Visibility = False
    if hasattr(hole, "ViewObject") and hole.ViewObject: hole.ViewObject.Visibility = True
    report["status"] = "pass"
    report["document"] = doc.Name
    report["note"] = "Native ModelThread enabled and final shape valid; helical pitch topology is not independently measured."
except Exception as exc:
    report["errors"].append(type(exc).__name__ + ": " + str(exc))
    if doc is not None:
        try: App.closeDocument(doc.Name)
        except Exception: pass
finally:
    if old and App.getDocument(old): App.setActiveDocument(old)
    print("NATIVE_THREAD_TEST_JSON:" + json.dumps(report, default=str))
'''

# Native PartDesign threaded-hole code is fixed, not generated by the LLM.
# This initial production engine works on an isolated new block and supports
# one or more circles in the same sketch, with one common metric thread size.
NATIVE_THREAD_JOB = r'''
import FreeCAD as App, Part, Sketcher, json
from datetime import datetime
job = json.loads(__JOB_JSON__)
report = {"status":"fail", "thread":job["thread"], "holes":job["positions"], "checks":{}, "errors":[]}
old = App.ActiveDocument.Name if App.ActiveDocument else None
doc = None
try:
    dims = job["dimensions"]
    length, width, height = map(float, (dims["length"], dims["width"], dims["height"]))
    points = job["positions"]
    if not (0 < length <= 10000 and 0 < width <= 10000 and 0 < height <= 10000):
        raise ValueError("Block dimensions out of range")
    if not (1 <= len(points) <= 24): raise ValueError("1-24 holes supported")
    for pt in points:
        if not (0 < float(pt[0]) < length and 0 < float(pt[1]) < width):
            raise ValueError("Hole centre outside block")
    doc = App.newDocument("AI_Threaded_" + datetime.now().strftime("%H%M%S_%f"))
    body = doc.addObject("PartDesign::Body", "ThreadedBody")
    base = doc.addObject("PartDesign::Feature", "BaseBlock")
    body.addObject(base)
    base.Shape = Part.makeBox(length, width, height)
    doc.recompute()
    sketch = doc.addObject("Sketcher::SketchObject", "HoleCentres")
    body.addObject(sketch)
    sketch.Placement.Base = App.Vector(0,0,height)
    for x,y in points:
        sketch.addGeometry(Part.Circle(App.Vector(float(x),float(y),0), App.Vector(0,0,1), 2),False)
    doc.recompute()
    hole = body.newObject("PartDesign::Hole", "NativeThreadedHoles")
    hole.Profile = sketch
    hole.Threaded = True
    enums = lambda p: list(hole.getEnumerationsOfProperty(p))
    iso = next((v for v in enums("ThreadType") if "ISO" in v.upper() and "METRIC" in v.upper()), None)
    if iso is None: raise RuntimeError("ISO metric thread type unavailable")
    hole.ThreadType = iso
    target = job["thread"].upper().replace(" ", "").replace("×", "X")
    matched = next((v for v in enums("ThreadSize") if v.upper().replace(" ", "").replace("×", "X") == target),None)
    if matched is None: raise RuntimeError("Thread size not supported: " + target)
    hole.ThreadSize = matched
    if "DepthType" in hole.PropertiesList:
        depth_enums = enums("DepthType")
        through = next((v for v in depth_enums if "through" in v.lower()), None)
        if through: hole.DepthType = through
    if "Depth" in hole.PropertiesList: hole.Depth = height
    if "ThreadDepth" in hole.PropertiesList: hole.ThreadDepth = height
    if "ModelThread" not in hole.PropertiesList: raise RuntimeError("ModelThread unavailable")
    hole.ModelThread = True
    doc.recompute()
    shape = hole.Shape
    checks = {"threaded":bool(hole.Threaded), "model_thread":bool(hole.ModelThread),
        "size":str(hole.ThreadSize), "valid":bool(not shape.isNull() and shape.isValid()),
        "solids":len(shape.Solids), "volume":float(shape.Volume) if not shape.isNull() else 0,
        "base_volume":length*width*height, "hole_profile_count":sketch.GeometryCount}
    report["checks"] = checks
    if not (checks["threaded"] and checks["model_thread"] and checks["size"].upper()==target and
        checks["valid"] and checks["solids"] == 1 and
        0 < checks["volume"] < checks["base_volume"] and checks["hole_profile_count"] == len(points)):
        raise RuntimeError("Native hole verification failed")
    base.ViewObject.Visibility = False
    sketch.ViewObject.Visibility = False
    hole.ViewObject.Visibility = True
    doc.recompute()
    App.setActiveDocument(doc.Name)
    try:
        import FreeCADGui as Gui
        Gui.activeDocument().activeView().viewAxonometric()
        Gui.activeDocument().activeView().fitAll()
    except Exception: pass
    report["document"] = doc.Name
    report["status"] = "pass"
    report["note"] = "Native modelled threads verified by PartDesign settings and shape validity. Pitch topology is not independently measured."
except Exception as e:
    report["errors"].append(type(e).__name__ + ": " + str(e))
    if doc is not None:
        try: App.closeDocument(doc.Name)
        except Exception: pass
    if old and App.getDocument(old): App.setActiveDocument(old)
finally:
    print("NATIVE_THREAD_JOB_JSON:" + json.dumps(report,default=str))
'''

NATIVE_THREAD_TOOL = {
    "type":"function", "function":{
        "name":"create_native_threaded_block",
        "description":"Create a NEW rectangular block with one or more physically modelled internal ISO metric threaded holes, using FreeCAD native PartDesign::Hole. Does not edit an existing block. For existing geometry, ask the user to make a copy or use a normal non-thread editing operation.",
        "parameters": {"type":"object", "properties":{
            "dimensions":{"type":"object","properties":{"length":{"type":"number"},"width":{"type":"number"},"height":{"type":"number"}},"required":["length","width","height"]},
            "thread":{"type":"string","description":"Metric size including pitch, e.g. M10x1.5"},
            "positions":{"type":"array","items":{"type":"array","items":{"type":"number"},"minItems":2,"maxItems":2},"minItems":1,"maxItems":24}
        },"required":["dimensions","thread","positions"]}
    }
}


def build_native_code(args):
    dims=args["dimensions"]
    values=[float(dims[k]) for k in ("length","width","height")]
    if not all(0 < x <= 10000 for x in values): raise ValueError("Invalid block dimensions")
    positions=args["positions"]
    if not isinstance(positions,list) or not 1 <= len(positions) <= 24: raise ValueError("Invalid hole count")
    for pt in positions:
        if not isinstance(pt,list) or len(pt)!=2 or not all(isinstance(x,(int,float)) for x in pt):
            raise ValueError("Invalid hole position")
    thread=args["thread"].strip().upper().replace("×","X")
    if not re.fullmatch(r"M\d+(?:\.\d+)?X\d+(?:\.\d+)?",thread):
        raise ValueError("Thread must include pitch, e.g. M10x1.5")
    payload={"dimensions":dict(zip(("length","width","height"),values)),"thread":thread,"positions":positions}
    return NATIVE_THREAD_JOB.replace("__JOB_JSON__",repr(json.dumps(payload)))


def parse_marked_json(output, marker):
    return snapshot_from_output(output.replace(marker, "FREECAD_SNAPSHOT_JSON:"))

THREAD_METADATA_EXAMPLE = """
For each existing tapping-hole cutter object, set editable properties:
  obj.addProperty('App::PropertyString', 'ThreadStandard', 'Thread') -> 'ISO Metric'
  obj.addProperty('App::PropertyString', 'ThreadSize', 'Thread') -> 'M10'
  obj.addProperty('App::PropertyFloat', 'ThreadPitch', 'Thread') -> 1.5
  obj.addProperty('App::PropertyString', 'ThreadType', 'Thread') -> 'Internal'
  obj.addProperty('App::PropertyLength', 'ThreadDepth', 'Thread') -> requested depth
  obj.addProperty('App::PropertyString', 'ThreadRepresentation', 'Thread') -> 'Engineering'
Use the actual requested size/pitch, not always M10. Check existence before addProperty.
The nominal thread size is not the tap-drill diameter: M10x1.5 uses ~8.5 mm pilot.
These properties are *engineering annotations*, never claim they create physical threads.
"""

SYSTEM = """You are a FreeCAD modelling assistant using FreeCAD's Python API.
The client has inspected the active document, and will independently inspect it again after edits.
Read the provided snapshot before changes. Reuse useful geometry; avoid duplicates.
Prefer editable parametric parts. Units are mm unless user says otherwise.
Do not close documents, delete objects/documents, overwrite files, run external commands,
or access the filesystem or network. Never claim verification without observed measurements.
Use execute_python for CAD operations; stop tool calls when your operation is complete.
Avoid creating new documents when an active document already exists unless explicitly requested.
Never create invalid, null, zero-solid or negative-volume final geometry and claim success.
"""


def mcp_text(result):
    parts = []
    for item in result.content or []:
        parts.append(item.text if getattr(item, "text", None) is not None
                     else item.model_dump_json())
    if getattr(result, "structured_content", None):
        parts.append(json.dumps(result.structured_content, default=str))
    return ("MCP_ERROR: " if getattr(result, "is_error", False) else "") + "\n".join(parts)


def snapshot_from_output(text):
    marker = "FREECAD_SNAPSHOT_JSON:"

    def parse(s):
        at = s.find(marker)
        if at >= 0:
            try:
                val, _ = json.JSONDecoder().raw_decode(s[at + len(marker):].lstrip())
                if isinstance(val, dict): return val
            except json.JSONDecodeError:
                pass
        return None

    def walk(value):
        if isinstance(value, str):
            direct = parse(value)
            if direct is not None: return direct
            if value.lstrip().startswith(("{", "[")):
                try: return walk(json.loads(value))
                except json.JSONDecodeError: pass
        elif isinstance(value, list):
            for v in value:
                found = walk(v)
                if found is not None: return found
        elif isinstance(value, dict):
            for k in ("page", "text", "content", "structured_content", "result"):
                if k in value:
                    found = walk(value[k])
                    if found is not None: return found
        return None

    found = walk(text)
    if found is None:
        raise ValueError("Unable to find document snapshot in FreeCAD response: " + text[:400])
    return found


class AgentBackend:
    """All networking and Ollama interactions occur in a background thread."""
    def __init__(self, ui_queue, commands):
        self.out = ui_queue
        self.commands = commands
        self.auto = False
        self.verify = True
        self.correct = True
        self.debug = False
        self.thread_mode = "engineering"
        self.pending_approvals = {}
        self.client = AsyncClient(host=OLLAMA_URL, timeout=float(CONFIG["ollama"]["timeout_seconds"]))

    def emit(self, kind, **fields):
        self.out.put((kind, fields))

    async def call(self, mcp, name, args):
        result = await mcp.call_tool(name, arguments=args)
        output = mcp_text(result)
        if self.debug or getattr(result, "is_error", False):
            self.emit("log", text=("ERROR " if result.is_error else "MCP ") + output[:3500])
        return output, bool(getattr(result, "is_error", False))

    async def inspect(self, mcp):
        output, failed = await self.call(mcp, "execute_python", {"code": SNAPSHOT_CODE})
        if failed: raise RuntimeError("Document inspection failed: " + output[:500])
        snap = snapshot_from_output(output)
        self.emit("log", text=f"Document: {snap.get('document') or '(none)'} · {len(snap['objects'])} objects")
        self.emit("snapshot", snapshot=snap)
        return snap

    async def approval(self, code):
        if self.auto: return True
        response = queue.Queue(maxsize=1)
        self.emit("approval", code=code, response=response)
        return await asyncio.to_thread(response.get)

    async def action_phase(self, mcp, tools, messages, stage, limit=MAX_TOOL_ROUNDS):
        allowed = {t["function"]["name"] for t in tools}
        changes = errors = 0
        self.emit("status", text=stage)
        for round_num in range(limit):
            # Ollama streams text tokens and completed tool calls. Collect tool calls
            # as returned by each chunk, then send one complete assistant message.
            content_parts = []
            thinking_parts = []
            collected_calls = []
            try:
                stream = await self.client.chat(model=MODEL, messages=messages,
                                                 tools=tools, stream=True,
                                                 options={"temperature": 0})
                async for chunk in stream:
                    msg = chunk.message
                    if getattr(msg, "content", None):
                        content_parts.append(msg.content)
                        self.emit("token", text=msg.content)
                    if getattr(msg, "thinking", None):
                        thinking_parts.append(msg.thinking)
                        if self.debug: self.emit("log", text=msg.thinking)
                    for tool_call in (getattr(msg, "tool_calls", None) or []):
                        collected_calls.append(tool_call.model_dump(exclude_none=True))
            except Exception as exc:
                # A failed stream may contain partial tokens/calls. Discard ALL
                # partial results and retry once without streaming; no MCP
                # action has been executed for this round yet.
                if type(exc).__name__ != "ResponseError":
                    raise
                self.emit("log", text="Ollama response failed; retrying this round once without streaming.")
                try:
                    response = await self.client.chat(
                        model=MODEL, messages=messages, tools=tools,
                        stream=False, options={"temperature": 0})
                except Exception as retry_error:
                    raise RuntimeError(
                        "Ollama failed both streamed and non-streamed requests; "
                        "operation will be rolled back. " + str(retry_error)
                    ) from retry_error
                content_parts = [response.message.content or ""]
                thinking_parts = [response.message.thinking] if getattr(response.message, "thinking", None) else []
                collected_calls = [c.model_dump(exclude_none=True)
                                   for c in (response.message.tool_calls or [])]
                self.emit("log", text="Ollama fallback response received.")

            assistant_msg = {"role": "assistant", "content": "".join(content_parts)}
            if collected_calls: assistant_msg["tool_calls"] = collected_calls
            if thinking_parts: assistant_msg["thinking"] = "".join(thinking_parts)
            messages.append(assistant_msg)
            if not collected_calls:
                return {"complete": True, "changes": changes, "errors": errors}

            for call in collected_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                args = fn.get("arguments", {}) or {}
                if isinstance(args, str):
                    try: args = json.loads(args)
                    except json.JSONDecodeError: args = {}
                self.emit("log", text="Action proposed: " + name)
                if self.debug: self.emit("log", text=json.dumps(args, indent=2, default=str))
                if name not in allowed:
                    output, failed = "Tool not allowed", True
                elif name == "create_native_threaded_block":
                    try:
                        code = build_native_code(args)
                        if not await self.approval(code):
                            output, failed = "Native thread operation declined", True
                        else:
                            raw, failed = await self.call(mcp, "execute_python", {"code": code})
                            try:
                                report = parse_marked_json(raw, "NATIVE_THREAD_JOB_JSON:")
                                failed = failed or report.get("status") != "pass"
                                output = json.dumps(report)
                                if not failed:
                                    changes += 1
                                    self.emit("log", text="Native modelled-thread block created: " + report.get("document", ""))
                            except Exception as err:
                                output, failed = "Cannot verify native thread result: " + str(err), True
                    except Exception as err:
                        output, failed = "Native thread request rejected: " + str(err), True
                elif name == "execute_python" and not await self.approval(args.get("code", "")):
                    output, failed = "User declined execution", True
                    self.emit("log", text="Execution declined")
                else:
                    output, failed = await self.call(mcp, name, args)
                    if name == "execute_python" and not failed: changes += 1
                errors += int(failed)
                messages.append({"role": "tool", "tool_name": name,
                                 "content": output[:MAX_OUTPUT_CHARS]})
        self.emit("log", text=f"Tool-round limit reached ({limit} rounds); inspecting geometry before deciding whether to commit.")
        return {"complete": False, "changes": changes, "errors": errors, "limit_reached": True}

    async def test_native_thread(self, mcp):
        self.emit("status", text="Testing FreeCAD native M10 thread")
        self.emit("log", text="Isolated M10x1.5 physical thread test; existing drawings untouched.")
        if not await self.approval(NATIVE_M10_TEST):
            self.emit("assistant", text="Native thread test cancelled.")
            return
        output, failed = await self.call(mcp, "execute_python", {"code": NATIVE_M10_TEST})
        def find_report(value):
            marker = "NATIVE_THREAD_TEST_JSON:"
            if isinstance(value, dict):
                for v in value.values():
                    found = find_report(v)
                    if found is not None: return found
            elif isinstance(value, list):
                for v in value:
                    found = find_report(v)
                    if found is not None: return found
            elif isinstance(value, str):
                if marker in value:
                    tail = value.split(marker, 1)[1].lstrip()
                    try: return json.JSONDecoder().raw_decode(tail)[0]
                    except ValueError: pass
                try: return find_report(json.loads(value))
                except (ValueError, TypeError): pass
            return None
        report = find_report(output)
        if failed or not isinstance(report, dict):
            self.emit("assistant", text="Native test failed or returned no parseable result. Enable Debug to view the MCP response.")
            return
        self.emit("log", text="Native M10 test: " + report.get("status", "unknown").upper())
        if report.get("status") == "pass":
            self.emit("assistant", text="Native M10 modelled-thread test PASSED in separate drawing " +
                      report.get("document", "") + ".\n" + report.get("note", ""))
        else:
            self.emit("assistant", text="Native M10 test FAILED safely: " +
                      "; ".join(map(str, report.get("errors", []))) +
                      ". Your original document was left unchanged.")
        if self.debug: self.emit("log", text=json.dumps(report, indent=2))

    async def transaction(self, mcp, action, document_name=None):
        code = TRANSACTION_CODE[action]
        if action != "begin":
            if not document_name:
                raise RuntimeError("Cannot finish transaction without originating document")
            code = code.replace("__DOC_NAME__", repr(document_name))
        output, failed = await self.call(mcp, "execute_python", {"code": code})
        if failed or "STUDIO_TRANSACTION:" not in output or "STUDIO_TRANSACTION:missing_document" in output:
            raise RuntimeError(f"Transaction {action} failed: {output[:500]}")
        return "STUDIO_TRANSACTION:no_document" not in output

    def hard_checks(self, instruction, after):
        """Checks which must not be overridden by an LLM's PASS."""
        problems = []
        objs = after.get("objects", [])
        visible = [o for o in objs if o.get("name", "").lower() in ("finalpart", "boxwithallholes")]
        for o in visible:
            shape = o.get("shape", {})
            if o.get("shape_null") or not shape.get("valid", False) or shape.get("solids", 0) < 1 or shape.get("volume_mm3", 0) <= 0:
                problems.append(f"{o['name']} is not a valid positive-volume solid")
        for o in objs:
            audit = o.get("boolean_audit")
            if not audit:
                continue
            for hole in audit.get("holes", []):
                if hole["intersect_base_mm3"] < 1.0:
                    problems.append(f"{o['name']}: {hole['name']} does not cut the base solid")
                elif hole["material_remaining_mm3"] > 0.1:
                    problems.append(f"{o['name']}: {hole['name']} is not fully removed from the result")
            if abs(audit.get("expected_removed_mm3", 0) - audit.get("actual_removed_mm3", 0)) > 0.2:
                problems.append(f"{o['name']}: expected cut volume {audit['expected_removed_mm3']} mm³ but actual removal is {audit['actual_removed_mm3']} mm³")
        if re.search(r"\bthread(?:ed|s|ing)?\b|\bM\d+(?:\s*[x×]\s*\d+(?:\.\d+)?)?\b", instruction, re.I):
            if self.thread_mode == "engineering":
                annotated = [o for o in objs if str(o.get('properties',{}).get('ThreadRepresentation','')).lower() == 'engineering'
                             and o.get('properties',{}).get('ThreadSize') and o.get('properties',{}).get('ThreadPitch')]
                requested = re.search(r"(?:four|4)\s+(?:corner\s+)?(?:threaded\s+)?holes", instruction, re.I)
                required = 4 if requested else 1
                if len(annotated) < required:
                    problems.append(f"Only {len(annotated)}/{required} engineering-thread annotations found")
            else:
                problems.append('Physical-thread topology has no deterministic pitch/profile verification; cannot report fully verified')
        return problems

    async def review(self, instruction, before, after, outcome):
        prompt = {"request": instruction, "before": before, "after": after,
                  "execution": outcome, "rules": [
                      "Use only evidence in the snapshots; distinguish observed facts and assumptions",
                      "pass if all material requirements are evidenced and operation completed",
                      "fail if proven discrepancies can be corrected",
                      "unknown if required measurements are absent",
                      "Return status, summary, verified, issues, unverified, corrections as JSON"]}
        self.emit("status", text="Reviewing geometry")
        async def _request_verdict(streaming):
            response = await self.client.chat(model=MODEL, format="json", stream=streaming,
                messages=[{"role": "system", "content": "Conservative CAD verifier; produce exactly one complete JSON object."},
                          {"role": "user", "content": json.dumps(prompt, default=str)}],
                options={"temperature": 0})
            if streaming:
                chunks = []
                async for chunk in response:
                    token = chunk.message.content or ""
                    chunks.append(token)
                    if self.debug: self.emit("token", text=token)
                raw = "".join(chunks)
            else:
                raw = response.message.content or ""
            try:
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("Review was not an object")
                return value
            except (ValueError, TypeError):
                if self.debug: self.emit("log", text="Malformed verification JSON: " + raw[:1000])
                return None

        verdict = await _request_verdict(True)
        if verdict is None:
            self.emit("log", text="Verification response malformed; retrying without streaming.")
            verdict = await _request_verdict(False)
        if verdict is None:
            verdict = {"status": "unknown", "summary": "Review response was not valid JSON after retry",
                       "issues": [], "verified": [], "unverified": ["LLM review unavailable"], "corrections": []}
        if verdict.get("status") not in ("pass", "fail", "unknown"):
            verdict["status"] = "unknown"
        issues = self.hard_checks(instruction, after)
        if issues:
            verdict["status"] = "unknown" if self.thread_mode == "physical" and all('Physical-thread' in x for x in issues) else "fail"
            verdict.setdefault("issues", [])
            verdict["issues"] = list(verdict["issues"]) + issues
            verdict["summary"] = "Independent verification did not establish a correct final result. " + str(verdict.get("summary", ""))
        if outcome.get("errors", 0):
            verdict["status"] = "fail"
            verdict.setdefault("issues", []).append(f"{outcome['errors']} MCP/tool error(s) occurred")
        elif not outcome.get("complete"):
            if verdict["status"] == "pass":
                verdict["status"] = "unknown"
            verdict.setdefault("unverified", []).append("Tool-round limit reached before agent completion; do not auto-commit")
        self.emit("review", verdict=verdict)
        return verdict

    async def perform(self, mcp, tools, instruction):
        self.emit("status", text="Inspecting existing drawing")
        original = await self.inspect(mcp)
        threaded = bool(re.search(r"\bthread(?:ed|s|ing)?\b|\bM\d+\b", instruction, re.I))
        mode = self.thread_mode if threaded else "none"
        extra = ""
        if threaded and mode == "engineering":
            extra = ("THREAD MODE: ENGINEERING. Create proper tapping holes and add explicit thread metadata "
                     "to each hole; DO NOT generate helices or physical-thread cut solids. "
                     "This is an annotated thread, not a printable physical thread.\n" + THREAD_METADATA_EXAMPLE)
        elif threaded:
            extra = ("THREAD MODE: PHYSICAL EXPERIMENTAL. Do not confuse holes or metadata with real helical "
                     "threads. Use a single minimal approach and stop if it fails; no repeated inventing of shapes.")
        if mode == "physical":
            self.emit("log", text="Native physical thread engine: new blocks only; existing drawing preserved.")
            messages = [{"role":"system", "content": SYSTEM + "\nPhysical-thread requests MUST use create_native_threaded_block only. It creates a NEW block, never edits existing CAD. Call it once, then report results; do not use execute_python. Ask for missing size, pitch, dimensions or coordinates rather than guessing."},
                {"role":"user", "content":"REQUEST:\n"+instruction+"\nCURRENT MODEL:\n"+json.dumps(original)}]
            outcome = await self.action_phase(mcp, [NATIVE_THREAD_TOOL], messages, "Native thread modelling", limit=3)
            if outcome["complete"] and outcome["changes"] > 0 and outcome["errors"] == 0:
                self.emit("assistant", text="Native thread operation completed. FreeCAD created a separate modelled-thread document; inspect the visible geometry in FreeCAD. Thread pitch topology has not been independently measured.")
            else:
                self.emit("assistant", text="Native thread creation not completed or not verified. No original drawing was changed by the dedicated thread tool.")
            await self.inspect(mcp)
            return
        self.emit("log", text=f"Thread mode: {mode}. Starting safeguarded operation.")
        guarded = False
        originating_document = original.get("document")
        try:
            guarded = await self.transaction(mcp, "begin")
            if not guarded:
                self.emit("log", text="WARNING: No active document; automatic rollback is unavailable.")
            messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": "REQUEST:\n" + instruction +
                    "\nEXISTING MODEL SNAPSHOT:\n" + json.dumps(original, default=str) +
                    "\n" + extra + "\nEdit existing objects, avoid duplicates."}]
            limit = 3 if mode == "physical" else MAX_TOOL_ROUNDS
            outcome = await self.action_phase(mcp, tools, messages, "Modelling", limit=limit)
            if not self.verify:
                if guarded: await self.transaction(mcp, "commit", originating_document)
                self.emit("assistant", text="Modelling completed, but verification was disabled. No geometric success claim can be made.")
                return
            verdict = None
            for attempt in range((0 if mode == "physical" else MAX_CORRECTIONS) + 1):
                self.emit("status", text="Inspecting result")
                after = await self.inspect(mcp)
                verdict = await self.review(instruction, original, after, outcome)
                if verdict["status"] == "pass":
                    if guarded: await self.transaction(mcp, "commit", originating_document)
                    self.emit("log", text="Verified operation committed.")
                    return
                if (not self.correct or verdict["status"] == "unknown" or
                    not verdict.get("corrections") or attempt >= MAX_CORRECTIONS or
                    mode == "physical" or not outcome.get("complete")):
                    break
                self.emit("log", text=f"Correction {attempt+1}/{MAX_CORRECTIONS}")
                messages.append({"role": "user", "content": "Fix only these discrepancies, no duplicate features:\n" +
                    json.dumps(verdict) + "\nCURRENT MODEL:\n" + json.dumps(after) + "\n" + extra})
                outcome = await self.action_phase(mcp, tools, messages, "Correcting", limit=3)
            if guarded:
                await self.transaction(mcp, "rollback", originating_document)
                # A newly created document isn't covered by the transaction on
                # the original document. Keep it visible for review rather than
                # implying that its geometry has been rolled back.
                if after.get("document") != originating_document:
                    self.emit("assistant", text="Verification was inconclusive. Original drawing transaction reverted; NEW drawing " + str(after.get("document")) + " has been retained for manual inspection (not verified).")
                else:
                    self.emit("assistant", text="Verification did not pass. Original drawing changes were rolled back.")
                await self.inspect(mcp)
            else:
                self.emit("assistant", text="Verification did not pass. No automatic rollback was available; inspect or restore the document before continuing.")
        except Exception:
            if guarded:
                try:
                    await self.transaction(mcp, "rollback", originating_document)
                    await self.inspect(mcp)
                    self.emit("log", text="Operation rolled back following an error.")
                except Exception as rollback_error:
                    self.emit("log", text=f"WARNING: Rollback failed: {rollback_error}")
            raise

    async def loop(self):
        async with streamable_http_client(FREECAD_MCP_URL) as (read, write):
            async with ClientSession(read, write) as mcp:
                await mcp.initialize()
                discovered = (await mcp.list_tools()).tools
                usable = [t for t in discovered if t.name in ("execute_python", "get_output_page")]
                if "execute_python" not in {t.name for t in usable}:
                    raise RuntimeError("Bridge has no execute_python tool")
                tools = [{"type": "function", "function": {"name": t.name,
                    "description": t.description or "", "parameters": t.input_schema}}
                    for t in usable]
                self.emit("connection", connected=True, text="Connected to FreeCAD MCP")
                while True:
                    command = await asyncio.to_thread(self.commands.get)
                    kind = command["kind"]
                    if kind == "quit": break
                    if kind == "settings":
                        for key in ("auto", "verify", "correct", "debug"):
                            if key in command: setattr(self, key, bool(command[key]))
                        if command.get("thread_mode") in ("engineering", "physical"):
                            self.thread_mode = command["thread_mode"]
                        continue
                    self.emit("busy", busy=True)
                    try:
                        if kind == "new":
                            if await self.approval(NEW_DOCUMENT_CODE):
                                out, failed = await self.call(mcp, "execute_python", {"code": NEW_DOCUMENT_CODE})
                                if failed: raise RuntimeError(out[:500])
                                await self.inspect(mcp)
                                self.emit("assistant", text="New drawing created. Existing drawings were left open.")
                            else:
                                self.emit("log", text="New drawing cancelled")
                        elif kind == "thread_test":
                            await self.test_native_thread(mcp)
                        elif kind == "inspect":
                            await self.inspect(mcp)
                            self.emit("assistant", text="Drawing inspection complete. See the Model tab.")
                        elif kind == "chat":
                            await self.perform(mcp, tools, command["text"])
                    except Exception as exc:
                        self.emit("log", text=f"ERROR: {type(exc).__name__}: {exc}")
                        self.emit("assistant", text="Operation stopped with an error. The drawing may have changed; inspect it before retrying.")
                    finally:
                        self.emit("status", text="Ready")
                        self.emit("busy", busy=False)
        self.emit("connection", connected=False, text="Disconnected")

    def run(self):
        try: asyncio.run(self.loop())
        except Exception as exc:
            self.emit("connection", connected=False, text=f"Connection failed: {exc}")
            self.emit("busy", busy=False)


class DesktopApp:
    def __init__(self, root):
        self.root = root
        root.title(f"FreeCAD AI Studio V{VERSION} · Ollama MCP")
        root.geometry("1150x750")
        root.minsize(860, 560)
        self.events, self.commands = queue.Queue(), queue.Queue()
        self.backend = AgentBackend(self.events, self.commands)
        self.busy = False
        self.connected = False
        self.history = []
        self.auto_var = tk.BooleanVar(value=False)
        self.verify_var = tk.BooleanVar(value=True)
        self.correct_var = tk.BooleanVar(value=True)
        self.debug_var = tk.BooleanVar(value=False)
        self.thread_mode_var = tk.StringVar(value="engineering")
        self.status_var = tk.StringVar(value="Connecting to FreeCAD...")
        self.document_var = tk.StringVar(value="Document: —")
        self._layout()
        self._load_history()
        self._settings()
        threading.Thread(target=self.backend.run, daemon=True).start()
        root.after(80, self._poll)
        root.protocol("WM_DELETE_WINDOW", self.close)

    def _apply_dark_theme(self):
        """Apply an explicit, high-contrast palette on macOS and Linux."""
        self.palette = {
            "background": "#161C27", "surface": "#222B3A",
            "input": "#101722", "foreground": "#F1F5F9",
            "muted": "#C4CFDB", "accent": "#80BFFF",
            "assistant": "#B5F0D1", "selection": "#335B87",
            "border": "#42536A", "button": "#34445A",
        }
        p = self.palette
        self.root.configure(background=p["background"])
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(".", background=p["background"], foreground=p["foreground"],
                        fieldbackground=p["input"], bordercolor=p["border"],
                        font=("Helvetica", 11))
        style.configure("TFrame", background=p["background"])
        style.configure("TLabel", background=p["background"], foreground=p["foreground"])
        style.configure("TButton", background=p["button"], foreground=p["foreground"],
                        bordercolor=p["border"], padding=(10, 7))
        style.map("TButton", background=[("active", "#47617F"), ("pressed", "#36516E")],
                  foreground=[("disabled", p["muted"]), ("active", "#FFFFFF")])
        style.configure("TCheckbutton", background=p["background"], foreground=p["foreground"],
                        font=("Helvetica", 11))
        style.map("TCheckbutton", background=[("active", p["background"])],
                  foreground=[("active", "#FFFFFF")])
        style.configure("TNotebook", background=p["background"], bordercolor=p["border"])
        style.configure("TNotebook.Tab", background=p["button"], foreground=p["foreground"],
                        padding=(14, 7))
        style.map("TNotebook.Tab", background=[("selected", "#395A7A"),
                                                    ("active", "#47617F")],
                  foreground=[("selected", "#FFFFFF"), ("active", "#FFFFFF")])
        style.configure("TPanedwindow", background=p["border"])

    def _theme_text(self, widget):
        p = self.palette
        widget.configure(background=p["input"], foreground=p["foreground"],
                         insertbackground=p["foreground"], selectbackground=p["selection"],
                         selectforeground="#FFFFFF", relief="flat", borderwidth=1,
                         highlightthickness=1, highlightbackground=p["border"],
                         highlightcolor=p["accent"])

    def _layout(self):
        self._apply_dark_theme()
        wrapper = ttk.Frame(self.root, padding=10)
        wrapper.pack(fill="both", expand=True)
        header = ttk.Frame(wrapper)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text=f"FreeCAD AI Studio  ·  V{VERSION}", font=("Helvetica", 17, "bold")).pack(side="left")
        ttk.Label(header, textvariable=self.status_var).pack(side="right")
        bar = ttk.Frame(wrapper)
        bar.pack(fill="x", pady=(0, 8))
        self.new_btn = ttk.Button(bar, text="＋ New Drawing", command=lambda: self._send_action("new"))
        self.new_btn.pack(side="left", padx=(0, 6))
        self.inspect_btn = ttk.Button(bar, text="Inspect Drawing", command=lambda: self._send_action("inspect"))
        self.inspect_btn.pack(side="left", padx=(0, 10))
        self.thread_test_btn = ttk.Button(bar, text="Test M10 Thread", command=lambda: self._send_action("thread_test"))
        self.thread_test_btn.pack(side="left", padx=(0, 10))
        for name, var in (("Auto execute", self.auto_var), ("Verify", self.verify_var),
                          ("Self-correct", self.correct_var), ("Debug", self.debug_var)):
            ttk.Checkbutton(bar, text=name, variable=var, command=self._settings).pack(side="left", padx=6)
        mode_bar = ttk.Frame(wrapper)
        mode_bar.pack(fill="x", pady=(0, 8))
        ttk.Label(mode_bar, text="Thread mode:").pack(side="left")
        selector = ttk.Combobox(mode_bar, state="readonly", width=29,
            values=("Engineering threads (recommended)", "Physical threads (native — new blocks)"))
        selector.current(0)
        selector.pack(side="left", padx=8)
        selector.bind("<<ComboboxSelected>>", lambda e: (self.thread_mode_var.set(
            "engineering" if selector.current() == 0 else "physical"), self._settings()))
        ttk.Label(mode_bar, text="Failed geometry attempts are rolled back when possible.",
                  foreground=self.palette["muted"]).pack(side="left", padx=8)
        ttk.Label(wrapper, textvariable=self.document_var).pack(anchor="w", pady=(0, 8))
        split = ttk.Panedwindow(wrapper, orient="horizontal")
        split.pack(fill="both", expand=True)
        left = ttk.Frame(split, padding=5)
        right = ttk.Frame(split, padding=5)
        split.add(left, weight=3)
        split.add(right, weight=2)
        ttk.Label(left, text="Conversation", font=("Helvetica", 12, "bold")).pack(anchor="w")
        self.chat = tk.Text(left, wrap="word", state="disabled", font=("Helvetica", 11), padx=12, pady=10)
        self._theme_text(self.chat)
        self.chat.pack(fill="both", expand=True, pady=(5, 8))
        self.chat.tag_configure("user", foreground=self.palette["accent"], spacing1=11)
        self.chat.tag_configure("assistant", foreground=self.palette["assistant"], spacing1=10)
        self.chat.tag_configure("stream", foreground=self.palette["foreground"])
        input_area = ttk.Frame(left)
        input_area.pack(fill="x")
        self.input = tk.Text(input_area, height=4, wrap="word", font=("Helvetica", 11))
        self._theme_text(self.input)
        self.input.pack(side="left", fill="x", expand=True)
        self.input.bind("<Control-Return>", self._send_message)
        self.input.bind("<Command-Return>", self._send_message)
        self.send_btn = ttk.Button(input_area, text="Send", command=self._send_message)
        self.send_btn.pack(side="right", padx=(8, 0), fill="y")
        ttk.Label(left, text="Ctrl+Enter / Cmd+Enter to send", foreground=self.palette["muted"]).pack(anchor="e")
        ttk.Label(right, text="Activity & Results", font=("Helvetica", 12, "bold")).pack(anchor="w")
        tabs = ttk.Notebook(right)
        tabs.pack(fill="both", expand=True, pady=(5, 6))
        logframe = ttk.Frame(tabs)
        modelframe = ttk.Frame(tabs)
        historyframe = ttk.Frame(tabs)
        tabs.add(logframe, text="Activity")
        tabs.add(modelframe, text="Model")
        tabs.add(historyframe, text="History")
        self.log = tk.Text(logframe, wrap="word", state="disabled", font=("Menlo", 10))
        self._theme_text(self.log)
        self.log.pack(fill="both", expand=True)
        self.model = tk.Text(modelframe, wrap="word", state="disabled", font=("Menlo", 10))
        self._theme_text(self.model)
        self.model.pack(fill="both", expand=True)
        self.history_list = tk.Listbox(historyframe)
        self.history_list.configure(background=self.palette["input"],
                                    foreground=self.palette["foreground"],
                                    selectbackground=self.palette["selection"],
                                    selectforeground="#FFFFFF", relief="flat",
                                    highlightthickness=1,
                                    highlightbackground=self.palette["border"])
        self.history_list.pack(fill="both", expand=True)
        ttk.Button(historyframe, text="View selected instruction", command=self._view_history).pack(fill="x")
        controls = ttk.Frame(right)
        controls.pack(fill="x")
        ttk.Button(controls, text="Save history", command=self._save_history_as).pack(side="left")
        ttk.Button(controls, text="Clear conversation", command=self._clear).pack(side="right")

    def _append(self, widget, value, tag=None):
        widget.configure(state="normal")
        if tag: widget.insert("end", value, tag)
        else: widget.insert("end", value)
        widget.see("end")
        widget.configure(state="disabled")

    def _settings(self):
        if not self.verify_var.get() and self.correct_var.get():
            self.correct_var.set(False)
        self.commands.put({"kind": "settings", "auto": self.auto_var.get(),
            "verify": self.verify_var.get(), "correct": self.correct_var.get(),
            "debug": self.debug_var.get(), "thread_mode": self.thread_mode_var.get()})

    def _send_message(self, event=None):
        if self.busy or not self.connected: return "break"
        text = self.input.get("1.0", "end").strip()
        if not text: return "break"
        self.input.delete("1.0", "end")
        self._append(self.chat, "\nYou: " + text + "\n", "user")
        self.history.append({"time": datetime.now().isoformat(timespec="seconds"),
                             "role": "user", "text": text})
        self._save_history()
        self.history_list.insert("end", datetime.now().strftime("%H:%M") + " · " + text[:65])
        self._append(self.chat, "\nAssistant: ", "assistant")
        self.commands.put({"kind": "chat", "text": text})
        self._lock(True)
        return "break"

    def _send_action(self, kind):
        if self.busy or not self.connected: return
        self.commands.put({"kind": kind})
        self._lock(True)

    def _lock(self, busy):
        self.busy = busy
        status = "disabled" if (busy or not self.connected) else "normal"
        for b in (self.new_btn, self.inspect_btn, self.send_btn): b.configure(state=status)

    def _poll(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "connection":
                    self.connected = data["connected"]
                    self.status_var.set(data["text"])
                    self._lock(self.busy)
                elif kind == "busy": self._lock(data["busy"])
                elif kind == "status":
                    self.status_var.set(data["text"])
                    self._append(self.log, "\n" + data["text"] + "\n")
                elif kind == "log": self._append(self.log, data["text"] + "\n")
                elif kind == "token": self._append(self.chat, data["text"], "stream")
                elif kind == "assistant":
                    self._append(self.chat, "\n" + data["text"] + "\n", "assistant")
                    self._record_assistant(data["text"])
                elif kind == "snapshot":
                    snap = data["snapshot"]
                    self.document_var.set(f"Document: {snap.get('document') or '(none)'} · Objects: {len(snap.get('objects', []))}")
                    self.model.configure(state="normal")
                    self.model.delete("1.0", "end")
                    self.model.insert("end", json.dumps(snap, indent=2, ensure_ascii=False))
                    self.model.configure(state="disabled")
                elif kind == "review":
                    v = data["verdict"]
                    s = f"\nVerification: {v['status'].upper()}\n{v.get('summary', '')}\n"
                    for name in ("verified", "issues", "unverified"):
                        items = v.get(name, [])
                        if items: s += name.title() + ":\n" + "".join("  • " + str(x) + "\n" for x in items)
                    self._append(self.chat, s, "assistant")
                    self._append(self.log, s)
                    self._record_assistant(s)
                elif kind == "approval":
                    code = data["code"]
                    # tkinter dialog runs only on the Tk main thread.
                    data["response"].put(self._approve_code(code))
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _approve_code(self, code):
        """Consent dialog with code collapsed by default."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Approve FreeCAD action")
        dialog.geometry("660x240")
        dialog.transient(self.root)
        dialog.grab_set()
        result = {"approved": False}
        ttk.Label(dialog, text="Allow the AI to execute a FreeCAD operation?",
                  font=("Helvetica", 12, "bold")).pack(anchor="w", padx=14, pady=(14, 4))
        ttk.Label(dialog, text="This may modify the active drawing. Review code if needed.").pack(
                  anchor="w", padx=14, pady=(0, 8))
        source = tk.Text(dialog, wrap="none", height=5, font=("Menlo", 10))
        self._theme_text(source)
        source.insert("1.0", code)
        source.configure(state="disabled")
        shown = {"value": False}
        def toggle():
            shown["value"] = not shown["value"]
            if shown["value"]:
                source.pack(fill="both", expand=True, padx=14, pady=5)
                dialog.geometry("750x450")
                reveal.configure(text="Hide code")
            else:
                source.pack_forget()
                dialog.geometry("660x240")
                reveal.configure(text="View code")
        def finish(approved):
            result["approved"] = approved
            dialog.destroy()
        buttons = ttk.Frame(dialog)
        buttons.pack(side="bottom", fill="x", padx=14, pady=12)
        reveal = ttk.Button(buttons, text="View code", command=toggle)
        reveal.pack(side="left")
        ttk.Button(buttons, text="Cancel", command=lambda: finish(False)).pack(side="right", padx=5)
        ttk.Button(buttons, text="Approve", command=lambda: finish(True)).pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", lambda: finish(False))
        dialog.wait_window()
        return result["approved"]

    def _record_assistant(self, text):
        self.history.append({"time": datetime.now().isoformat(timespec="seconds"),
                             "role": "assistant", "text": text})
        self._save_history()

    def _save_history(self):
        try: HISTORY_FILE.write_text(json.dumps(self.history, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError: pass

    def _load_history(self):
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list): self.history = data[-500:]
        except (OSError, ValueError): return
        for entry in self.history:
            role = entry.get("role", "assistant")
            self._append(self.chat, f"\n{'You' if role == 'user' else 'Assistant'}: {entry.get('text','')}\n",
                         "user" if role == "user" else "assistant")
            if role == "user":
                self.history_list.insert("end", entry.get("time", "")[:16] + " · " + entry.get("text", "")[:60])

    def _view_history(self):
        selection = self.history_list.curselection()
        if not selection: return
        user_msgs = [x for x in self.history if x.get("role") == "user"]
        i = selection[0]
        if i < len(user_msgs): messagebox.showinfo("Previous instruction", user_msgs[i]["text"])

    def _save_history_as(self):
        filename = filedialog.asksaveasfilename(defaultextension=".json",
                    filetypes=[("JSON files", "*.json")], title="Export chat history")
        if filename: Path(filename).write_text(json.dumps(self.history, indent=2, ensure_ascii=False), encoding="utf-8")

    def _clear(self):
        if not messagebox.askyesno("Clear conversation", "Clear saved chat history? Drawings will not be modified."): return
        self.history.clear()
        self._save_history()
        self.history_list.delete(0, "end")
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")

    def close(self):
        # The worker is a daemon thread; closing the app will end the session.
        # Do not close during active CAD execution without acknowledging risk.
        if self.busy and not messagebox.askyesno("Active operation", "An operation may be running. Close anyway?"): return
        self.commands.put({"kind": "quit"})
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    DesktopApp(root)
    root.mainloop()