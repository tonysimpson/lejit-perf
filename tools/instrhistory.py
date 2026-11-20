"""Instruction counts of every merge to main, kept in bench/history/instrcount.jsonl.

    python3 tools/instrhistory.py record [COMMIT] [--python X.Y]... [--keep]
    python3 tools/instrhistory.py report [A [B]] [--python X.Y]...
    python3 tools/instrhistory.py graph [-o FILE]

record builds COMMIT (default HEAD) for release in a scratch tree,
build/instrhistory/<commit>, once per version that was supported at that
commit (its tools/versions.txt; 3.7 before there was one) and is supported
here, one version at a time, and counts each with this checkout's
tools/instrcount.py: warm (-n 10, raised for a short bench) and cold, every bench on its own row,
two repeats. It holds a heavy-job slot throughout. It adds one line per
(commit, version, bench, warm or cold); a commit already recorded with
the same tool is refused. The scratch tree is removed unless --keep, and
a tree already there is reused, with any module already built in it.

report compares two recorded commits (default the newest two of each
version), by a tool that measured both if there is one (the highest
method), else each commit's highest: instructions per bench, warm and cold, the totals and the
geometric mean of the warm ratios, with '!' on a warm change over 1% and
'~' on a count too spread to read. graph writes a static
HTML page (default build/instrhistory.html) of the history per version.

Each record: n (first-parent commits up to it, the sort key), commit (12
hex digits),
date (committer, UTC), tool (files- and a hash of the contents of
instrcount.py, pyperf37.py and the benchmarks as they are in this
checkout, then /m and METHOD; BY_COMMIT maps the commit-named tools of the
first records to this form), v and py (version and full version),
bench, kind (warm: per iteration; cold: per fresh process), count (the
smaller repeat), spread ((max - min) / min of the repeats) and noisy
(spread over 1%, or a bench in NOISY_BENCHES; method 1 also marked a
count under 1,000,000, its loop too short to read).
"""
import datetime
import glob
import hashlib
import html
import json
import math
import os
import resource
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY = os.path.join(ROOT, 'bench', 'history', 'instrcount.jsonl')
SCRATCH = os.path.join(ROOT, 'build', 'instrhistory')
# what the counts depend on: the counter, the harness and the benchmarks, and
# METHOD, raised when ITERATIONS, REPEATS or how a count is taken changes
TOOL_FILES = ['tools/instrcount.py', 'tools/pyperf37.py', 'tools/pyperformance']
METHOD = 2
MEMORY_LIMIT = 4000000000
ITERATIONS = 10
REPEATS = 2
NOISY_SPREAD = 0.01
# moved by heap state alone, the sign changing under the heap controls
# (PROFILE.md), though two repeats in one record agree
NOISY_BENCHES = {'hexiom', 'scimark_lu', 'scimark_sor'}
FLAG = 0.01


def git(*args, cwd=ROOT):
    return subprocess.run(['git', '-C', cwd] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, universal_newlines=True, check=True).stdout


def limit():
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))


def versions_table(text):
    """{version: (status, interpreter)} from a tools/versions.txt"""
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 3 and not line.startswith('#'):
            out[f[0]] = (f[1], f[2])
    return out


def interpreter(v, table):
    p = os.environ.get('NI_PY' + v.replace('.', '')) or table[v][1]
    return os.path.expanduser(p)


def files_hash(blobs):
    """a short hash of [(path, git blob id)], as git would list them"""
    h = hashlib.sha1()
    for path, blob in sorted(blobs):
        h.update(('%s %s\n' % (path, blob)).encode())
    return h.hexdigest()[:10]


def blob_id(path):
    data = open(path, 'rb').read()
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def tool_version():
    """the measuring files' contents as they are here, and METHOD"""
    paths = git('ls-files', '-co', '--exclude-standard', '-z', '--', *TOOL_FILES).split('\0')
    blobs = [(p, blob_id(os.path.join(ROOT, p))) for p in set(paths)
             if p and os.path.isfile(os.path.join(ROOT, p))]
    return 'files-%s/m%d' % (files_hash(blobs), METHOD)


