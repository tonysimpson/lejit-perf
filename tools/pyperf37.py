"""pyperformance's pure-Python benchmarks (tools/pyperformance/, vendored
from pyperformance 1.11.0, MIT) with and without LeJIT, on the Python that
runs this, a venv under build/venv/ (tools/lejitenv.py). A third run, plain
CPython with a hook installed that only passes frames on to CPython
(tools/passhook.c), compares LeJIT with CPython paying what any hook costs.

pyperformance itself won't run them on 3.7 (they declare 3.8+) and needs
pyperf; here a stand-in 'pyperf' module calls each benchmark's function
directly. The loop count is calibrated on CPython (about 0.2 s), then each
mode (CPython, CPython+hook, LeJIT) runs one warm-up and the best of five,
in a fresh process.

    python3 tools/pyperf37.py                       # table
    python3 tools/pyperf37.py -k richards           # names containing 'richards'
    python3 tools/pyperf37.py --python 3.7 --lejit ~/work/lejit-worktrees/x

--python and --lejit pick the Python and the LeJIT, which is installed into
that Python's venv first (tools/lejitenv.py: default 3.10 and main).
"""
import argparse
import glob
import os
import subprocess
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BMDIR = os.path.join(ROOT, 'tools', 'pyperformance')
TARGET = 0.2


def lejit_dir(argv=None):
    """the LeJIT repository, for tools that read its history or tests:
    --lejit DIR (taken out of argv, and passed on to
    child processes in $LEJIT), else $LEJIT, else ~/work/lejit"""
    if argv and '--lejit' in argv:
        i = argv.index('--lejit')
        os.environ['LEJIT'] = os.path.abspath(argv[i + 1])
        del argv[i:i + 2]
    return os.path.abspath(os.path.expanduser(
        os.environ.get('LEJIT') or '~/work/lejit'))


def start_lejit(d=None):
    """import LeJIT, the one installed or else from the checkout d, start it
    and return its extension module; an older checkout's ni starts on import"""
    if d:
        sys.path.insert(0, d)
        has_lejit = os.path.isdir(os.path.join(d, 'lejit'))
    else:
        try:
            import lejit  # noqa: F401
            has_lejit = True
        except ImportError:
            has_lejit = False
    if has_lejit:
        import lejit
        if lejit._core is None:
            raise SystemExit('no LeJIT built for this Python in %s' % (d or lejit.__file__))
        lejit.start()
        return lejit._core
    import ni
    if hasattr(ni, 'start'):
        ni.start()
        return ni._core
    return ni


def passhook_dir():
    import platform
    return os.path.join(ROOT, 'build', 'passhook', platform.python_version())


def build_passhook():
    """build tools/passhook.c for the Python that runs this, if it's missing
    or older than its source"""
    import ctypes
    import shlex
    import sysconfig
    src = os.path.join(ROOT, 'tools', 'passhook.c')
    so = os.path.join(passhook_dir(), 'passhook' + sysconfig.get_config_var('EXT_SUFFIX'))
    if os.path.exists(so) and os.path.getmtime(so) >= os.path.getmtime(src):
        return
    os.makedirs(passhook_dir(), exist_ok=True)
    define = []
    if hasattr(ctypes.pythonapi, '_PyInterpreterState_SetEvalFrameFunc'):
        define = ['-DHAVE_SET_EVAL_FRAME_FUNC']
    subprocess.run(shlex.split(sysconfig.get_config_var('CC')) + [
        '-O2', '-fPIC', '-shared', '-I', sysconfig.get_paths()['include']] + define +
        [src, '-o', so], check=True)


def start_passhook():
    """install the pass-through hook (built by build_passhook)"""
    sys.path.insert(0, passhook_dir())
    import passhook
    passhook.install()


class Runner:
    """the part of pyperf.Runner the benchmarks use"""

    def __init__(self, *a, **kw):
        self.metadata = {}
        self.argparser = argparse.ArgumentParser()
        self.benches = []

    def parse_args(self, args=None):
        return self.argparser.parse_args([])

    def bench_func(self, name, func, *args, **kw):
        def timed(loops):
            t = time.perf_counter()
            for _ in range(loops):
                func(*args)
            return time.perf_counter() - t
        self.benches.append((name, timed))

    def bench_time_func(self, name, func, *args, **kw):
        self.benches.append((name, lambda loops: func(loops, *args)))

    def bench_async_func(self, name, func, *args, **kw):
        import asyncio

        def timed(loops):
            t = time.perf_counter()
            for _ in range(loops):
                asyncio.get_event_loop().run_until_complete(func(*args))
            return time.perf_counter() - t
        self.benches.append((name, timed))


