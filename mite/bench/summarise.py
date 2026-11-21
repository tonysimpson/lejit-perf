"""Summarise results/runs-*.jsonl: median and spread per benchmark and mode."""
import glob
import json
import os
import statistics
import sys

out = sys.argv[1]
runs = {}
errors = []
for fn in glob.glob(os.path.join(out, "runs-*.jsonl")):
    for line in open(fn):
        d = json.loads(line)
        if "error" in d:
            errors.append(d)
            continue
        runs.setdefault((d["bench"], d["mode"]), []).append(d)

order = ["codec", "context_send", "runner", "controller", "stats", "stats_http",
         "collector", "collector_json", "duplicator", "prom_update", "prom_format",
         "pipeline"]
units = {"controller": "req/s", "prom_update": "dumps/s", "prom_format": "scrapes/s"}


def fmt(x):
    return f"{x/1e6:.2f}M" if x >= 1e6 else f"{x/1e3:.1f}k" if x >= 1e4 else f"{x:.0f}"


def med(rs, k):
    v = [r[k] for r in rs if r.get(k) is not None]
    return statistics.median(v) if v else None


def spread(rs):
    v = [r["warm"] for r in rs]
    m = statistics.median(v)
    return f"±{(max(v) - min(v)) / 2 / m * 100:.0f}%"


print("| benchmark | unit | plain | LeJIT | speed-up | LeJIT warm-up | late-start | late warm-up | RSS plain | RSS LeJIT | runs |")
print("|---|---|---|---|---|---|---|---|---|---|---|")
for b in order:
    p, l, t = (runs.get((b, m), []) for m in ("plain", "lejit", "late"))
    if not p or not l:
        continue
    mp, ml = med(p, "warm"), med(l, "warm")
    mt = med(t, "warm") if t else None
    wl, wt = med(l, "warmup_s"), med(t, "warmup_s") if t else None
    print(f"| {b} | {units.get(b, 'msg/s')} | {fmt(mp)} {spread(p)} | {fmt(ml)} {spread(l)} "
          f"| {ml/mp:.2f}x | {wl:.2f} s | {fmt(mt) if mt else '-'} ({mt/mp:.2f}x) "
          f"| {wt:.2f} s | {med(p, 'maxrss_mb'):.0f} MB | {med(l, 'maxrss_mb'):.0f} MB "
          f"| {len(p)}/{len(l)}/{len(t)} |")
if errors:
    print("\nErrors:")
    for e in errors:
        print(" ", e)
