"""Instruction counts (perf stat -e instructions:u) of compiling and of warm
code, for comparing two builds without wall time:

  cold     one fresh process per pyperformance benchmark: start, import and
           start LeJIT, load the benchmark, one iteration of each of its
           functions
  warm     per iteration, warm: (3 iterations then N) minus (3 alone), / N;
           N is -n, raised for a bench whose N iterations come to under
           WARM_LOOP instructions

    python3.10 tools/instrcount.py cold [-k NAME] [--plain]
    python3.10 tools/instrcount.py warm [-k NAME] [-b BENCH | --each] [-n 20] [-r 1]

-k may be repeated (names containing any of them); -b (repeatable) runs
only the named benches of a script that has several (deepcopy_memo,
scimark_fft), each counted on its own row; --each gives every bench of
every script its own row, named as the bench (create_gc_cycles, not
gc_collect); -r repeats each count and
prints every repeat. PYTHONHASHSEED=0, address-space randomisation off
(setarch -R) and every process under a 4 GB address-space limit. Counts
the Python that runs this, with the LeJIT checkout given by --lejit DIR,
else $LEJIT, else ~/work/lejit, built for release in place for that Python.
--callgrind counts under valgrind's callgrind instead of perf, for machines
without hardware counters; its counts are not comparable with perf's.
"""
import glob
import os
import resource
import shutil
import subprocess
import sys
import tempfile

TOOLS = os.path.dirname(os.path.abspath(__file__))
MEMORY_LIMIT = 4000000000
# Address-space randomisation moves one process's count by up to 10 M
# instructions (deepcopy_reduce on 3.10), more than a small bench's warm loop
# of 10 iterations, so such a warm count could come out below zero; with it
# off, by a few thousand. A warm loop of at least WARM_LOOP instructions makes
# those negligible, and spreads what is still one-off after the first three
# iterations (deepcopy_reduce: about 190 K, 0.2% of the loop) thinly.
WARM_LOOP = 100000000
MAX_ITERATIONS = 100000
CALLGRIND = False
SIGNAL_TEXTS = (': Aborted', ': Segmentation fault', ': Killed', ': Bus error',
                ': Illegal instruction', ': Floating point exception',
                ': Trace/breakpoint trap')


def limit():
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))


def count(args, timeout=900):
    """instructions:u of running sys.executable with args"""
    if not shutil.which('setarch'):
        raise SystemExit('instrcount needs setarch (util-linux) to turn off randomisation')
    if CALLGRIND:
        return count_callgrind(args, timeout * 20)
    with tempfile.NamedTemporaryFile('r', suffix='.perf') as out:
        p = subprocess.run(['perf', 'stat', '-x,', '-e', 'instructions:u', '-o', out.name,
                            '--', 'setarch', os.uname().machine, '-R', sys.executable] + args,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout,
                           env=dict(os.environ, PYTHONHASHSEED='0'), preexec_fn=limit,
                           universal_newlines=True)
        # perf stat exits 0 when the child dies of a signal, and says so
        died = [l for l in p.stderr.splitlines()
                if l.endswith(SIGNAL_TEXTS) or 'Fatal Python error' in l]
        if p.returncode or died:
            raise RuntimeError('%s: %s' % (args, p.stderr.strip()[-300:]))
        for line in out.read().splitlines():
            f = line.split(',')
            if len(f) > 2 and f[2].startswith('instructions'):
                return int(f[0])
    raise RuntimeError('no count for %s' % args)


def count_callgrind(args, timeout):
    """instructions of running sys.executable with args, under callgrind"""
    with tempfile.NamedTemporaryFile('r', suffix='.callgrind') as out:
        p = subprocess.run(['setarch', os.uname().machine, '-R', 'valgrind', '--tool=callgrind',
                            '--smc-check=all-non-file', '--callgrind-out-file=' + out.name,
                            sys.executable] + args,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout,
                           env=dict(os.environ, PYTHONHASHSEED='0'), universal_newlines=True)
        if p.returncode or 'Fatal Python error' in p.stderr:
            raise RuntimeError('%s: %s' % (args, p.stderr.strip()[-300:]))
        for line in out.read().splitlines():
            if line.startswith(('summary:', 'totals:')):
                return int(line.split()[1])
    raise RuntimeError('no count for %s' % args)


def child_bench(mode, script, first, more, only=''):
    sys.path.insert(0, TOOLS)
    import pyperf37
    if mode == 'lejit':
        pyperf37.start_lejit()
    for name, timed in pyperf37.load(script):
        if only and name != only:
            continue
        timed(first)
        if more:
            timed(more)


def child_names(script):
    sys.path.insert(0, TOOLS)
    import pyperf37
    names = [name for name, _ in pyperf37.load(script)]
    sys.stdout.write('\n'.join(names) + '\n')


def bench_names(script):
    """the benches a script registers, read in a plain child"""
    p = subprocess.run([sys.executable, os.path.abspath(__file__), '--names', script],
                       stdout=subprocess.PIPE, universal_newlines=True, check=True,
                       env=dict(os.environ, PYTHONHASHSEED='0'), preexec_fn=limit)
    return p.stdout.split()


def warm(me, mode, p, n, b, reps):
    """warm counts per iteration, reps of them, over at least n iterations"""
    base = count([me, '--bench', mode, p, '3', '0', b])
    loop = count([me, '--bench', mode, p, '3', str(n), b]) - base
    if loop < WARM_LOOP:
        n = min(MAX_ITERATIONS, -(-WARM_LOOP * n // max(loop, n)))
        loop = count([me, '--bench', mode, p, '3', str(n), b]) - base
    cs = [loop // n]
    for _ in range(reps - 1):
        cs.append((count([me, '--bench', mode, p, '3', str(n), b]) -
                   count([me, '--bench', mode, p, '3', '0', b])) // n)
    return cs


def selected(paths, keys):
    if not keys:
        return paths
    return [p for p in paths if any(k in os.path.basename(p) for k in keys)]


def name_of(path):
    return os.path.splitext(os.path.basename(path))[0].replace('bm_', '')


def main(argv):
    what = argv[0]
    keys = [argv[i + 1] for i, a in enumerate(argv) if a == '-k']
    n = int(argv[argv.index('-n') + 1]) if '-n' in argv else 20
    reps = int(argv[argv.index('-r') + 1]) if '-r' in argv else 1
    benches = [argv[i + 1] for i, a in enumerate(argv) if a == '-b']
    mode = 'plain' if '--plain' in argv else 'lejit'
    me = os.path.abspath(__file__)
    total = 0
    paths = selected(sorted(glob.glob(os.path.join(TOOLS, 'pyperformance', 'bm_*.py'))), keys)
    each = '--each' in argv
    for p, b in [(p, b) for p in paths
                 for b in (bench_names(p) if each else benches or [''])]:
        if what == 'cold':
            cs = [count([me, '--bench', mode, p, '1', '0', b]) for _ in range(reps)]
        else:
            cs = warm(me, mode, p, n, b, reps)
        total += cs[0]
        print('%-24s %s' % (b or name_of(p), ' '.join('%15d' % c for c in cs)), flush=True)
    print('%-24s %15d' % ('total', total))


if __name__ == '__main__':
    sys.path.insert(0, TOOLS)
    import pyperf37
    pyperf37.lejit_dir(sys.argv)
    if sys.argv[1] == '--bench':
        child_bench(sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5]), *sys.argv[6:])
    elif sys.argv[1] == '--names':
        child_names(sys.argv[2])
    else:
        if '--callgrind' in sys.argv:
            sys.argv.remove('--callgrind')
            CALLGRIND = True
        main(sys.argv[1:])
