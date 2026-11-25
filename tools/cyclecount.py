"""Cycles and cache misses of warm pyperformance code, base against new:
what instruction counts miss (microcoded instructions, latency chains, code
layout, cache misses).

    python3 tools/cyclecount.py --lejit NEW [--base main] [--python V]
                                [-k NAME] [-r 3]

Installs both LeJITs (GitHub refs or local checkouts, as tools/lejitenv.py
takes them) as release builds into two directories of the same length under
build/cycles/, and runs both from the same venv Python with only PYTHONPATH
telling them apart, so the processes are laid out alike (PYTHONHASHSEED=0,
setarch -R, as tools/instrcount.py). For a CPython LeJIT doesn't support
yet, such as CPython main, set LEJIT_DEV_FALLBACK=skip (tools/lejitenv.py).

Each process is pinned to the less busy thread of the least busy core and
runs each benchmark 3 times to warm it, then in chunks of about CHUNK
instructions (at least one iteration), counting only inside the chunks
(perf_event_open on its own thread, user mode), so start-up and compiling
drop out.

Other jobs on the machine change the counts mostly through the core's other
SMT thread: whenever it runs anything (even a pause loop) Zen 5 splits the
core between the two, and richards' warm iteration takes about 55% more
cycles. So every chunk also counts de_no_dispatch_per_slot.smt_contention,
and only quiet chunks are used, those that lost under QUIET of their
dispatch slots to the other thread. Each count is the median over a
group's quiet chunks; a group takes more chunks, up to MAX_CHUNKS, until it
has QUIET_CHUNKS of them, and a process that still lacks them is run again,
up to RETRIES times, then counted from its least contended chunks and
named in the report. -r rounds (default 3) each run one process per build,
alternating which goes first.

The counters, Zen 5 encodings. Zen 5 has six and the NMI watchdog takes
one, so there are three groups of at most five, enabled in turn, chunk by
chunk, each with cycles and the SMT count; nothing is multiplexed, and a
count the kernel multiplexed anyway is marked "scaled".

  cycles        cycles
  instructions  instructions (median over all chunks)
  branch-miss   branch-misses
  L1d-miss      L1-dcache-load-misses (raw 0xc860): demand requests from
                the L1 data cache to L2
  L1i-miss      ic_cache_fill_l2 + ic_cache_fill_sys (raw 0x82, 0x83):
                instruction cache lines filled; a loop through 256 KB of
                code fills 4096 lines per time round, as it should (the
                generic L1-icache-load-misses is raw 0x81 on AMD, which Zen
                5 doesn't document: it counted 128 there)
  L2-miss       cache-misses: on AMD, requests that missed L2
                (instruction and data), not last-level misses
  dram-fill     ls_any_fills_from_sys.dram_io_all (raw 0x4844): data cache
                fills from DRAM, i.e. data that missed L3 too; L3 misses as
                such are counted only by the amd_l3 PMU, for the whole
                machine

Noise: each build's spread over the rounds, (max - min) / median. A change
is marked real (*) only when every round moves the same way, no round of
one build falls within the other's range, and the median change is at least
0.1% and beyond both builds' spread, on a count of at least SMALL per
iteration. The load average is
printed at the start and end.
"""
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

TOOLS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS)
sys.path.insert(0, TOOLS)
import instrcount  # noqa: E402
import lejitenv  # noqa: E402

HARDWARE, RAW = 0, 4
CYCLES = ('cycles', HARDWARE, 0)
# dispatch slots this thread lost because the other SMT thread had the cycle
SMT_LOST = ('smt-lost', RAW, 0x1000060a0)
DISPATCH_WIDTH = 8
GROUPS = [
    [CYCLES, ('instructions', HARDWARE, 1), ('branch-miss', HARDWARE, 5), SMT_LOST],
    [CYCLES, ('L1d-miss', RAW, 0xc860), ('ic_cache_fill_l2', RAW, 0x82),
     ('ic_cache_fill_sys', RAW, 0x83), SMT_LOST],
    [CYCLES, ('L2-miss', HARDWARE, 3), ('dram-fill', RAW, 0x4844), SMT_LOST],
]
# what is reported, each the sum of these counts
METRICS = [
    ('cycles', ['cycles']),
    ('instructions', ['instructions']),
    ('branch-miss', ['branch-miss']),
    ('L1d-miss', ['L1d-miss']),
    ('L1i-miss', ['ic_cache_fill_l2', 'ic_cache_fill_sys']),
    ('L2-miss', ['L2-miss']),
    ('dram-fill', ['dram-fill']),
]
CHUNK = 100000000         # instructions in a chunk, at least one iteration
QUIET = 0.005             # a chunk is quiet below this share of slots lost to SMT
QUIET_CHUNKS = 3          # per group, wanted
MAX_CHUNKS = 100          # per group
BUDGET = 10000000000      # instructions per group, at most (but 3 chunks)
RETRIES = 5
SMALL = 1000              # per iteration: a count this small is never marked real