# The first records named the tool by the measuring files' last commit. These
# are all the names they used, each checked to have the files of
# files-e60c3cbf78; ccf15ef is a pre-rebase commit that needn't stay in the
# repository, so the reader can't look it up.
BY_COMMIT = {'ccf15ef/m1': 'files-e60c3cbf78/m1', 'a230bae/m1': 'files-e60c3cbf78/m1'}


def canonical(tool):
    return BY_COMMIT.get(tool, tool)


def method(tool):
    return int(tool.rsplit('/m', 1)[1])


def read_history():
    if not os.path.exists(HISTORY):
        return []
    return [json.loads(l) for l in open(HISTORY) if l.strip()]


def key(r):
    return (r['n'], r['commit'], [int(x) for x in r['v'].split('.')], r['kind'], r['bench'])


def line(r):
    return json.dumps(r, separators=(',', ':')) + '\n'


def write_history(old, new):
    os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
    if not old or key(min(new, key=key)) >= key(old[-1]):
        with open(HISTORY, 'a') as f:      # the usual case: a merge newer than all
            f.writelines(line(r) for r in sorted(new, key=key))
        return
    tmp = HISTORY + '.tmp'                 # a backfill goes in at its place
    with open(tmp, 'w') as f:
        f.writelines(line(r) for r in sorted(old + new, key=key))
    os.replace(tmp, HISTORY)


def counts(py, tree, kind):
    args = [py, os.path.join(ROOT, 'tools', 'instrcount.py'), kind, '--each', '-r', str(REPEATS)]
    if kind == 'warm':
        args += ['-n', str(ITERATIONS)]
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True, env=dict(os.environ, NI_MODULE_DIR=tree),
                       preexec_fn=limit)
    if p.returncode:
        raise SystemExit('instrcount %s failed:\n%s' % (kind, p.stdout[-2000:]))
    out = {}
    for l in p.stdout.splitlines():
        f = l.split()
        if len(f) >= 2 and f[0] != 'total' and all(x.lstrip('-').isdigit() for x in f[1:]):
            out[f[0]] = [int(x) for x in f[1:]]
    return out


def build(tree, v, py):
    tag = v.replace('.', '')
    if glob.glob(os.path.join(tree, 'ni.cpython-%s*.so' % tag)):
        return
    env = {k: x for k, x in os.environ.items()
           if k not in ('NI_TRACE', 'NI_ALL_CHECKS', 'NI_SANITIZE', 'NI_CODESTATS')}
    log = os.path.join(tree, 'build-%s.log' % v)
    with open(log, 'w') as f:
        p = subprocess.run([py, 'setup.py', 'build_ext', '--inplace'], cwd=tree, stdout=f,
                           stderr=subprocess.STDOUT, env=env, preexec_fn=limit)
    if p.returncode:
        raise SystemExit('build failed for %s (%s)' % (v, log))


