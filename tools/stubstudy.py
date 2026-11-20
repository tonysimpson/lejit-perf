"""Stubs and records: what is used (FINDINGS.md "Stubs and records: what is
used").

Needs a build with the counters, whose NI_STUBDUMP=FILE writes a record to
FILE for every stub made and entered, owner record allocated, owner
released, snapshot taken and parked state frozen, with the user-mode
instruction count at each (dispatcher.c, the stub study's dump):

    NI_CODESTATS=1 python3.7 setup.py build_ext --inplace
    python3.7 tools/stubstudy.py dump DIR          # the corpus, below
    python3 tools/stubstudy.py analyse DIR/*.stub  # the tables

'dump' runs import_heavy, many_functions and exec_templates (as
instrcount.py's adversarial child, short length), one cold iteration of
chaos, go and deltablue, and memrepro.py with 2000 functions, two at a
time, each under a 4 GB address-space limit, writing DIR/NAME.stub and
DIR/ni.nm (the symbols, to name the callers of psyco_owner_alloc).

Stub kinds: R a respawn (a run-time condition's rare branch), G a cached
global's guard (a respawn when the value changed, else it resumes), P a
coding pause, M a promotion site. A stub is reached when psyco_stub_enter
(a respawn's in the pool: psyco_respawn_enter) runs it; a promotion site's
stub is its miss path, entered for each value its inline cache doesn't
hold. Bytes are as allocated: pymalloc rounds to 8 bytes up to 512, and
each owner record has an 8-byte list link; a respawn's record in the pool
is counted with its stub, unrounded.
"""
import bisect
import collections
import os
import struct
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REC = struct.Struct('<12Q')
KINDS = 'RGPM'
KIND_NAMES = {'R': 'respawn', 'G': 'global guard', 'P': 'coding pause',
              'M': 'promotion site'}
POOL_STUB = 15  # bytes of a pooled stub (idispatcher.h:STUB_SIZE_MAX)
REGISTERED_KINDS = 'BbMmJj'


def rnd(n):
    """bytes pymalloc gives for n (system malloc above 512)"""
    if n <= 512:
        return (n + 7) & ~7
    return max(32, (n + 8 + 15) & ~15)


def pct(xs, q):
    if not xs:
        return 0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


class Stub:
    __slots__ = ('kind', 'owner', 'base', 'head', 'dist', 't0', 'ps', 'co',
                 'live', 'serial', 'inl', 'pos', 'rec', 'entries', 'first',
                 'freed', 'fz', 'pyc', 'replays', 'compiles')

    def __init__(self, w, kind):
        self.kind = kind
        self.pos = w[0] >> 32
        (_, self.owner, self.base, self.head, self.dist, self.t0, self.ps,
         self.co, self.live, self.serial, self.inl) = w[1:12]
        self.rec = 0
        self.entries = 0
        self.first = None
        self.freed = None
        self.fz = self.pyc = 0
        self.replays = []  # (bytecodes replayed, instructions)
        self.compiles = 0


class Snap:
    __slots__ = ('kind', 'serial', 'pred', 'owner', 'fz', 'pyc', 'summary',
                 'h1', 'h2', 'co', 'dist', 'pos')

    def __init__(self, w, kind):
        self.kind = kind
        self.pos = w[0] >> 32
        (self.serial, self.pred, self.owner, self.fz, p, self.h1, self.h2,
         self.co, self.dist) = w[1:10]
        # a pyc_data shared with an earlier snapshot costs nothing more;
        # a new one has a 16-byte header (dispatcher.c:pyc_share)
        self.pyc = 0 if p >> 48 else (p & 0xffffffff) + 16
        self.summary = (p >> 32) & 0xffff


def load_nm(path):
    syms = []
    if os.path.exists(path):
        for line in open(path):
            f = line.split()
            if len(f) >= 3 and f[1] in 'tTwW':
                syms.append((int(f[0], 16), f[2]))
    syms.sort()
    return syms


