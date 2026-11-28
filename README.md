# lejit-perf

Performance tools for LeJIT (`tonysimpson/lejit`): the benchmarks, the
instruction counts and their per-merge history. Moved out of the LeJIT
repository (then `ni`) at `0c1a792` on 20 Nov 2026, where the earlier history
of every file here is.

From a fresh clone, once (it needs pyenv; it installs any missing CPython
3.7.17–3.10.16 and makes a venv for each under `build/venv/`):

    tools/setup.sh

Every tool then runs on `--python` (a version, pyenv's newest installed
match, or a path; default 3.10) with `--lejit` installed into that version's
venv first, a release build by pip: a GitHub ref of `tonysimpson/lejit`
(branch, tag or full SHA) or a local directory, which is copied, so nothing
is built in it; default the `LEJIT` environment variable, else `main`. Run
Python under `prlimit --as=4000000000`, and take a heavy-job slot before a
long run.

- `tools/pyperformance/`, `tools/pyperf37.py`: the pyperformance benchmarks
  that run on 3.7–3.10, and their driver: LeJIT against CPython, best of five
  in fresh processes. `-k NAME` picks benchmarks.
- `tools/pyperformance_extended/`, `tools/pyperfext.py`: the rest of
  pyperformance 1.14.0 that runs in-process (`benchmarks.py` there lists each
  benchmark's script, options and what it needs), counted as `instrcount.py`
  counts the core 23: `pyperfext.py warm|cold [--set all]`, with the same
  options. `--set all` prints the core rows and total first, as
  `instrcount.py` does. A benchmark is skipped, with a row saying why, on a
  Python that can't compile it, lacks a module or attribute it needs, can't
  install its packages, or fails it under plain CPython. Third-party packages
  are installed by pip, pinned as pyperformance pins them, into
  `build/pydeps/<version>/<script>/` on first use. `instrcount.py`, the core
  set and the history are unchanged by it.
- `tools/bench.py`: small kernels, LeJIT against CPython.
- `tools/benchmarks.sh`: installs LeJIT, runs both, and keeps the tables in
  `build/bench/`.
- `tools/setup.sh`, `tools/lejitenv.py`: the Pythons and venvs, and the
  LeJIT install every tool does.
- `tools/instrcount.py`: instructions per benchmark, warm and cold, under
  `perf stat`. Needs `perf` and `setarch`.
- `tools/cyclecount.py --lejit NEW [--base main]`: cycles, instructions,
  branch misses and L1 instruction, L1 data, L2 and DRAM misses per
  warm iteration, counted inside the benchmark process on chunks free of
  SMT contention, base against new, over three
  alternating rounds, each laid out differently (a small function compiled
  first moves the code and heap), with each round's change and the noise. For changes
  to emitted code, where instruction counts miss microcoded instructions,
  latency and layout. The docstring says what each event counts on Zen 5.
- `tools/instrhistory.py`, `bench/history/instrcount.jsonl`: those counts per
  merge to LeJIT's main: `record [COMMIT]` takes a commit of the repository
  `--lejit DIR` (default `~/work/lejit`) out to `build/instrhistory/`,
  installs it into each version's venv and counts it, `report [A [B]]`
  compares two, `graph` writes an HTML page.
- `tools/samecode.py A B`: does LeJIT emit the same machine code with two
  revisions or checkouts, per test and benchmark. Builds with
  `LEJIT_DEV_CODESTATS=1` in its own trees and needs `capstone` in the Python
  that runs it.
- `tools/profile_bench.py BENCH SECONDS`: one benchmark warm, for `perf
  record`; run it with a venv's Python after `tools/lejitenv.py install`.
- `mite/`: Tony's mite load-testing framework benchmarked with and without
  LeJIT (21 Nov 2026): `REPORT.md` has the results, where the time goes, and
  how to rerun (it needs a mite clone, a venv and a LeJIT build).

Examples:

    python3 tools/instrcount.py warm -k richards -k nbody -r 2
    python3 tools/pyperfext.py warm --python 3.15 -k regex -r 2
    python3 tools/cyclecount.py --python 3.10 --lejit my-branch -k richards
    python3 tools/pyperf37.py -k richards --lejit ~/work/lejit-worktrees/x
    tools/benchmarks.sh --python 3.7 --lejit my-branch bench
    python3 tools/instrhistory.py record --python 3.10
    python3.10 tools/samecode.py HEAD~1 HEAD --no-tests

## Notes

- **Profiling:** `perf record -m 16 -F 4999 -g` on `build/venv/3.10.16/bin/python tools/profile_bench.py BENCH SECONDS` (release build, warm), then `perf report --no-children --sort sym -g none`. Emitted code shows as `[JIT]`. Needs `kernel.perf_event_paranoid` ≤ 2.
- **The figure to judge a change by** is warm instructions per iteration (`perf stat -e instructions:u` at two run lengths, `PYTHONHASHSEED=0`, `setarch -R`), not wall time. Check a ~1% move against heap state (`GLIBC_TUNABLES=glibc.malloc.tcache_count=0`, `PYTHONMALLOC=malloc`, callgrind by object) before believing it.
- **pyperformance on 3.7:** from 1.0.3 most benchmarks declare `requires-python >=3.8`, and pyperformance drops them. So `tools/pyperf37.py` runs each vendored `bm_X.py` itself, with a stand-in `pyperf` module that calls the benchmark's functions directly, in one process per mode.
- **History records** name the tool by a hash of `instrcount.py`, `pyperf37.py` and the benchmarks, so any change to them starts a new series; `report` compares two commits by a tool that measured both if there is one.
