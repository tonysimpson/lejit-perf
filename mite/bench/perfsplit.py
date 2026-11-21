"""Break a perf.data file's self cycles down by category and list the top
symbols.   python -I perfsplit.py FILE.data [N_TOP]

Categories: CPython eval loop; CPython object code (dict, float, tuple,
unicode/repr, alloc/free, GC, other); LeJIT compiled code (anonymous
executable mappings, which have no symbols); LeJIT runtime and compiler
(_core*.so); C extensions; libc; kernel; other."""

import collections
import re
import subprocess
import sys

path = sys.argv[1]
ntop = int(sys.argv[2]) if len(sys.argv) > 2 else 15

txt = subprocess.run(
    ["perf", "report", "-i", path, "--no-children", "--sort", "dso,sym",
     "--stdio", "-q", "-g", "none", "--percent-limit", "0"],
    capture_output=True, text=True).stdout

rows = []
for line in txt.splitlines():
    m = re.match(r"\s*([\d.]+)%\s+(.*?)\s+\[(.)\]\s+(.*\S)", line)
    if m:
        rows.append((float(m.group(1)), m.group(2), m.group(3), m.group(4)))

OBJ = [
    ("float->str (dtoa)", r"_Py_dg_dtoa|^diff$|quorem|multadd|Balloc|Bfree|lshift|pow5mult|^mult$|hi0bits|lo0bits|^b2d|^d2b|^cmp$|double_to_string|float_repr"),
    ("hashing", r"siphash|_Py_HashBytes|PyObject_Hash|tuplehash|_Py_HashDouble|long_hash|unicode_hash"),
    ("dict", r"dict|lookdict|insertdict|PyDict|_Py_dict|defdict"),
    ("float", r"float|PyFloat"),
    ("tuple", r"tuple|PyTuple"),
    ("unicode/repr/format", r"unicode|PyUnicode|_PyUnicodeWriter|repr|Repr|format|Format|ascii|utf8|_Py_dg_|ucs"),
    ("int", r"long_|PyLong|_PyLong"),
    ("alloc/free", r"[Mm]alloc|[Ff]ree|pymalloc|_PyObject_GC_(New|Alloc|Del|Track|UnTrack)|object_dealloc|dealloc|Dealloc|arena|_PyMem|PyMem"),
    ("GC", r"^collect|gc_|visit_|subtract_refs|update_refs|move_unreachable|traverse"),
    ("calls/attrs/frames", r"Vectorcall|vectorcall|_PyObject_Call|PyObject_Call|_PyFunction|function_code|frame|Frame|GetAttr|getattr|GenericGetAttr|_PyType_Lookup|method_|cfunction|slot_|PyObject_RichCompare|RichCompare|contextvar|PyContext|gen_|Gen"),
]


def category(dso, sym):
    if dso.startswith("[kernel") or dso == "[vdso]":
        return "kernel"
    if dso.startswith("_core") and "cpython" in dso:
        return "LeJIT runtime+compiler (_core.so)"
    if dso.startswith("[JIT]") or dso in ("[unknown]", "//anon", "[anon]") or dso.startswith("//anon") \
            or dso.startswith("[anon") or dso.startswith("perf-"):
        return "LeJIT compiled code (anon exec)"
    if "libpython" in dso or dso.startswith("python3"):
        if sym in ("_PyEval_EvalFrameDefault", "_PyEval_EvalFrame",
                   "_PyEval_Vector") or sym.startswith("_PyEval_EvalFrame"):
            return "CPython eval loop"
        for name, rx in OBJ:
            if re.search(rx, sym):
                return "CPython " + name
        return "CPython other"
    if "msgpack" in dso:
        return "C ext: msgpack"
    if "zmq" in dso:
        return "C ext: zmq"
    if dso.startswith("loop.") or "uvloop" in dso or "libuv" in dso:
        return "C ext: uvloop"
    if dso.startswith("_") and ".so" in dso:
        return "C ext: stdlib (" + dso.split(".")[0] + ")"
    if dso.startswith("libc") or dso.startswith("ld-linux") or dso.startswith("libm"):
        return "libc/libm"
    return "other (" + dso + ")"


cats = collections.Counter()
for pct, dso, kind, sym in rows:
    cats[category(dso, sym)] += pct
total = sum(cats.values())
name = path.split("/")[-1].replace(".data", "")
print(f"== {name}  ({len(rows)} symbols, {total:.1f}% accounted)")
for c, v in cats.most_common():
    if v >= 0.1:
        print(f"   {v:5.1f}%  {c}")
print("   top symbols:")
for pct, dso, kind, sym in sorted(rows, reverse=True)[:ntop]:
    print(f"   {pct:5.2f}%  {dso[:28]:28s} {sym[:70]}")
print()
# LeJIT detail: compiled code by mapping (1 MB region), and _core.so symbols
jit = [(p, s) for p, d, k, s in rows if category(d, s).startswith("LeJIT compiled")]
if jit:
    regions = collections.Counter()
    for p, s in jit:
        try:
            regions[int(s, 16) >> 20 << 20] += p
        except ValueError:
            regions[s] += p
    print("   LeJIT compiled code by 1 MB region (dso '%s'):" %
          next(d for p, d, k, s in rows if category(d, s).startswith("LeJIT compiled")))
    for r, p in regions.most_common(4):
        print(f"     {p:5.2f}%  {hex(r) if isinstance(r, int) else r}")
core = [(p, s) for p, d, k, s in rows if category(d, s).startswith("LeJIT runtime")]
if core:
    print("   top _core.so symbols:")
    for p, s in sorted(core, reverse=True)[:8]:
        print(f"     {p:5.2f}%  {s}")
    print()