class Counters:
    """groups of counters on this thread, user mode, read with ctypes"""

    def __init__(self, groups):
        import ctypes
        import struct
        self.ctypes, self.struct = ctypes, struct
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.groups = []
        for group in groups:
            fds = []
            for i, (_, kind, config) in enumerate(group):
                # perf_event_attr: type, size, config, ..., read_format,
                # flags (disabled for the leader, exclude_kernel, exclude_hv)
                flags = (1 if i == 0 else 0) | 1 << 5 | 1 << 6
                attr = struct.pack('IIQQQQQ', kind, 112, config, 0, 0, 1 | 2 | 8, flags)
                attr = ctypes.create_string_buffer(attr + bytes(112 - len(attr)), 112)
                fd = self.libc.syscall(298, attr, 0, -1, fds[0] if fds else -1, 8)
                if fd < 0:
                    raise OSError(ctypes.get_errno(), 'perf_event_open %s' % group[i][0])
                fds.append(fd)
            self.groups.append((group, fds))

    def run(self, g, work):
        """{name: count} of group g over work(), and whether it was scaled"""
        group, fds = self.groups[g]
        self.libc.ioctl(fds[0], 0x2403, 1)      # reset, the whole group
        self.libc.ioctl(fds[0], 0x2400, 1)      # enable
        work()
        self.libc.ioctl(fds[0], 0x2401, 1)      # disable
        data = os.read(fds[0], 8 * (3 + len(group)))
        n, enabled, running = self.struct.unpack_from('QQQ', data)
        values = self.struct.unpack_from('%dQ' % n, data, 24)
        return {name: v for (name, _, _), v in zip(group, values)}, running < enabled


def contention(c):
    return c['smt-lost'] / (DISPATCH_WIDTH * c['cycles']) if c['cycles'] else 0


