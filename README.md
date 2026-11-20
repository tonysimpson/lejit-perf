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