def symbol(syms, off):
    i = bisect.bisect_right(syms, (off, '\xff')) - 1
    return syms[i][1] if i >= 0 else '?'


def read(path, syms):
    data = open(path, 'rb').read()
    hdr = info = end = None
    live = {}        # closure -> Stub, while its owner lives
    by_owner = collections.defaultdict(list)
    stubs = []
    snaps = {}
    frozen = {}      # parked state -> (fz, pyc)
    allocs = {}      # address -> (size, caller), owners' records
    alloc_owner = collections.defaultdict(list)
    alloc_sites = collections.Counter()   # caller -> bytes
    alloc_count = collections.Counter()
    ps_of = {}
    entries = []
    owner_frees = 0
    maps = {}        # address -> [bytes, hash, owner, reads]
    map_list = []
    for off in range(0, len(data) - REC.size + 1, REC.size):
        w = REC.unpack_from(data, off)
        t = chr(w[0] & 0xff)
        k = chr((w[0] >> 8) & 0xff)
        if t == 'H':
            hdr = w[1:12]
        elif t == 'I':
            info = w[1:12]
        elif t == 'X':
            end = w[1:6]
        elif t == 'A':
            addr, size, owner, caller = w[1:5]
            allocs[addr] = (size, caller)
            alloc_owner[owner].append(addr)
            name = symbol(syms, caller)
            alloc_sites[name] += rnd(size + 8)
            alloc_count[name] += 1
        elif t == 'S':
            s = Stub(w, k)
            rec = allocs.get(w[1] - hdr[5])  # the stub_record_t before it
            s.rec = rec[0] if rec else 0
            if not rec and k == 'R' and info and info[6]:
                s.rec = -info[6]  # in the pool: stub and record, unrounded
            live[w[1]] = s
            by_owner[s.owner].append(w[1])
            stubs.append(s)
            if s.ps:
                ps_of[s.ps] = s
        elif t == 'E':
            s = live.get(w[1])
            if s is None:
                continue
            t_in, t_out, ser_in, ser_out, t_replay, bc = w[2:8]
            if bc >= 1 << 63:
                bc = -1
            s.entries += 1
            if s.first is None:
                s.first = t_in - s.t0
            if ser_out > ser_in:
                s.compiles += 1
            if bc >= 0 and t_replay:
                s.replays.append((bc, t_replay - t_in, t_out - t_replay))
            entries.append((s.kind, t_out - t_in, bc))
        elif t == 'N':
            n = Snap(w, k)
            if k == 'Z':
                s = ps_of.get(w[10])
                if s is not None:
                    s.fz, s.pyc = n.fz, n.pyc
            else:
                snaps[n.serial] = n
        elif t == 'L':
            a = allocs.get(w[1])
            m = [rnd(a[0] + 8) if a else 0, w[2], w[3], 0]
            maps[w[1]] = m
            map_list.append(m)
        elif t == 'Q':
            m = maps.get(w[1])
            if m is not None:
                m[3] += 1
        elif t == 'F':
            owner, now = w[1:3]
            owner_frees += 1
            for c in by_owner.pop(owner, ()):
                s = live.pop(c, None)
                if s is not None:
                    s.freed = now - s.t0
            for a in alloc_owner.pop(owner, ()):
                allocs.pop(a, None)
    return dict(hdr=hdr, info=info, end=end, stubs=stubs, snaps=snaps,
                alloc_sites=alloc_sites, alloc_count=alloc_count,
                entries=entries, owner_frees=owner_frees, maps=map_list)


def stub_bytes(s, hdr):
    """what a stub holds: its record (with the parked state in it), its
    pooled code, and the frozen state's snapshot and pyc_data"""
    if s.rec < 0:
        return -s.rec
    b = rnd(s.rec + 8)
    if s.kind in 'RGP':
        b += POOL_STUB
    if s.fz:
        b += rnd(s.fz) + rnd(s.pyc)
    return b


