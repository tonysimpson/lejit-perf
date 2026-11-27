"""Instruction counts of the extended set of pyperformance benchmarks
(tools/pyperformance_extended/, listed in its benchmarks.py): the rest of
pyperformance 1.14.0 that runs in-process, counted as tools/instrcount.py
counts the core set (the 23 in tools/pyperformance/), by its functions.

    python3 tools/pyperfext.py cold|warm [--set extended|all] [-k NAME]
                               [-b BENCH | --each] [-n 20] [-r 1] [--plain]
                               [--python V] [--lejit REF]

--set all runs tools/instrcount.py's rows and total for the core set first,
as it prints them, then the extended rows and "total extended". The other
options are tools/instrcount.py's; -k matches benchmark names. A benchmark
this Python can't run gets a row saying why: its script doesn't compile, a
module or attribute in its needs is missing, its packages don't install, or
it fails under plain CPython. One that fails under the mode counted gets a
row saying how, and the rest go on.

Third-party packages are installed with pip, pinned as pyperformance pins
them, one directory per script and CPython version under build/pydeps/, put
on sys.path only for that benchmark; a directory is made again when its pins
change. tools/instrcount.py, tools/pyperf37.py and tools/pyperformance/ are
untouched, so the core counts and the history's tool name are too.
"""
import asyncio
import importlib
import os
import platform
import shutil
import subprocess
import sys
import time
import types

TOOLS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS)
EXTENDED_DIR = os.path.join(TOOLS, 'pyperformance_extended')
PYDEPS = os.path.join(ROOT, 'build', 'pydeps')
SETS = ('core', 'extended', 'all')
sys.path.insert(0, TOOLS)
import instrcount  # noqa: E402
import pyperf37  # noqa: E402


class Bench:
    def __init__(self, name, script, options=(), needs=(), extended=False):
        self.name, self.script = name, script
        self.options, self.needs, self.extended = list(options), list(needs), extended


def manifest():
    m = {}
    path = os.path.join(EXTENDED_DIR, 'benchmarks.py')
    exec(compile(open(path).read(), path, 'exec'), m)
    return m


def extended():
    return [Bench(name, os.path.join(EXTENDED_DIR, 'bm_%s.py' % script), options, needs, True)
            for name, script, options, needs in manifest()['BENCHMARKS']]


def find(name):
    for b in extended():
        if b.name == name:
            return b
    raise SystemExit('no extended benchmark %s' % name)


def stem(b):
    return os.path.basename(b.script)[3:-3]


def deps_dir(b):
    """where b's packages are for this CPython, or None if it has none"""
    if stem(b) not in manifest()['REQUIREMENTS']:
        return None
    return os.path.join(PYDEPS, platform.python_version(), stem(b))


def install_deps(benches):
    """pip install the pinned packages of each of benches that has any and
    whose directory doesn't have them yet; a failed install is kept (and
    named by missing) until the pins change"""
    reqs = manifest()['REQUIREMENTS']
    for s in sorted({stem(b) for b in benches if b.extended and stem(b) in reqs}):
        d = os.path.join(PYDEPS, platform.python_version(), s)
        want = ' '.join(reqs[s])
        marker = os.path.join(d, '.pins')
        if os.path.exists(marker) and open(marker).readline().strip() == want:
            continue
        if os.path.isdir(d):
            shutil.rmtree(d)
        print('installing %s for %s' % (want, s), file=sys.stderr, flush=True)
        p = subprocess.run([sys.executable, '-m', 'pip', 'install', '-q',
                            '--disable-pip-version-check', '--no-cache-dir', '--target', d]
                           + reqs[s], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True)
        os.makedirs(d, exist_ok=True)
        with open(marker, 'w') as f:
            f.write(want + '\n')
            if p.returncode:
                lines = [l for l in p.stdout.splitlines() if l.strip()]
                f.write('failed: %s\n' % (lines[-1] if lines else 'pip failed'))


def use_deps(b):
    d = deps_dir(b)
    if d:
        sys.path.insert(0, d)


def missing(b):
    """why this Python can't run b, or None"""
    try:
        compile(open(b.script).read(), b.script, 'exec')
    except SyntaxError as e:
        return 'syntax (%s, line %s)' % (e.msg, e.lineno)
    d = deps_dir(b)
    if d:
        lines = open(os.path.join(d, '.pins')).read().splitlines()
        if len(lines) > 1:
            return "packages don't install: " + lines[1][len('failed: '):][:80]
        use_deps(b)
    for need in b.needs:
        module, _, attr = need.partition('.')
        try:
            m = importlib.import_module(module)
        except Exception as e:
            return 'no %s (%s: %s)' % (module, type(e).__name__, str(e)[:60])
        if attr and not hasattr(m, attr):
            return 'no %s' % need
    return None


