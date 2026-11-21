"""From a plain cProfile dump: share of own time spent in generator or
coroutine code (which LeJIT runs interpreted), in other Python code (which
it compiles), and in C functions.  python -I genshare.py FILE.prof"""
import inspect
import pstats
import sys

GEN = inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR
_cache = {}


def flags_for(filename):
    if filename not in _cache:
        m = {}
        try:
            code = compile(open(filename).read(), filename, "exec")
        except Exception:
            code = None
        todo = [code] if code else []
        while todo:
            c = todo.pop()
            m[(c.co_firstlineno, c.co_name)] = c.co_flags
            todo.extend(k for k in c.co_consts if hasattr(k, "co_code"))
        _cache[filename] = m
    return _cache[filename]


st = pstats.Stats(sys.argv[1]).stats
tot = {"gen/coroutine (interpreted)": 0.0, "other Python (compiled)": 0.0, "C": 0.0}
top = []
for (fn, line, name), (cc, nc, tt, ct, callers) in st.items():
    if fn == "~" or fn.startswith("<"):
        tot["C"] += tt
        continue
    fl = flags_for(fn).get((line, name))
    if fl is not None and fl & GEN and name != "<genexpr>":
        tot["gen/coroutine (interpreted)"] += tt
        top.append((tt, f"{fn.split('/')[-1]}:{line}:{name}"))
    else:
        tot["other Python (compiled)"] += tt
s = sum(tot.values())
print(sys.argv[1].split("/")[-1], {k: f"{v / s * 100:.0f}%" for k, v in tot.items()})
for tt, n in sorted(top, reverse=True)[:6]:
    print(f"    {tt / s * 100:4.1f}%  {n}")