def nearest_kept(s, snaps):
    """distance in bytecode instructions from the nearest snapshot that is
    kept anyway (a registered block, or the first of a chain), and what the
    walk crossed: 'promotion' if it needs a promoted value"""
    d = s.dist
    serial = s.base - 1
    crossed = ''
    seen = 0
    while serial >= 0 and seen < 1000:
        seen += 1
        n = snaps.get(serial)
        if n is None:
            return d, crossed or 'unknown'
        if n.summary:          # registered at a merge or join point
            return d, crossed or 'registered'
        d += n.dist
        if n.kind == 'M' or n.kind == 'm':
            crossed = 'promotion'  # a promotion's continuation
        if n.pred == 0:
            return d, crossed or 'root'
        serial = n.pred - 1
        if n.kind == 'R':          # its distance is from the chain's head
            while serial in snaps and snaps[serial].kind == 'R':
                serial = snaps[serial].pred - 1
    return d, crossed or 'unknown'


def analyse(paths):
    syms = load_nm(os.path.join(os.path.dirname(paths[0]), 'ni.nm'))
    tot = collections.defaultdict(collections.Counter)
    runs = [(os.path.basename(p).rsplit('.', 1)[0], read(p, syms))
            for p in paths]
    # one fit for all: a program's respawns may all be of one length
    fit = replay_fit([x for _, r in runs for s in r['stubs']
                      for x in s.replays], runs[0][1]['hdr'][0])
    print('Replay cost today, all runs: %.0f + %.0f instructions per bytecode '
          'instruction replayed' % fit)
    for name, r in runs:
        hdr, end = r['hdr'], r['end']
        overhead = hdr[0]
        nbuf = end[4] or 1
        stubs = r['stubs']
        # end: instructions, bytecodes compiled, of them replayed,
        # CodeBufferObjects, code buffers
        print('== %s: %d code buffers, %d CodeBufferObjects, %d stubs, %d '
              'owners freed; %d bytecode instructions compiled, %d of them '
              'in replays; about %d instructions per dump record' %
              (name, nbuf, end[3], len(stubs), r['owner_frees'], end[1],
               end[2], overhead))
        reach_table(name, stubs, hdr, nbuf, tot, r['entries'])
        soon_table(stubs, overhead)
        breakdown(name, r, stubs, nbuf, hdr)
        cbo_table(r, nbuf, hdr)
        maps_table(r, nbuf)
        pyc_table(r['snaps'])
        replay_table(name, stubs, r['snaps'], nbuf, hdr, overhead, fit,
                     end[0])
        print()
    if len(paths) > 1:
        print('== all: never reached, by kind')
        for k in KINDS:
            c = tot[k]
            if c['n']:
                print('  %-15s %8d made, %5.1f%% never reached' %
                      (KIND_NAMES[k], c['n'], 100.0 * c['never'] / c['n']))


