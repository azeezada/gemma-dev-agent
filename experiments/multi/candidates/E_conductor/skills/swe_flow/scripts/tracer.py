"""Run a script and print what the repository's own functions received and returned.

    python tracer.py SCRIPT      (env FLOW_ROOT = repository root)

Only functions defined in FLOW_ROOT (not tests, not the script) are recorded, up to
nesting depth 3 counted in repository frames. Generators show the values they yielded.
This shows where a value first differs from what the script expects, without reading code.
"""
import os
import reprlib
import runpy
import sys

ROOT = os.path.realpath(os.environ.get("FLOW_ROOT", os.getcwd())) + os.sep
MAX_RECORDS = 600
MAX_DEPTH = 6
CO_GENERATOR = 0x20
CO_OPTIMIZED = 0x1  # unset for module and class bodies (import-time noise)
TRANSPARENT = {"<genexpr>", "<listcomp>", "<dictcomp>", "<setcomp>", "<lambda>", "<module>"}
KEEP_DUNDERS = {"__init__", "__call__", "__iter__", "__next__", "__getitem__", "__rich_console__", "__str__"}
_repr = reprlib.Repr()
_repr.maxstring = 48
_repr.maxother = 48
_repr.maxlist = 4
_repr.maxdict = 3
_repr.maxtuple = 4

SCRIPT = [""]  # realpath of the traced script; top-level records must be called from it
records = []   # dicts in call order
active = {}    # id(frame) -> record


def short(value):
    try:
        return _repr.repr(value)
    except Exception:
        return "<{}>".format(type(value).__name__)


_cache = {}


def interesting(code):
    name = code.co_filename
    if name in _cache:
        return _cache[name]
    _cache[name] = _interesting(name)
    return _cache[name]


def _interesting(name):
    if not os.path.isabs(name) or not os.path.isfile(name):  # <frozen ...>, <string>
        return False
    path = os.path.realpath(name)
    if not path.startswith(ROOT):
        return False
    rel = path[len(ROOT):]
    parts = rel.split(os.sep)
    return not ({"tests", "test", "testing"} & set(parts[:-1])) and not parts[-1].startswith("test_")


def parent_record(frame):
    back, hops = frame.f_back, 0
    while back is not None and hops < 60:
        record = active.get(id(back))
        if record is not None:
            return record
        back, hops = back.f_back, hops + 1
    return None


def called_from_script(frame):
    back, hops = frame.f_back, 0
    while back is not None and hops < 8:
        if back.f_code.co_filename == SCRIPT[0] or os.path.realpath(back.f_code.co_filename) == SCRIPT[0]:
            return True
        if back.f_code.co_name not in TRANSPARENT:
            return False
        back, hops = back.f_back, hops + 1
    return False


def profile(frame, event, arg):
    if event == "call":
        code = frame.f_code
        if id(frame) in active or not interesting(code) or len(records) >= MAX_RECORDS:
            return
        if (code.co_name in TRANSPARENT or not code.co_flags & CO_OPTIMIZED
                or (code.co_name.startswith("__") and code.co_name not in KEEP_DUNDERS)):
            return  # children attach to the nearest recorded ancestor
        parent = parent_record(frame)
        if parent is None and not called_from_script(frame):
            return  # import-time or library-internal work, not something the script asked for
        depth = parent["depth"] + 1 if parent else 1
        if depth > MAX_DEPTH:
            return
        names = code.co_varnames[:code.co_argcount + code.co_kwonlyargcount]
        argv = [short(frame.f_locals.get(n)) for n in names if n not in ("self", "cls")]
        record = {"depth": depth, "name": getattr(code, "co_qualname", code.co_name), "args": ", ".join(argv),
                  "argv": argv,
                  "gen": bool(code.co_flags & CO_GENERATOR), "yields": [], "ret": None, "done": False,
                  "file": os.path.realpath(code.co_filename)[len(ROOT):], "line": code.co_firstlineno}
        records.append(record)
        active[id(frame)] = record
    elif event == "return":
        record = active.get(id(frame))
        if record is None:
            return
        if record["gen"]:
            if arg is not None:
                record["yields"].append(short(arg))
            else:
                record["done"] = True
                active.pop(id(frame), None)
        else:
            record["ret"] = short(arg)
            record["done"] = True
            active.pop(id(frame), None)


def line_for(record):
    if record["gen"]:
        shown = ", ".join(record["yields"][:6]) + (", ..." if len(record["yields"]) > 6 else "")
        result = "yielded [{}]".format(shown)
    else:
        result = "-> " + (record["ret"] if record["done"] else "(raised)")
    return "{}{}({}) {}   [{}:{}]".format("  " * (record["depth"] - 1), record["name"], record["args"],
                                         result, record["file"], record["line"])


def render(limit_chars=1800):
    """Top-level calls with up to 6 children each; repeated top-level functions shown up to 4 times."""
    groups, current = [], None
    for record in records:
        if record["depth"] == 1:
            current = [record]
            groups.append(current)
        elif current is not None and not (record["done"] and not record["gen"] and record["ret"] == "None"):
            # nested calls that returned None (setters, __init__) carry no value information
            current.append(record)
    # Calls whose inputs appear in the script's FAIL lines come first (FLOW_FOCUS = those lines).
    focus = set(filter(None, os.environ.get("FLOW_FOCUS", "").split("\n")))
    hit = lambda group: any(a in focus for a in group[0].get("argv", []))
    groups = [g for g in groups if hit(g)] + [g for g in groups if not hit(g)]
    seen, text, used, dropped = {}, [], 0, 0
    for group in groups:
        name = group[0]["name"]
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 4:
            dropped += 1
            continue
        # Keep the 7 most informative nested calls, in call order: ones that received arguments
        # (data flowing through) first, then the rest.
        nested = group[1:]
        ranked = sorted(range(len(nested)), key=lambda i: (not nested[i]["args"], i))[:7]
        block = [line_for(group[0])] + [line_for(nested[i]) for i in sorted(ranked)]
        size = sum(len(line) + 1 for line in block)
        if used + size > limit_chars:
            if used + len(block[0]) < limit_chars:
                text.append(block[0])
                used += len(block[0]) + 1
            dropped += 1
            continue
        text += block
        used += size
    if dropped:
        text.append("... ({} more top-level calls not shown)".format(dropped))
    return "\n".join(text)


def main():
    if len(sys.argv) < 2:
        print("usage: tracer.py SCRIPT")
        return 2
    script = sys.argv[1]
    SCRIPT[0] = os.path.realpath(script)
    sys.argv = sys.argv[1:]
    sys.path.insert(0, os.path.dirname(os.path.abspath(script)))
    code = 0
    stdout = sys.stdout
    devnull = open(os.devnull, "w")
    sys.stdout = devnull  # the script's own output is shown separately by flow.py
    sys.setprofile(profile)
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except BaseException as exc:  # the script crashed; the trace up to here is still useful
        code = 1
        records.append({"depth": 1, "name": "EXCEPTION " + type(exc).__name__, "args": short(str(exc)),
                        "gen": False, "yields": [], "ret": "", "done": True, "file": "", "line": 0})
    finally:
        sys.setprofile(None)
        sys.stdout = stdout
        devnull.close()
    print(render(int(os.environ.get("FLOW_TRACE_CHARS", "1800"))))
    return code


if __name__ == "__main__":
    sys.exit(main())