class Runner(pyperf37.Runner):
    """the stand-in with the benchmark's options, each bench measured (by
    load's run) when the script declares it, as pyperf does, so a script
    may clean up after it; bench_async_func as pyperf runs it: a new event
    loop per timing, the iterations in one coroutine"""
    options = []
    run = None

    def parse_args(self, args=None):
        return self.argparser.parse_args(self.options)

    def bench_func(self, name, *a, **kw):
        pyperf37.Runner.bench_func(self, name, *a, **kw)
        self.declared()

    def bench_time_func(self, name, *a, **kw):
        pyperf37.Runner.bench_time_func(self, name, *a, **kw)
        self.declared()

    def bench_async_func(self, name, func, *args, **kw):
        def timed(loops):
            async def main():
                t = time.perf_counter()
                for _ in range(loops):
                    await func(*args)
                return time.perf_counter() - t
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(main())
            finally:
                asyncio.set_event_loop(None)
                loop.close()
        self.benches.append((name, timed))
        self.declared()

    def declared(self):
        if self.run:
            self.run(*self.benches[-1])


def load(b, run=None):
    """run b's script as __main__ with its options, calling run(name, timed)
    for each bench as the script declares it; its benches"""
    use_deps(b)
    # for the worker processes that import the script again
    sys.path.append(os.path.join(EXTENDED_DIR, 'pyperf_stub'))
    made = []
    Runner.options, Runner.run = b.options, staticmethod(run) if run else None

    class Made(Runner):
        def __init__(self, *a, **kw):
            Runner.__init__(self, *a, **kw)
            made.append(self)
    m = pyperf37.fake_pyperf()
    m.Runner = Made
    sys.argv = [b.script] + b.options
    code = compile(open(b.script).read(), b.script, 'exec')
    main = types.ModuleType('__main__')
    main.__file__ = b.script
    sys.modules['__main__'] = main
    exec(code, main.__dict__)
    return [x for r in made for x in r.benches]


def child_bench(mode, name, first, more, only):
    """the counted process: start LeJIT unless plain, then each bench (or
    only the one named) for first iterations, then more"""
    bench = find(name)
    if mode == 'lejit':
        pyperf37.start_lejit()

    def run(bench_name, timed):
        if only and bench_name != only:
            return
        timed(first)
        if more:
            timed(more)
    load(bench, run)


def child_names(name):
    """the benches an extended benchmark declares, or why it is skipped"""
    bench = find(name)
    reason = missing(bench)
    if reason:
        sys.stdout.write('skipped: %s\n' % reason)
    else:
        sys.stdout.write('\n'.join(n for n, _ in load(bench)) + '\n')


def names(bench):
    """child_names in a plain child; a benchmark that fails there is
    skipped too"""
    p = subprocess.run([sys.executable, os.path.abspath(__file__), '--names', bench.name],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                       env=dict(os.environ, PYTHONHASHSEED='0'), preexec_fn=instrcount.limit)
    if p.returncode:
        return ['skipped: fails under plain CPython: %s'
                % (p.stderr.strip().splitlines() or ['?'])[-1][-100:]]
    return p.stdout.splitlines()


def failed(me, mode, b, only):
    """what a failed count's row says: skipped when plain CPython fails
    too, else that LeJIT failed"""
    if mode == 'lejit':
        try:
            instrcount.count([me, '--bench', 'plain', b.name, '1', '0', only])
            return 'failed under LeJIT'
        except RuntimeError:
            pass
    return 'skipped: fails under plain CPython'


def main(argv):
    what = argv[0]
    which = argv[argv.index('--set') + 1] if '--set' in argv else 'extended'
    if which not in ('extended', 'all'):
        sys.exit('--set takes extended or all')
    if which == 'all':
        instrcount.main([a for i, a in enumerate(argv)
                         if a != '--set' and argv[i - 1] != '--set'])
    keys = [argv[i + 1] for i, a in enumerate(argv) if a == '-k']
    n = int(argv[argv.index('-n') + 1]) if '-n' in argv else 20
    reps = int(argv[argv.index('-r') + 1]) if '-r' in argv else 1
    benches = [argv[i + 1] for i, a in enumerate(argv) if a == '-b']
    mode = 'plain' if '--plain' in argv else 'lejit'
    each = '--each' in argv
    me = os.path.abspath(__file__)
    chosen = [b for b in extended() if not keys or any(k in b.name for k in keys)]
    install_deps(chosen)
    total = 0
    for b in chosen:
        declared = names(b)
        if declared[:1] and declared[0].startswith('skipped: '):
            print('%-24s %s' % (b.name, declared[0]), flush=True)
            continue
        # with --each, a row per bench the script declares, if it has several
        for only in (declared if each and len(declared) > 1 else benches or ['']):
            try:
                if what == 'cold':
                    cs = [instrcount.count([me, '--bench', mode, b.name, '1', '0', only])
                          for _ in range(reps)]
                else:
                    cs = instrcount.warm(me, mode, b.name, n, only, reps)
            except RuntimeError as e:
                print('%-24s %s: %s' % (only or b.name, failed(me, mode, b, only),
                                        str(e).strip().splitlines()[-1][-120:]), flush=True)
                continue
            total += cs[0]
            print('%-24s %s' % (only or b.name, ' '.join('%15d' % c for c in cs)), flush=True)
    print('%-24s %15d' % ('total extended' if which == 'all' else 'total', total))


if __name__ == '__main__':
    if sys.argv[1:2] == ['--bench']:
        child_bench(sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5]), sys.argv[6])
    elif sys.argv[1:2] == ['--names']:
        child_names(sys.argv[2])
    elif sys.argv[1:2] in (['cold'], ['warm']):
        import lejitenv
        lejitenv.prepare(sys.argv)
        main(sys.argv[1:])
    else:
        sys.exit(__doc__)
