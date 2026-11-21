# lejit-perf

Performance for the core repo (`tonysimpson/ni`, to be `lejit`): the
benchmarks, the instruction counts and their per-commit history, and the tools
of the studies behind past optimisations. Moved out of it at `0c1a792` on 20 Nov
2026, where the history of every file here is. Not yet adapted to run from
here: each expects to sit at its old path in a ni checkout, and takes a
heavy-job slot through ni's `tools/heavy.py`.

- `tools/pyperformance/`, `tools/pyperf37.py`: the pyperformance benchmarks
  that run on 3.7–3.10, and their driver.
- `tools/benchmarks.sh`, `tools/bench.py`: timing runs, plain against the JIT
  (its `adversarial` suite is in lejit-extra-tests now).
- `tools/instrcount.py`: instructions per benchmark, warm and cold, under
  `perf stat`, with heap controls.
- `tools/instrhistory.py`, `bench/history/instrcount.jsonl`: those counts per
  merge, with `report` and `graph` (an HTML page).
- `tools/samecode.py`: does a change emit the same machine code per benchmark.
- The studies' tools: `codesize`, `promostats`, `compilebench`,
  `profile_bench`, `interpreted`, `snapstudy`, `stubstudy`, `strstudy`,
  `store*`, `closure*`, `load*`. They read counters a build with
  `NI_CODESTATS` and the like exposes.
- `mite/`: Tony's mite load-testing framework benchmarked with and without
  LeJIT (21 Nov 2026): `REPORT.md` has the results, where the time goes, and
  how to rerun (it needs a mite clone, a venv and a LeJIT build).

## Notes

- **Profiling:** `perf record -m 16 -F 4999 -g` on `tools/profile_bench.py BENCH SECONDS` (release build, warm), then `perf report --no-children --sort sym -g none`. Emitted code shows as `[JIT]`. Needs `kernel.perf_event_paranoid` ≤ 2.
- **The figure to judge a change by** is warm instructions per iteration (`perf stat -e instructions:u` at two run lengths, `PYTHONHASHSEED=0`, `setarch -R`), not wall time. Check a ~1% move against heap state (`GLIBC_TUNABLES=glibc.malloc.tcache_count=0`, `PYTHONMALLOC=malloc`, callgrind by object) before believing it.
- **pyperformance on 3.7:** from 1.0.3 most benchmarks declare `requires-python >=3.8`, and pyperformance drops them. So `tools/pyperf37.py` runs each vendored `bm_X/run_benchmark.py` directly with pyperf 2.8.0, the last release for 3.7. pyperf starts fresh worker processes, so the JIT must be started in each worker.