def record(argv):
    sys.path.insert(0, os.path.join(ROOT, 'tools'))
    import heavy
    heavy.hold()
    rest = [a for i, a in enumerate(argv) if not a.startswith('-') and
            (i == 0 or argv[i - 1] != '--python')]
    sha = git('rev-parse', '--verify', (rest[0] if rest else 'HEAD') + '^{commit}').strip()
    here = versions_table(open(os.path.join(ROOT, 'tools', 'versions.txt')).read())
    try:
        then = versions_table(git('show', sha + ':tools/versions.txt'))
        wanted = [v for v, (s, _) in then.items() if s == 'supported']
    except subprocess.CalledProcessError:
        wanted = ['3.7']
    asked = [argv[i + 1] for i, a in enumerate(argv) if a == '--python']
    if asked:
        wanted = [v for v in wanted if v in asked]
    vs = [v for v in wanted if v in here and here[v][0] == 'supported']
    if not vs:
        raise SystemExit('no version to measure (supported at %s: %s)' % (sha[:12], wanted))
    tool = tool_version()
    old = read_history()
    done = {r['v'] for r in old if r['commit'] == sha[:12] and canonical(r['tool']) == tool}
    if done & set(vs):
        raise SystemExit('%s already recorded with tool %s for %s' % (
            sha[:12], tool, ' '.join(sorted(done & set(vs)))))
    n = int(git('rev-list', '--count', '--first-parent', sha).strip())
    date = datetime.datetime.fromtimestamp(int(git('show', '-s', '--format=%ct', sha)),
                                           datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    tree = os.path.join(SCRATCH, sha[:12])
    if not os.path.isdir(tree):
        os.makedirs(tree)
        archive = subprocess.Popen(['git', '-C', ROOT, 'archive', sha], stdout=subprocess.PIPE)
        subprocess.run(['tar', '-x', '-C', tree], stdin=archive.stdout, check=True)
        if archive.wait():
            raise SystemExit('git archive failed')
    for v in vs:
        py = interpreter(v, here)
        full = subprocess.run([py, '-c', 'import platform; print(platform.python_version())'],
                              stdout=subprocess.PIPE, universal_newlines=True,
                              check=True).stdout.strip()
        print('%s %s: building' % (sha[:12], v), flush=True)
        build(tree, v, py)
        new = []
        for kind in ('warm', 'cold'):
            print('%s %s: counting %s' % (sha[:12], v, kind), flush=True)
            for bench, cs in sorted(counts(py, tree, kind).items()):
                lo = min(cs)
                if lo <= 0:
                    raise SystemExit('%s %s: %s count %d, a fault in the method' % (
                        v, bench, kind, lo))
                spread = (max(cs) - lo) / lo
                new.append({'n': n, 'commit': sha[:12], 'date': date, 'tool': tool, 'v': v,
                            'py': full, 'bench': bench, 'kind': kind, 'count': lo,
                            'spread': round(spread, 5),
                            'noisy': (spread > NOISY_SPREAD
                                      or bench in NOISY_BENCHES)})
        write_history(read_history(), new)   # each version as it finishes
        print('%s %s: %d records' % (sha[:12], v, len(new)), flush=True)
    if '--keep' not in argv:
        shutil.rmtree(os.path.join(SCRATCH, sha[:12]))


def by_commit(records, v):
    """{commit: {tool: {(bench, kind): record}}}, the tool in its content form"""
    out = {}
    for r in records:
        if r['v'] == v:
            out.setdefault(r['commit'], {}).setdefault(canonical(r['tool']), {})[
                (r['bench'], r['kind'])] = r
    return out


def newest(tools):
    """of a commit's tools, the one of the highest method, the last recorded on a tie"""
    return max(enumerate(tools), key=lambda it: (method(it[1]), it[0]))[1]


def first_record(cs, c):
    return next(iter(next(iter(cs[c].values())).values()))


def resolve(ref, commits):
    hits = [c for c in commits if c.startswith(ref)]
    if not hits:
        try:
            full = git('rev-parse', '--verify', ref + '^{commit}').strip()
            hits = [c for c in commits if full.startswith(c)]
        except subprocess.CalledProcessError:
            pass
    if len(hits) != 1:
        raise SystemExit('%s: %s' % (ref, 'not recorded' if not hits else 'ambiguous'))
    return hits[0]


def geomean(ratios):
    return math.exp(sum(math.log(x) for x in ratios) / len(ratios)) if ratios else float('nan')


def report(argv):
    records = read_history()
    refs = [a for i, a in enumerate(argv) if not a.startswith('-') and
            (i == 0 or argv[i - 1] != '--python')]
    asked = [argv[i + 1] for i, a in enumerate(argv) if a == '--python']
    vs = sorted({r['v'] for r in records}, key=lambda v: [int(x) for x in v.split('.')])
    flagged = 0
    for v in vs:
        if asked and v not in asked:
            continue
        cs = by_commit(records, v)
        order = sorted(cs, key=lambda c: first_record(cs, c)['n'])
        if refs:
            try:
                a = resolve(refs[0], order)
                b = resolve(refs[1], order) if len(refs) > 1 else order[-1]
            except SystemExit as e:
                print('%s: %s' % (v, e))
                continue
        elif len(order) >= 2:
            a, b = order[-2], order[-1]
        else:
            print('== %s: only %s recorded' % (v, order[0][:10]))
            continue
        # like for like when both commits were measured by one tool
        common = [t for t in cs[a] if t in cs[b]]
        ta, tb = (newest(common),) * 2 if common else (newest(cs[a]), newest(cs[b]))
        A, B = cs[a][ta], cs[b][tb]
        print('== %s: %s (%s) -> %s (%s), %s' % (
            v, a[:10], first_record(cs, a)['date'][:10], b[:10],
            first_record(cs, b)['date'][:10], tb))
        if ta != tb:
            print('   measured by different tools (%s, %s): compare with care' % (ta, tb))
        print('   %-24s %15s %15s %8s %8s' % ('bench', 'warm before', 'warm after', 'warm', 'cold'))
        benches = sorted({b2 for b2, _ in A} & {b2 for b2, _ in B})
        totals = {'warm': [0, 0], 'cold': [0, 0]}
        ratios = []
        for bench in benches:
            cols, marks = [], ''
            for kind in ('warm', 'cold'):
                ra, rb = A.get((bench, kind)), B.get((bench, kind))
                if not ra or not rb:
                    cols.append(None)
                    continue
                totals[kind][0] += ra['count']
                totals[kind][1] += rb['count']
                ch = rb['count'] / ra['count'] - 1
                cols.append(ch)
                if kind == 'warm':
                    if ra['noisy'] or rb['noisy']:
                        marks += '~'
                    else:
                        ratios.append(rb['count'] / ra['count'])
                        if abs(ch) > FLAG:
                            marks += '!'
                            flagged += 1
            wa, wb = A.get((bench, 'warm')), B.get((bench, 'warm'))
            print('   %-24s %15s %15s %8s %8s %s' % (
                bench, wa['count'] if wa else '-', wb['count'] if wb else '-',
                *['%+7.2f%%' % (100 * c) if c is not None else '-' for c in cols], marks))
        for kind in ('warm', 'cold'):
            x, y = totals[kind]
            if x:
                print('   %-24s %15d %15d %+7.2f%%' % ('total ' + kind, x, y, 100 * (y / x - 1)))
        print('   %-24s %15s %15s %+7.2f%%  (warm, %d benches, noisy ones left out)' % (
            'geometric mean', '', '', 100 * (geomean(ratios) - 1), len(ratios)))
    if flagged:
        print('\n%d warm change%s over %.0f%% (!)' % (flagged, '' if flagged == 1 else 's',
                                                     100 * FLAG))


def series(records, v):
    """[(commit record, {bench: warm count}, geomean against the first, total)]"""
    cs = {c: t[newest(t)] for c, t in by_commit(records, v).items()}
    order = sorted(cs, key=lambda c: next(iter(cs[c].values()))['n'])
    first = cs[order[0]]
    out = []
    for c in order:
        warm = {b: r for (b, k), r in cs[c].items() if k == 'warm'}
        ratios = [r['count'] / first[(b, 'warm')]['count'] for b, r in warm.items()
                  if (b, 'warm') in first and not r['noisy'] and not first[(b, 'warm')]['noisy']]
        out.append((next(iter(cs[c].values())), warm, geomean(ratios),
                    sum(r['count'] for r in warm.values())))
    return out


def chart(points, title, w=640, h=180, small=False):
    """an inline SVG line chart of [(label, value)], values as % against the first"""
    pad_l, pad_r, pad_t, pad_b = (44, 10, 22, 30) if not small else (40, 6, 18, 8)
    vals = [p[1] for p in points if p[1] == p[1]]
    if not vals:
        return ''
    lo, hi = min(vals + [0.0]), max(vals + [0.0])
    if hi - lo < 1:
        lo, hi = lo - 0.5, hi + 0.5
    span = hi - lo
    lo, hi = lo - span * 0.08, hi + span * 0.08
    n = len(points)

    def x(i):
        return pad_l + (w - pad_l - pad_r) * (i / (n - 1) if n > 1 else 0.5)

    def y(val):
        return pad_t + (h - pad_t - pad_b) * (hi - val) / (hi - lo)

    out = ['<svg viewBox="0 0 %d %d" role="img" aria-label="%s">' % (w, h, html.escape(title)),
           '<text x="%d" y="13" class="t">%s</text>' % (pad_l, html.escape(title))]
    for val in (lo + (hi - lo) * k / 4 for k in range(5)):
        out.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="g"/>' % (
            pad_l, w - pad_r, y(val), y(val)))
        out.append('<text x="%d" y="%.1f" class="a" text-anchor="end">%+.1f%%</text>' % (
            pad_l - 4, y(val) + 3, val))
    out.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="z"/>' % (
        pad_l, w - pad_r, y(0), y(0)))
    pts = ' '.join('%.1f,%.1f' % (x(i), y(p[1])) for i, p in enumerate(points) if p[1] == p[1])
    out.append('<polyline points="%s" class="l"/>' % pts)
    for i, (label, val) in enumerate(points):
        if val == val:
            out.append('<circle cx="%.1f" cy="%.1f" r="3" class="d"><title>%s: %+.2f%%</title>'
                       '</circle>' % (x(i), y(val), html.escape(label), val))
        if not small:
            out.append('<text x="%.1f" y="%d" class="a" text-anchor="middle">%s</text>' % (
                x(i), h - 10, html.escape(label.split()[0])))
    out.append('</svg>')
    return '\n'.join(out)


