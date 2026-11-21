I've benchmarked mite's busy components with and without LeJIT, in-process and with the sockets replaced. LeJIT gives ×1.5 on the controller and almost nothing on stats, and found no LeJIT bugs.

Setup: mite HEAD on CPython 3.10.16, and a release build of LeJIT (`lejit.start(check=True, error=True)` passes). Each run was pinned to its own core, two at a time, under `prlimit --as=4000000000`. Things I changed from the brief:
- `aio_pika>=10` needs Python 3.11, so I installed 9.6.2.
- I installed `selenium<4` so that every stats plugin of a full install loads (32 processors; playwright is a separate install).
- I didn't install mite itself; a stub dist-info in `bench/site` registers its entry points.
- The controller runs with `spawn_rate=None`, so the work it hands out is the same in both modes.

## 1. Components ranked by load
1. **Stats: `mite/stats.py:matcher_by_type.type_matcher`.** About 140 µs per message, so one `mite stats` process tops out at about 7,000 messages/s (29,000 with only the mite and mite_http processors).
   - 88% of that is one line: `logger.debug(f"message to match by type: {msg}")`.
   - The f-string is built before the log level is checked, so each of the 32 processors formats the whole message dict for every message, even at INFO.
   - So the cost is messages × processors × message size.
   - The real aggregation (`Stats.process`, each processor's `process`, `extract_items`, dict updates, 15 histogram bins) is about 5.5 µs.
2. **Controller: `mite/scenario.py:ScenarioManager.get_work`**, reached through `controller.py:Controller.request_work` and `zmq.py:ControllerServer.run`. It answers one request at a time. Each request:
   - builds a list of all runners in `RunnerTracker.get_active`, and two sets of all runner ids in `WorkTracker.get_total_work`;
   - does O(scenarios) dict work;
   - expands the shortfall into one list entry per missing journey and runs `random.shuffle` (pure Python) over all of it, before taking only `limit` of them.

   Runners ask again as soon as any journey ends (`loop_wait_min` is stored but never used). So the request rate and the cost of each request both grow with the number of runners: roughly O(runners²) in total. At the start of a test the shortfall is the whole volume: handing out 50,000 journeys to 500 runners takes 3.2 s.
3. **Runner: `runner.py:Runner._run`/`_execute`, `context.py:Context.transaction`/`send`.** About 5 µs per message, spread across many processes.
4. **Collector: `collector.py:Collector.process_raw_message`.** It only writes raw bytes, but `zmq.py:Receiver._run` decodes every message even when there are only raw listeners. `--collector-use-json` is about 4.5× slower.
5. **Prometheus exporter: `web/prometheus.py`.** `process` runs 4 times a second per stats process, and `format` once per scrape. Both are cheap. `process` has the same debug f-string, formatting the whole dump each time.
6. **Duplicator (`zmq.py:Duplicator._run`)** and **msgpack (`utils.py`)** are C or transport-bound.

`nanomsg.py:ControllerServer._run` calls the async `request_work` without `await`, so that path looks broken. ZMQ is the default; I didn't benchmark nanomsg.

## 2. Results
Median of 5 runs × 10 s each. "Warm" is the rate over the last 40% of the run; the spread is the range divided by 2.

| benchmark | plain | LeJIT | speed-up | LeJIT warm-up | started after 3 s | RSS plain / LeJIT |
|---|---|---|---|---|---|---|
| stats (32 processors) | 7,115 msg/s ±2% | 7,483 ±0% | 1.05× | <0.25 s | 1.04× | 66 / 165 MB |
| stats_http (7 processors) | 29.2k ±1% | 30.8k ±1% | 1.05× | <0.25 s | 1.05× | 66 / 153 MB |
| controller (500 runners, 50 scenarios) | 11.4k req/s ±1% | 17.6k ±1% | **1.54×** | 1.0 s (plain 3.25 s)¹ | 1.52× | 93 / 182 MB |
| runner (`Runner.run`, uvloop) | 203k msg/s ±1% | 212k ±1% | 1.04× | <0.25 s | 1.06× | 64 / 163 MB |
| context_send (journeys, no event loop) | 319k ±17% | 343k ±1% | 1.08× | <0.25 s | 1.07× | 41 / 129 MB |
| collector (raw) | 493k ±1% | 585k ±2% | 1.19× | <0.25 s | 1.20× | 66 / 153 MB |
| collector_json | 115k ±1% | 122k ±3% | 1.06× | <0.25 s | 1.09× | 66 / 153 MB |
| duplicator | 3.56M ±33% | 3.70M ±29% | 1.04× | <0.25 s | 1.06× | 66 / 153 MB |
| codec (msgpack unpack + pack) | 406k ±30% | 393k ±21% | 0.97× | <0.25 s | 0.96× | 66 / 152 MB |
| prom_update | 2,179 dumps/s ±9% | 2,279 ±1% | 1.05× | <0.25 s | 1.05× | 42 / 131 MB |
| prom_format | 412 scrapes/s ±1% | 621 ±2% | **1.51×** | <0.25 s | 1.50× | 42 / 129 MB |
| pipeline (journeys → collector + stats → prometheus) | 6,853 msg/s ±25% | 7,128 ±5% | 1.04× | <0.25 s | 1.05× | 68 / 169 MB |

¹ This is the workload ramp (the first ~17k requests hand out the initial journeys), not JIT warm-up. LeJIT gets through it about 3× faster.

- **Controller scaling** (steady state, 3 runs of 20 s):

  | runners | plain | LeJIT | speed-up |
  |---|---|---|---|
  | 100 | 6.9k req/s | 9.8k | 1.41× |
  | 500 | 11.2k | 17.0k | 1.52× |
  | 2,000 | 5.7k (177 µs per request) | 11.1k | 1.97× |

- **Memory:** LeJIT adds a roughly constant 90–100 MB of RSS.
- **Starting late:** LeJIT reaches full speed within a second of `lejit.start()`.
- **Noise:** the ±30% spreads on codec and duplicator come from a few noisy runs; their medians are stable.

## 3. Behaviour differences and LeJIT bugs
None. Every benchmark has a fixed-workload `--check` mode that hashes mite's output: collector files, Prometheus text, controller replies, and runner message counts. All 12 benchmarks gave the same hash in plain, LeJIT and late-start runs. There were no crashes or exceptions in 180 timed runs plus the scaling runs.

## 4. Where the time goes (the stats question)
**The benchmark does measure the hot path.** The socket stand-ins are C callables, imports (~0.09 s) happen before timing starts, and dumps run every 0.25 s. Split per message (µs, median):

| part | plain | LeJIT |
|---|---|---|
| dict `repr` in the debug f-string (C) | 121 (88%) | 119 (90%) |
| 32 `logger.debug` / `isEnabledFor` calls | ~9.5 (7%) | ~8.5 (6%) |
| the actual stats aggregation | ~5.5 (4%) | ~3.8 (3%) |
| msgpack `unpack_msg` (C) | 1.4 (1%) | 1.4 (1%) |
| **total** | **138** | **132** |

The function-level cProfile pass agrees: `type_matcher` holds 87% of own time (plain).

**perf is blocked:** `perf_event_paranoid` is 3. To allow it for Tony's own processes, run `sudo sysctl kernel.perf_event_paranoid=1` (`-1` for kernel symbols too; revert with `=3`). Then:
```
cd …/mite-bench/bench
perf record -g -o stats.data ../venv/bin/python -I mitebench.py stats --mode lejit
perf report -i stats.data
```

**What LeJIT does with the hot functions** (checked build in `lejit-checked/`, with `_stats()`, `_code_info()` and `_promostats()`):
- **Compiled:** `Receiver._run` is compiled. `Stats.process`, `Counter`/`Gauge`/`Histogram.process`, `type_matcher` and `Logger.debug` are compiled; most show "no entry point", which I take to mean they were compiled inside their caller rather than run by the interpreter.
- **Interpreted:** the `extract_items` generators are never compiled, because LeJIT never compiles generators or coroutines.
- **No recompiling:** compiling stops after the second batch of 2,500 messages: about 135 promotions and 48 KB of code, a few milliseconds in all.
- **Specialisation:** every promotion site on the hot path sees one type; the only megamorphic site is in setup code.
- **Floats:** these are boxed at every dict store (`sums[key] += value`), and the values arrive boxed from the interpreted generator. But there is very little float maths: per message one add and 15 comparisons per matching histogram. Most of the work is building label tuples and dict increments.

**Why the speed-up is small:**
- 89% of the time is C `dict.__repr__`/`float.__repr__` called from `FORMAT_VALUE`, and LeJIT can't speed up C.
- With that line taken out of the matchers (a what-if run, not mite as shipped), stats does 6.8 µs per message plain (≈146k msg/s, 20× faster) and 5.1 µs under LeJIT (≈195k msg/s, 1.33×).
- What remains is dict, tuple-hash and `defaultdict` work in C, plus interpreted generators.

**What LeJIT would need for the speed-up Tony expected:**
1. Treat the f-string result as virtual (lazy) and drop it when it's dead. That means inlining `Logger.debug` → `isEnabledFor`, guarding on the cached level, and knowing that `repr` of dicts of str, int, float and None has no side effects. This alone is worth up to about 20×.
2. Compile simple generators, or inline them into the consumer's loop as it already does for generator expressions. That would let the label tuple and value stay virtual.
3. Even then, the dict stores keep floats boxed, so expect about 2× on the remaining Python, not more.

The fix in mite itself is a one-liner: `logger.debug("…%s", msg)`, or drop the line. That gives 20× on plain CPython.

**Controller:** under plain CPython 29% of its time is in coroutines (`async def get_work`, `ControllerServer.run`, datapool `checkout`), which LeJIT runs interpreted. The 1.5–2× comes from compiling `random.shuffle`, the `get_active` list comprehension and the dict loops.

**Runner:** 37% of its time is in coroutines and generators (`async` journeys, `asynccontextmanager`), which LeJIT runs interpreted, so the runner gains only 1.04×.

## 5. How to rerun
- **Full suite** (5 runs × 3 modes, about 35 minutes, then the table): `cd ~/work/lejit-perf/mite/bench && ./runall.sh 5 10`
- **One benchmark:** `../venv/bin/python -I mitebench.py stats --mode plain|lejit|late [--check]`
- **Other scripts:**
  - `scale.sh`: controller throughput against the number of runners
  - `attrib.py plain|lejit`: the stats time split and the what-if run
  - `lejit_probe.py`: compile state of the stats path; needs the checked build in `lejit-checked/`
  - `genshare.py`: share of profile time in generators and coroutines
- **Regenerate the message data:** `mitebench.py gen`