def child(script):
    """per bench of script, per group: each chunk's counts per iteration,
    as JSON; a group takes chunks in turn with the others until it has
    QUIET_CHUNKS quiet ones or MAX_CHUNKS in all"""
    import pyperf37
    pyperf37.start_lejit()
    counters = Counters(GROUPS)
    out = []
    for name, timed in pyperf37.load(script):
        timed(3)
        one, _ = counters.run(0, lambda: timed(1))
        each = max(1, one['instructions'])
        n = max(1, -(-CHUNK // each))
        most = max(QUIET_CHUNKS, min(MAX_CHUNKS, BUDGET // (n * each)))
        chunks = [[] for _ in GROUPS]
        scaled = False
        while True:
            todo = [g for g, cs in enumerate(chunks) if len(cs) < most and
                    sum(contention(c) < QUIET for c in cs) < QUIET_CHUNKS]
            if not todo:
                break
            for g in todo:
                got, s = counters.run(g, lambda: timed(n))
                scaled = scaled or s
                chunks[g].append({e: v / n for e, v in got.items()})
        out.append({'bench': name, 'iterations': n, 'chunks': chunks, 'scaled': scaled})
    sys.stdout.write(json.dumps(out))


def cpu_times():
    with open('/proc/stat') as f:
        return {int(l.split()[0][3:]): [int(x) for x in l.split()[1:]]
                for l in f if l.startswith('cpu') and l[3].isdigit()}


def quietest_cpu():
    """the less busy thread of the least busy core, over a quarter second"""
    before = cpu_times()
    time.sleep(0.25)
    after = cpu_times()
    busy = {}
    for c in after:
        d = [a - b for a, b in zip(after[c], before[c])]
        busy[c] = sum(d) - d[3] - d[4]          # all but idle and iowait
    core = {}
    for c in busy:
        with open('/sys/devices/system/cpu/cpu%d/topology/core_id' % c) as f:
            core.setdefault((f.read().strip()), []).append(c)
    cpus = min(core.values(), key=lambda cs: sum(busy[c] for c in cs))
    return min(cpus, key=lambda c: busy[c])


def measure(py, path, script, timeout=3600):
    """one process: {count: per iteration} for script, summed over its
    benches, each the median of a group's quiet chunks (or its
    QUIET_CHUNKS least contended); whether any count was scaled; and
    whether a group had too few quiet chunks"""
    env = dict(os.environ, PYTHONHASHSEED='0', PYTHONPATH=path)
    cpu = quietest_cpu()
    p = subprocess.run(['taskset', '-c', str(cpu), 'setarch', os.uname().machine, '-R', py,
                        os.path.abspath(__file__), '--child', script],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                       env=env, preexec_fn=instrcount.limit, universal_newlines=True)
    if p.returncode:
        raise RuntimeError('%s: %s' % (script, p.stderr.strip()[-500:]))
    total, scaled, contended = {}, False, False
    for bench in json.loads(p.stdout):
        scaled = scaled or bench['scaled']
        for g, group in enumerate(GROUPS):
            cs = sorted(bench['chunks'][g], key=contention)
            quiet = [c for c in cs if contention(c) < QUIET]
            if len(quiet) < QUIET_CHUNKS:
                contended = True
                quiet = cs[:QUIET_CHUNKS]
            for name, _, _ in group:
                if name == 'smt-lost' or (name == 'cycles' and g):
                    continue
                # instructions don't depend on the sibling: every chunk
                use = cs if name == 'instructions' else quiet
                total[name] = total.get(name, 0) + statistics.median(c[name] for c in use)
    return total, scaled, contended


def loadavg():
    with open('/proc/loadavg') as f:
        return ' '.join(f.read().split()[:3])


def pct(new, base):
    return 100.0 * (new - base) / base if base else float('nan')


def spread(values):
    m = statistics.median(values)
    return 100.0 * (max(values) - min(values)) / abs(m) if m else float('nan')


def report(rows):
    """rows: [(name, {metric: ([base per round], [new per round])})]"""
    for metric, _ in METRICS:
        print('\n### %s\n' % metric)
        print('| benchmark | base | new | change | rounds | noise base/new |  |')
        print('|:--|--:|--:|--:|:--|--:|:-:|')
        for name, m in rows:
            base, new = m[metric]
            changes = [pct(n, b) for b, n in zip(base, new)]
            mb, mn = statistics.median(base), statistics.median(new)
            change = pct(mn, mb)
            sb, sn = spread(base), spread(new)
            apart = min(new) > max(base) or max(new) < min(base)
            real = (len(base) > 1 and apart and (all(c > 0 for c in changes) or
                                                 all(c < 0 for c in changes))
                    and abs(change) >= 0.1 and abs(change) > max(sb, sn)
                    and max(mb, mn) >= SMALL)
            print('| %s | %.0f | %.0f | %+.2f%% | %s | %.1f%% / %.1f%% | %s |' % (
                name, mb, mn, change, ' '.join('%+.1f' % c for c in changes), sb, sn,
                '*' if real else ''))


def main(argv):
    keys = [argv[i + 1] for i, a in enumerate(argv) if a == '-k']
    nrounds = int(argv[argv.index('-r') + 1]) if '-r' in argv else 3
    spec = lejitenv.take(argv, '--python')
    new_ref = lejitenv.take(argv, '--lejit')
    base_ref = lejitenv.take(argv, '--base') or 'main'
    if not new_ref:
        sys.exit(__doc__)
    if 'AuthenticAMD' not in open('/proc/cpuinfo').read():
        sys.exit('cyclecount uses AMD Zen 5 counters')
    py = lejitenv.venv_python(lejitenv.find_python(spec or lejitenv.DEFAULT_PYTHON))
    os.makedirs(os.path.join(ROOT, 'build', 'cycles'), exist_ok=True)
    work = tempfile.mkdtemp(prefix='run-', dir=os.path.join(ROOT, 'build', 'cycles'))
    try:
        # equal-length paths, so both builds' processes are laid out alike
        paths = {'base': os.path.join(work, 'a'), 'new': os.path.join(work, 'b')}
        lejitenv.install(py, base_ref, paths['base'])
        lejitenv.install(py, new_ref, paths['new'])
        version = subprocess.run([py, '-c', 'import platform; print(platform.python_version())'],
                                 stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()
        start = loadavg()
        scripts = instrcount.selected(sorted(
            os.path.join(TOOLS, 'pyperformance', f)
            for f in os.listdir(os.path.join(TOOLS, 'pyperformance'))
            if f.startswith('bm_') and f.endswith('.py')), keys)
        rows, scaled, contended = [], set(), set()
        for script in scripts:
            got = {'base': [], 'new': []}
            for r in range(nrounds):
                for b in (['base', 'new'] if r % 2 == 0 else ['new', 'base']):
                    # a process that found too few quiet chunks is run again
                    for _ in range(RETRIES):
                        counts, s, c = measure(py, paths[b], script)
                        if not c:
                            break
                    got[b].append(counts)
                    if s:
                        scaled.add(instrcount.name_of(script))
                    if c:
                        contended.add(instrcount.name_of(script))
            m = {metric: tuple([sum(g[e] for e in events) for g in got[b]] for b in ('base', 'new'))
                 for metric, events in METRICS}
            rows.append((instrcount.name_of(script), m))
            print('%s: cycles %s' % (rows[-1][0], ' '.join(
                '%+.1f%%' % pct(nw, bs) for bs, nw in zip(*m['cycles']))),
                file=sys.stderr, flush=True)
        rows.append(('total', {metric: tuple(
            [sum(m[metric][i][r] for _, m in rows) for r in range(nrounds)] for i in (0, 1))
            for metric, _ in METRICS}))
        print('# Cycles and cache misses, warm, per iteration\n')
        print('CPython %s; base %s, new %s; %d rounds; load average %s at the start, '
              '%s at the end (%d threads).' % (version, base_ref, new_ref, nrounds, start,
                                               loadavg(), os.cpu_count()))
        print('Scaled (multiplexed): %s.' % ', '.join(sorted(scaled)) if scaled else
              'No count was scaled.')
        if contended:
            print('Too few chunks free of SMT contention in %d tries (counted from the '
                  'least contended): %s.' % (RETRIES, ', '.join(sorted(contended))))
        print('"rounds" is each round\'s change; * marks a change beyond the noise.')
        report(rows)
    finally:
        shutil.rmtree(work)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--child']:
        child(sys.argv[2])
    else:
        main(sys.argv[1:])