def graph(argv):
    out = argv[argv.index('-o') + 1] if '-o' in argv else os.path.join(ROOT, 'build',
                                                                         'instrhistory.html')
    records = read_history()
    vs = sorted({r['v'] for r in records}, key=lambda v: [int(x) for x in v.split('.')])
    body = []
    for v in vs:
        s = series(records, v)
        label = ['%s %s' % (r['commit'][:7], r['date'][:10]) for r, _, _, _ in s]
        body.append('<h2>CPython %s</h2>' % v)
        body.append('<p>Warm instructions per iteration against %s, the first recorded commit; '
                    'lower is faster. Noisy counts are left out of the mean.</p>' % label[0])
        body.append(chart([(l, 100 * (g - 1)) for l, (_, _, g, _) in zip(label, s)],
                          'geometric mean, warm'))
        body.append(chart([(l, 100 * (t / s[0][3] - 1)) for l, (_, _, _, t) in zip(label, s)],
                          'total, warm (mostly mdp)'))
        benches = sorted({b for _, warm, _, _ in s for b in warm})
        body.append('<div class="grid">')
        for b in benches:
            first = next((warm[b]['count'] for _, warm, _, _ in s if b in warm), None)
            pts = [(l, 100 * (warm[b]['count'] / first - 1) if b in warm else float('nan'))
                   for l, (_, warm, _, _) in zip(label, s)]
            noisy = any(warm[b]['noisy'] for _, warm, _, _ in s if b in warm)
            body.append('<div>%s</div>' % chart(pts, b + (' (noisy)' if noisy else ''),
                                               w=300, h=120, small=True))
        body.append('</div>')
    page = PAGE % '\n'.join(body)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, 'w') as f:
        f.write(page)
    print(out)


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ni instruction counts</title>
<style>
:root { --bg: #fff; --fg: #1a1a1a; --muted: #666; --grid: #e4e4e4; --line: #2563eb; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #161616; --fg: #e8e8e8; --muted: #9a9a9a; --grid: #333; --line: #7aa2ff; }
}
body { background: var(--bg); color: var(--fg); font: 14px/1.4 system-ui, sans-serif;
       margin: 0 auto; max-width: 980px; padding: 16px; }
svg { width: 100%%; max-width: 640px; height: auto; display: block; margin: 8px 0 16px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 4px 16px; }
.grid svg { max-width: 300px; }
.t { fill: var(--fg); font-size: 12px; font-weight: 600; }
.a { fill: var(--muted); font-size: 10px; }
.g { stroke: var(--grid); stroke-width: 1; }
.z { stroke: var(--muted); stroke-width: 1; stroke-dasharray: 3 3; }
.l { fill: none; stroke: var(--line); stroke-width: 2; }
.d { fill: var(--line); }
p { color: var(--muted); }
</style></head><body>
<h1>Ni instruction counts</h1>
<p>From bench/history/instrcount.jsonl (tools/instrhistory.py).</p>
%s
</body></html>
"""


def main(argv):
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__)
        return
    cmd = {'record': record, 'report': report, 'graph': graph}.get(argv[0])
    if cmd is None:
        raise SystemExit('unknown command %s (record, report, graph)' % argv[0])
    cmd(argv[1:])


if __name__ == '__main__':
    main(sys.argv[1:])