def fake_pyperf():
    m = types.ModuleType('pyperf')
    m.Runner = Runner
    m.perf_counter = time.perf_counter
    m.python_implementation = lambda: 'cpython'
    sys.modules['pyperf'] = m
    return m


def load(script):
    """run the script as __main__ with the stand-in; its Runner's benches"""
    made = []
    orig = Runner.__init__

    def init(self, *a, **kw):
        orig(self, *a, **kw)
        made.append(self)
    Runner.__init__ = init
    fake_pyperf()
    sys.argv = [script]
    code = compile(open(script).read(), script, 'exec')
    # a real module in sys.modules, as when the script is run, so its classes
    # are found in their module like any program's
    main = types.ModuleType('__main__')
    main.__file__ = script
    sys.modules['__main__'] = main
    exec(code, main.__dict__)
    return [b for r in made for b in r.benches]


def child(mode, script, loops):
    if mode == 'lejit':
        start_lejit()
    elif mode == 'hook':
        start_passhook()
    out = {}
    for name, timed in load(script):
        n = loops.get(name)
        if n is None:          # calibrate
            n = 1
            while timed(n) < TARGET / 4 and n < 1 << 20:
                n *= 2
            n = max(1, int(n * TARGET / max(timed(n), 1e-6)))
        timed(n)               # warm-up (and compilation)
        out[name] = (n, min(timed(n) for _ in range(5)))
    print('RESULT ' + repr(out))


def run_child(mode, script, loops):
    p = subprocess.run([sys.executable, __file__, '--child', mode, script,
                        repr(loops)], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True)
    lines = [l for l in p.stdout.splitlines() if l.startswith('RESULT ')]
    if p.returncode or not lines:
        return None, (p.stderr.strip().splitlines() or ['?'])[-1]
    return eval(lines[-1][7:]), None


def main():
    import lejitenv
    lejitenv.prepare(sys.argv)
    scripts = sorted(glob.glob(os.path.join(BMDIR, 'bm_*.py')))
    if '-k' in sys.argv:
        k = sys.argv[sys.argv.index('-k') + 1]
        scripts = [s for s in scripts if k in os.path.basename(s)]
    build_passhook()
    print('%-26s %9s %12s %9s %8s %8s' % ('', 'CPython', 'CPython+hook', 'LeJIT',
                                          'speed-up', 'vs +hook'))
    ratios, hook_ratios = [], []
    for s in scripts:
        plain, err = run_child('plain', s, {})
        if plain is None:
            print('%-26s CPython failed: %s' % (os.path.basename(s), err))
            continue
        loops = {k: v[0] for k, v in plain.items()}
        hook_res, hook_err = run_child('hook', s, loops)
        jit_res, err = run_child('lejit', s, loops)
        for name, (n, tp) in sorted(plain.items()):
            if hook_res is None:
                print('%-26s %8.3fs   CPython+hook failed: %s' % (name, tp, hook_err[:60]))
                continue
            if jit_res is None:
                print('%-26s %8.3fs   LeJIT failed: %s' % (name, tp, err[:60]))
                continue
            (nh, th), (nn, tn) = hook_res[name], jit_res[name]
            if nn != n or nh != n:
                print('%-26s loop counts differ: %d, %d, %d' % (name, n, nh, nn))
                continue
            ratios.append(tp / tn)
            hook_ratios.append(th / tn)
            print('%-26s %8.3fs %11.3fs %8.3fs %7.2fx %7.2fx %6d'
                  % (name, tp, th, tn, tp / tn, th / tn, n))
    if ratios:
        print('%-26s %40.2fx %7.2fx  (%d benchmarks)'
              % ('geometric mean', geomean(ratios), geomean(hook_ratios), len(ratios)))


def geomean(xs):
    g = 1.0
    for x in xs:
        g *= x
    return g ** (1 / len(xs))

if __name__ == '__main__':
    if sys.argv[1:2] == ['--child']:
        child(sys.argv[2], sys.argv[3], eval(sys.argv[4]))
    else:
        main()