def reach_table(name, stubs, hdr, nbuf, tot, entries):
    print('Reach, by kind (bytes: record with parked state, pooled code, '
          'frozen snapshot and pyc_data)')
    print('  %-15s %8s %7s %7s %9s %9s %8s %9s %9s' %
          ('kind', 'made', 'per buf', 'never%', 'entries', 'B each', 'B/buf',
           'never B/b', 'freed%'))
    allb = allnever = 0
    for k in KINDS:
        ss = [s for s in stubs if s.kind == k]
        if not ss:
            continue
        never = [s for s in ss if s.entries == 0]
        reached = [s for s in ss if s.entries]
        b = sum(stub_bytes(s, hdr) for s in ss)
        nb = sum(stub_bytes(s, hdr) for s in never)
        allb += b
        allnever += nb
        tot[k]['n'] += len(ss)
        tot[k]['never'] += len(never)
        print('  %-15s %8d %7.2f %7.1f %9.2f %9.1f %8.1f %9.1f %9.1f' %
              (KIND_NAMES[k], len(ss), len(ss) / nbuf,
               100.0 * len(never) / len(ss),
               sum(s.entries for s in reached) / max(1, len(reached)),
               b / len(ss), b / nbuf, nb / nbuf,
               100.0 * sum(1 for s in ss if s.freed is not None) / len(ss)))
    print('  %-15s %8d %7.2f %7.1f %9s %9s %8.1f %9.1f' %
          ('all', len(stubs), len(stubs) / nbuf,
           100.0 * sum(1 for s in stubs if not s.entries) / max(1, len(stubs)),
           '', '', allb / nbuf, allnever / nbuf))
    g = [s for s in stubs if s.kind == 'G' and s.entries]
    if g:
        print('  global guards reached: %d, of them respawned %d; %d entries, '
              '%.1f M instructions in them with the dump\'s' %
              (len(g), sum(1 for s in g if s.replays),
               sum(s.entries for s in g),
               sum(e[1] for e in entries if e[0] == 'G') / 1e6))
    m = [s for s in stubs if s.kind == 'M']
    if m:
        vals = collections.Counter(min(s.compiles, 4) for s in m)
        print('  promotion sites by values compiled (4: 4 or more): %s' %
              ', '.join('%d: %.1f%%' % (v, 100.0 * vals[v] / len(m))
                        for v in sorted(vals)))
    p = [s for s in stubs if s.kind in 'PM']
    if p:
        fz = [s for s in p if s.fz]
        print('  parked states frozen: %d of %d; live states (upper bound) '
              'mean %.0f B' % (len(fz), len(p),
                               sum(s.live for s in p if not s.fz) /
                               max(1, len(p) - len(fz))))


def soon_table(stubs, overhead):
    print('Of those reached, instructions from creation to first entry:')
    for k in KINDS:
        ss = [s.first for s in stubs if s.kind == k and s.first is not None]
        if ss:
            print('  %-15s %8d reached: median %.2f M, p10 %.2f M, p90 %.1f M'
                  % (KIND_NAMES[k], len(ss), pct(ss, .5) / 1e6,
                     pct(ss, .1) / 1e6, pct(ss, .9) / 1e6))


def breakdown(name, r, stubs, nbuf, hdr):
    print('Owner records per code buffer, by caller of psyco_owner_alloc '
          '(rounded, with the 8-byte link):')
    sites = r['alloc_sites']
    tot = sum(sites.values())
    for site, b in sites.most_common(10):
        print('  %-36s %8.1f B/buf  %7.2f per buf  %5.1f B each' %
              (site, b / nbuf, r['alloc_count'][site] / nbuf,
               b / r['alloc_count'][site]))
    print('  %-36s %8.1f B/buf' % ('all', tot / nbuf))
    snaps = r['snaps']
    cbo = collections.Counter()
    for n in snaps.values():
        cbo[n.summary] += 1
    print('  CodeBufferObjects with a snapshot, by summary bytes: %s' %
          ', '.join('%d: %d' % kv for kv in sorted(cbo.items())))
    fzb = sum(rnd(n.fz) for n in snaps.values())
    pycb = sum(rnd(n.pyc) for n in snaps.values())
    print('  buffers\' snapshots %.1f B/buf, their pyc_data %.1f B/buf' %
          (fzb / nbuf, pycb / nbuf))
    dup = collections.Counter((s.kind, s.base, s.pos) for s in stubs)
    print('  stubs sharing kind, block and instruction with another: %.1f%%'
          % (100.0 * sum(c for c in dup.values() if c > 1) / max(1, len(stubs))))


def pyc_table(snaps):
    by_co = collections.defaultdict(list)
    for n in snaps.values():
        by_co[n.co].append(n)
    same = same2 = total = 0
    distinct = distinct2 = 0
    for co, ns in by_co.items():
        h1 = collections.Counter(n.h1 for n in ns)
        h2 = collections.Counter(n.h2 for n in ns)
        total += len(ns)
        distinct += len(h1)
        distinct2 += len(h2)
    print('pyc_data: %d snapshots of %d code objects; distinct per code '
          'object: %.1f%% of snapshots as stored, %.1f%% with next_instr '
          'left out' % (total, len(by_co), 100.0 * distinct / max(1, total),
                        100.0 * distinct2 / max(1, total)))


