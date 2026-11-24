"""The Python and the LeJIT a tool measures: one venv per CPython version
under build/venv/, with LeJIT installed into it by pip.

    --python X.Y | X.Y.Z | pythonX.Y | PATH
                                  pyenv's newest installed X.Y (or that
                                  exact version), or that interpreter;
                                  default 3.10
    --lejit REF | DIR             a GitHub ref of tonysimpson/lejit (branch,
                                  tag or full SHA), or a local checkout;
                                  default $LEJIT, else main

A local directory is copied first (a git checkout: its files as git sees
them), so nothing is built inside it. The build is a release build (LEJIT_DEV_ and LE_ settings are left out,
except LEJIT_DEV_FALLBACK: set it to skip to measure a CPython LeJIT
doesn't support yet, such as 3.11).

    python3 tools/lejitenv.py venv [--python V]             # print its python
    python3 tools/lejitenv.py install [--python V] [--lejit REF]
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENVS = os.path.join(ROOT, 'build', 'venv')
LEJIT_URL = 'git+https://github.com/tonysimpson/lejit@'
# the versions setup.sh installs, as CI and the history use them
DEFAULT_VERSIONS = ['3.7.17', '3.8.20', '3.9.21', '3.10.16', '3.11.11', '3.12.9', '3.13.2', '3.14.10']
DEFAULT_PYTHON = '3.10'


def run(args, **kw):
    return subprocess.run(args, check=True, universal_newlines=True, **kw)


def output(args):
    return run(args, stdout=subprocess.PIPE).stdout.strip()


def pyenv_root():
    try:
        return output(['pyenv', 'root'])
    except (OSError, subprocess.CalledProcessError):
        return os.path.expanduser('~/.pyenv')


def version_key(v):
    return [int(x) if x.isdigit() else -1 for x in v.split('.')]


def find_python(spec):
    """the interpreter for --python: a path, or pyenv's newest installed
    match of X.Y or X.Y.Z"""
    if os.sep in spec:
        if not os.access(spec, os.X_OK):
            raise SystemExit('no Python at %s' % spec)
        return os.path.abspath(spec)
    if spec.startswith('python'):
        spec = spec[len('python'):]
    if not spec.replace('.', '').isdigit():
        raise SystemExit('--python takes a version (3.10 or 3.10.16), '
                         'pythonX.Y, or a path')
    xy = '.'.join(spec.split('.')[:2])
    found = [d for d in glob.glob(os.path.join(pyenv_root(), 'versions', spec + '*'))
             if os.path.basename(d) == spec or os.path.basename(d).startswith(spec + '.')]
    found = [d for d in found if os.path.basename(d).replace('.', '').isdigit()
             and os.access(os.path.join(d, 'bin', 'python' + xy), os.X_OK)]
    if not found:
        want = spec if spec.count('.') == 2 else next(
            (v for v in DEFAULT_VERSIONS if v.startswith(spec + '.')), None)
        if want is None:
            try:
                want = output(['pyenv', 'latest', '--known', spec])
            except (OSError, subprocess.CalledProcessError):
                want = spec
        raise SystemExit('CPython %s is not installed in pyenv; install it with:\n'
                         '    pyenv install %s' % (spec, want))
    d = max(found, key=lambda d: version_key(os.path.basename(d)))
    return os.path.join(d, 'bin', 'python' + xy)


def venv_python(base):
    """the venv for the interpreter base, made if missing, with what a
    LeJIT build needs"""
    full = output([base, '-c', 'import platform; print(platform.python_version())'])
    d = os.path.join(VENVS, full)
    py = os.path.join(d, 'bin', 'python')
    cfg = os.path.join(d, 'pyvenv.cfg')
    if os.path.exists(cfg):
        home = [l.split('=', 1)[1].strip() for l in open(cfg) if l.startswith('home')]
        if not home or os.path.realpath(home[0]) != os.path.dirname(os.path.realpath(base)):
            shutil.rmtree(d)               # made from another interpreter
    if not os.path.exists(py):
        print('making %s' % d, file=sys.stderr, flush=True)
        run([base, '-m', 'venv', d])
        run([py, '-m', 'pip', 'install', '-q', '--disable-pip-version-check',
             'setuptools>=61', 'wheel'])
    return py


def install(py, ref):
    """pip install LeJIT from ref (a GitHub ref or a local directory) into
    the venv whose python is py"""
    # a release build, but LEJIT_DEV_FALLBACK still chooses whether a CPython
    # LeJIT doesn't yet support gets the real thing
    env = {k: v for k, v in os.environ.items()
           if k == 'LEJIT_DEV_FALLBACK' or not k.startswith(('LEJIT_DEV_', 'LE_'))}
    pip = [py, '-m', 'pip', 'install', '-q', '--disable-pip-version-check', '--no-deps',
           '--force-reinstall', '--no-build-isolation', '--no-cache-dir']
    print('installing LeJIT %s into %s' % (ref, os.path.dirname(os.path.dirname(py))),
          file=sys.stderr, flush=True)
    if not os.path.isdir(ref):
        run(pip + [LEJIT_URL + ref], env=env, stdout=sys.stderr)
        return
    with tempfile.TemporaryDirectory(prefix='lejit-') as tmp:
        src = os.path.join(tmp, 'src')
        top = subprocess.run(['git', '-C', ref, 'rev-parse', '--show-toplevel'],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             universal_newlines=True).stdout.strip()
        if top and os.path.realpath(top) == os.path.realpath(ref):
            # a checkout: its files as git sees them, uncommitted changes
            # included, without what builds leave in it
            files = output(['git', '-C', ref, 'ls-files', '-co', '--exclude-standard', '-z'])
            for f in files.split('\0'):
                if f and os.path.isfile(os.path.join(ref, f)):
                    os.makedirs(os.path.dirname(os.path.join(src, f)), exist_ok=True)
                    shutil.copy2(os.path.join(ref, f), os.path.join(src, f))
        else:
            shutil.copytree(ref, src)
        run(pip + [src], env=env, stdout=sys.stderr)


def take(argv, flag):
    """the value of flag in argv, taken out of it, or None"""
    if flag not in argv:
        return None
    i = argv.index(flag)
    value = argv[i + 1]
    del argv[i:i + 2]
    return value


def ours():
    """is this Python one of the venvs here?"""
    return os.path.dirname(os.path.realpath(sys.prefix)) == os.path.realpath(VENVS)


def prepare(argv):
    """take --python and --lejit out of argv; unless this already runs in a
    venv here with neither given, install LeJIT into the venv for --python
    and run this tool again in it"""
    spec, ref = take(argv, '--python'), take(argv, '--lejit')
    if ours() and spec is None and ref is None:
        return
    py = venv_python(find_python(spec or DEFAULT_PYTHON))
    install(py, ref or os.environ.get('LEJIT') or 'main')
    sys.stdout.flush()
    os.execv(py, [py] + argv)


if __name__ == '__main__':
    args = sys.argv[1:]
    what = args.pop(0) if args else ''
    spec, ref = take(args, '--python'), take(args, '--lejit')
    if what not in ('venv', 'install') or args:
        raise SystemExit(__doc__)
    py = venv_python(find_python(spec or DEFAULT_PYTHON))
    if what == 'install':
        install(py, ref or os.environ.get('LEJIT') or 'main')
    print(py)
