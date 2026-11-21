# lejit-perf

Performance tools for LeJIT (`tonysimpson/lejit`): the benchmarks, the
instruction counts and their per-merge history. Moved out of the LeJIT
repository (then `ni`) at `0c1a792` on 20 Nov 2026, where the earlier history
of every file here is.

Every tool measures the LeJIT checkout given by `--lejit DIR`, else the
`LEJIT` environment variable, else `~/work/lejit`, on the Python that runs it
(`benchmarks.sh` takes `--python` or `PY`). The checkout must be built in
place for that Python (`make PY=python3.10`); use a release build, not the
checked build `make check` leaves. Run Python under
`prlimit --as=4000000000`, and take a heavy-job slot before a long run.

- `tools/pyperformance/`, `tools/pyperf37.py`: the pyperformance benchmarks
  that run on 3.7–3.10, and their driver: LeJIT against CPython, best of five
  in fresh processes. `-k NAME` picks benchmarks.
- `tools/bench.py`: small kernels, LeJIT against CPython.
- `tools/benchmarks.sh`: builds the checkout for release, runs both, and
  keeps the tables in `build/bench/`.
- `tools/instrcount.py`: instructions per benchmark, warm and cold, under
  `perf stat`. Needs `perf` and `setarch`.
- `tools/instrhistory.py`, `bench/history/instrcount.jsonl`: those counts per
  merge to LeJIT's main: `record [COMMIT]` builds a commit in
  `build/instrhistory/` and counts it on each version, `report [A [B]]`
  compares two, `graph` writes an HTML page.
- `tools/samecode.py A B`: does LeJIT emit the same machine code with two
  revisions or checkouts, per test and benchmark. Builds with
  `LE_CODESTATS=1` and needs `capstone` in the Python that runs it.
- `tools/profile_bench.py BENCH SECONDS`: one benchmark warm, for `perf record`.
- `mite/`: Tony's mite load-testing framework benchmarked with and without
  LeJIT (21 Nov 2026): `REPORT.md` has the results, where the time goes, and
  how to rerun (it needs a mite clone, a venv and a LeJIT build).

Examples:

    python3.10 tools/instrcount.py warm -k richards -k nbody -r 2
    python3.10 tools/pyperf37.py -k richards --lejit ~/work/lejit-worktrees/x
    tools/benchmarks.sh --python python3.10 bench
    python3 tools/instrhistory.py record --python 3.10
    python3.10 tools/samecode.py HEAD~1 HEAD --no-tests

## Notes

- **Profiling:** `perf record -m 16 -F 4999 -g` on `tools/profile_bench.py BENCH SECONDS` (release build, warm), then `perf report --no-children --sort sym -g none`. Emitted code shows as `[JIT]`. Needs `kernel.perf_event_paranoid` ≤ 2.
- **The figure to judge a change by** is warm instructions per iteration (`perf stat -e instructions:u` at two run lengths, `PYTHONHASHSEED=0`, `setarch -R`), not wall time. Check a ~1% move against heap state (`GLIBC_TUNABLES=glibc.malloc.tcache_count=0`, `PYTHONMALLOC=malloc`, callgrind by object) before believing it.
- **pyperformance on 3.7:** from 1.0.3 most benchmarks declare `requires-python >=3.8`, and pyperformance drops them. So `tools/pyperf37.py` runs each vendored `bm_X.py` itself, with a stand-in `pyperf` module that calls the benchmark's functions directly, in one process per mode.
- **History records** name the tool by a hash of `instrcount.py`, `pyperf37.py` and the benchmarks, so any change to them starts a new series; `report` compares two commits by a tool that measured both if there is one.