def replay_fit(rs, overhead):
    """instructions = a + b * bytecodes replayed, least squares, over
    (bytecodes, replay instructions) of the respawns seen"""
    n = len(rs)
    if n < 2:
        return 5000.0, 3000.0
    bcs = [x[0] for x in rs]
    ins = [max(0, x[1] - overhead) for x in rs]
    mx = sum(bcs) / n
    my = sum(ins) / n
    sxx = sum((x - mx) ** 2 for x in bcs) or 1
    b = sum((x - mx) * (y - my) for x, y in zip(bcs, ins)) / sxx
    return my - b * mx, b


# what a parked state would leave in its record if it were replayed
# instead: the respawn count and the promoted variable's path
REPLAY_KEEP = 16


def replay_table(name, stubs, snaps, nbuf, hdr, overhead, fit, total_instr):
    rs = [x for s in stubs for x in s.replays]
    a, b = fit
    if rs:
        bcs = [x[0] for x in rs]
        ins = [max(0, x[1] - overhead) for x in rs]
        print('Respawns today: %d; bytecodes replayed median %d, p90 %d, max %d;'
              ' replay instructions median %.1f k, p90 %.1f k, mean %.1f k; '
              'the branch compiled after: median %.0f k; all replays %.2f%% of '
              'the process\'s instructions'
              % (len(rs), pct(bcs, .5), pct(bcs, .9), max(bcs),
                 pct(ins, .5) / 1e3, pct(ins, .9) / 1e3,
                 sum(ins) / len(ins) / 1e3,
                 pct([x[2] for x in rs], .5) / 1e3,
                 100.0 * sum(ins) / total_instr))
    print('Replay distance in bytecode instructions: from the stub\'s own '
          'block (its respawn base today), and from the nearest registered '
          'block or chain root, with what that walk starts from')
    psize = hdr[4]
    for k in KINDS:
        ss = [s for s in stubs if s.kind == k]
        if not ss:
            continue
        d = [nearest_kept(s, snaps) for s in ss]
        why = collections.Counter(x[1] for x in d)
        dist = [x[0] for x in d]
        own = [s.dist for s in ss]
        print('  %-15s own block: median %d p90 %d max %d; registered: median '
              '%d p90 %d max %d (%s)' %
              (KIND_NAMES[k], pct(own, .5), pct(own, .9), max(own),
               pct(dist, .5), pct(dist, .9), max(dist),
               ', '.join('%s %.0f%%' % (w, 100.0 * c / len(ss))
                         for w, c in why.most_common())))
        if k not in 'PM':
            continue
        # every entry replays from the stub's own block
        cost = sum(s.entries * (a + b * max(1, s.dist)) for s in ss)
        saved = sum(psize - REPLAY_KEEP +
                    (rnd(s.fz) + rnd(s.pyc) if s.fz else 0) for s in ss)
        live = sum(s.live for s in ss if not s.fz)
        print('  %-15s replaying instead of parking: %.0f B/buf saved, and the '
              'live states (upper bound) %.0f B/buf; %.1f M instructions for %d '
              'entries (%.0f per buffer, %.2f%% of the process)'
              % ('', saved / nbuf, live / nbuf, cost / 1e6,
                 sum(s.entries for s in ss), cost / nbuf,
                 100.0 * cost / total_instr))
        if k == 'M':
            # the state kept for the first value, then dropped: later
            # values replay from the site's block
            cost = sum(max(0, s.entries - 1) * (a + b * max(1, s.dist))
                       for s in ss)
            saved = sum(psize - REPLAY_KEEP +
                        (rnd(s.fz) + rnd(s.pyc) if s.fz else 0)
                        for s in ss if s.compiles)
            print('  %-15s dropping the state after the first value: %.0f '
                  'B/buf saved, %.1f M instructions (%.2f%% of the process)'
                  % ('', saved / nbuf, cost / 1e6, 100.0 * cost / total_instr))


def maps_table(r, nbuf):
    ms = r['maps']
    if not ms:
        return
    b = sum(m[0] for m in ms)
    read = [m for m in ms if m[3]]
    seen = set()
    dup = 0
    for m in ms:
        key = (m[2], m[1])
        if key in seen:
            dup += m[0]
        seen.add(key)
    print('Locals maps: %d (%.2f per buf, %.0f B each, %.1f B/buf with the '
          'global index\'s 8 B); read %d (%.2f%%), %d reads; bytes duplicating '
          'an earlier map of the same owner %.1f%%'
          % (len(ms), len(ms) / nbuf, b / len(ms), (b + 8 * len(ms)) / nbuf,
             len(read), 100.0 * len(read) / len(ms), sum(m[3] for m in read),
             100.0 * dup / max(1, b)))


def cbo_table(r, nbuf, hdr):
    snaps = r['snaps']
    total = r['end'][3]
    base = hdr[2]
    by = collections.Counter(n.summary for n in snaps.values())
    nosnap = total - sum(by.values())
    b = (nosnap + by[0]) * base + sum(c * rnd(base + s) for s, c in by.items()
                                      if s)
    print('CodeBufferObjects: %.2f per buf (%.2f with no snapshot, %.2f '
          'unregistered, %.2f registered); %.1f B/buf as objects, of which '
          'the 16-byte object header %.1f B/buf'
          % (total / nbuf, nosnap / nbuf, by[0] / nbuf,
             (sum(by.values()) - by[0]) / nbuf, b / nbuf, 16.0 * total / nbuf))


def dump(outdir):
    py = sys.executable
    adv = os.path.join(ROOT, 'tools', 'adversarial')
    sizes = {}
    for line in open(os.path.join(adv, 'sizes.txt')):
        f = line.split()
        if f and not line.startswith('#'):
            sizes[f[0]] = f[1]
    ic = os.path.join(ROOT, 'tools', 'instrcount.py')
    jobs = [(n, [ic, '--adv', 'ni', os.path.join(adv, n + '.py'), sizes[n]])
            for n in ('import_heavy', 'many_functions', 'exec_templates')]
    jobs += [(n, [ic, '--bench', 'ni',
                  os.path.join(ROOT, 'tools', 'pyperformance', 'bm_%s.py' % n),
                  '1', '0'])
             for n in ('chaos', 'go', 'deltablue')]
    jobs.append(('memrepro', [os.path.join(ROOT, 'tools', 'memrepro.py'), ROOT,
                              '2000']))
    os.makedirs(outdir, exist_ok=True)
    so = [f for f in os.listdir(ROOT) if f.startswith('ni.') and f.endswith('.so')]
    with open(os.path.join(outdir, 'ni.nm'), 'w') as f:
        subprocess.run(['nm', os.path.join(ROOT, so[0])], stdout=f, check=True)

    def limit():
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (4000000000, 4000000000))
    running = []
    for name, args in jobs + [(None, None), (None, None)]:
        if name is not None:
            out = os.path.join(outdir, name + '.stub')
            env = dict(os.environ, NI_STUBDUMP=out, PYTHONHASHSEED='0')
            log = open(os.path.join(outdir, name + '.out'), 'w')
            running.append((name, subprocess.Popen([py] + args, env=env, cwd=ROOT,
                                                   stdout=log, stderr=log,
                                                   preexec_fn=limit)))
        while running and (len(running) >= 2 or name is None):
            n, p = running.pop(0)
            print(n, p.wait(timeout=1800), flush=True)
            if name is None:
                break


if __name__ == '__main__':
    if sys.argv[1:2] == ['dump']:
        dump(sys.argv[2])
    elif sys.argv[1:2] == ['analyse']:
        analyse(sys.argv[2:])
    else:
        raise SystemExit(__doc__)
