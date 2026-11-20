"""What snapshots hold and whether they compress (FINDINGS.md "Snapshots:
what they hold and whether they compress").

Needs a build with the counters, whose NI_SNAPDUMP=FILE appends every
snapshot taken to FILE (dispatcher.c:psyco_cs_snapdump):

    NI_CODESTATS=1 python3.7 setup.py build_ext --inplace
    python3.7 tools/snapstudy.py dump DIR             # the corpus, below
    python3 tools/snapstudy.py analyse DIR/*.snap     # the tables
    python3 tools/snapstudy.py analyse F --buffers N  # also bytes per buffer

'dump' runs import_heavy, many_functions and exec_templates (as
instrcount.py's adversarial child, short length), one cold iteration of
chaos, go and deltablue, and memrepro.py with 500 functions, two at a time,
each under a 4 GB address-space limit, writing DIR/NAME.snap.

'analyse' (any Python 3.7+; uses the zstd command if there is one) reports,
per file and for all files: count and bytes by kind, the size distribution,
exact duplicates, general compressors on each snapshot alone and with a
dictionary trained on half the snapshots, order-0 entropy, deltas against
related snapshots as copy/insert edits, and tables of the K most frequent
fragments. Kinds: B a new block at a merge point, b the same after a
partial match, M and m a proxy at a merge point (new state, partial
match), J and j at a join point, U an unregistered buffer (a coding
pause), R a respawned branch's buffer, Z a parked state frozen.

A snapshot is two streams in one allocation (dispatcher.c, fz_compress):
one byte per opcode (an item's sub-array length, NULL, a link to an item
already stored, a type hint, or an escape for a length over 127), and one
8-byte Source per stored item and hint. Both are read from the middle
outwards, so the 'read order' used here is the order fz_uncompress reads
them, the last vlocals first. A token is one item as read: its opcode
bytes and its arguments.
"""
import collections
import glob
import lzma
import math
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HDR = struct.Struct('<4sBQQQiBBIII')
KINDS = 'BbMmJjURZ'
OPC_EXT, OPC_NULL, OPC_HINT = -1, -2, -3
CT = 1  # compile-time tag in a Source


class Snap:
    __slots__ = ('idx', 'kind', 'serial', 'pred', 'co', 'pos', 'inl', 'summary',
                 'opc', 'args', 'vals', 'flags', 'pyc', 'name', 'file', 'line',
                 'tokens', 'toksize')

    def fzbytes(self):
        return len(self.opc) + 8 * len(self.args)

    def blob(self):
        return self.opc + struct.pack('<%dQ' % len(self.args), *self.args)


def alloc(n):
    """bytes PyMem_MALLOC takes for n: pymalloc's 8-byte classes up to 512,
    then glibc malloc's 16-byte chunks with an 8-byte header"""
    if n <= 512:
        return (n + 7) & ~7
    return max(32, (n + 8 + 15) & ~15)


def load(path):
    data = open(path, 'rb').read()
    out, p = [], 0
    while p < len(data):
        (magic, kind, serial, pred, co, pos, inl, summ,
         nopc, nargs, npyc) = HDR.unpack_from(data, p)
        assert magic == b'SNP1', (path, p)
        p += HDR.size
        s = Snap()
        s.idx, s.kind, s.serial, s.pred, s.co = len(out), chr(kind), serial, pred, co
        s.pos, s.inl, s.summary = pos, inl, summ
        strs = []
        for _ in range(2):
            n, = struct.unpack_from('<H', data, p)
            strs.append(data[p + 2:p + 2 + n].decode('ascii', 'replace'))
            p += 2 + n
        s.name, s.file = strs
        s.line, = struct.unpack_from('<i', data, p)
        p += 4
        s.opc = data[p:p + nopc]
        p += nopc
        s.args = struct.unpack_from('<%dQ' % nargs, data, p)
        p += 8 * nargs
        s.vals = struct.unpack_from('<%dq' % nargs, data, p)
        p += 8 * nargs
        s.flags = data[p:p + nargs]
        p += nargs
        s.pyc = data[p:p + npyc]
        p += npyc
        tokenise(s)
        out.append(s)
    return out


def tokenise(s):
    """s.tokens: one tuple per item as read (opcode bytes, then arguments);
    a fragment of tokens is a fragment of both streams"""
    opc = struct.unpack('%db' % len(s.opc), s.opc)
    toks, sizes = [], []
    oi = ai = 0
    while oi < len(opc):
        o0 = oi
        a0 = ai
        o = opc[oi]
        oi += 1
        if o == OPC_HINT:
            ai += 1
            o = opc[oi]
            oi += 1
        if o == OPC_EXT:
            ai += 1
            o = 0  # any length
        if o >= 0 and o0 > 0:
            ai += 1  # the item's source
        toks.append((s.opc[o0:oi],) + tuple(s.args[a0:ai]))
        sizes.append((oi - o0) + 8 * (ai - a0))
    assert ai == len(s.args), 'bad parse'
    s.tokens, s.toksize = toks, sizes


def tree(s):
    """the items as (first token, end token) spans of whole sub-arrays"""
    opc = [t[0] for t in s.tokens]
    spans = []

    def length(b):
        v = struct.unpack('%db' % len(b), b)
        v = [x for x in v if x != OPC_HINT]
        return v[0]

    def walk(i, n):
        for _ in range(n):
            o = length(opc[i])
            start = i
            i += 1
            if o == OPC_EXT:
                return None
            if o > 0:
                i = walk(i, o)
                if i is None:
                    return None
                spans.append((start, i))
        return i
    walk(1, length(opc[0]))
    return spans


def pct(a, b):
    return '%5.1f%%' % (100.0 * a / b) if b else '    -'


def vlen(n):
    k = 1
    while n >= 128:
        n >>= 7
        k += 1
    return k


def show(title, rows, head):
    print('\n' + title)
    w = [max(len(str(r[i])) for r in rows + [head]) for i in range(len(head))]
    for r in [head] + rows:
        print('  ' + '  '.join(str(c).rjust(w[i]) if i else str(c).ljust(w[i])
                               for i, c in enumerate(r)))


# ------------------------------------------------------------------ measures

def by_kind(snaps, buffers):
    rows = []
    tot = collections.Counter()
    for k in KINDS + '*':
        ss = [s for s in snaps if k == '*' or s.kind == k]
        if not ss:
            continue
        c = collections.Counter()
        for s in ss:
            c['n'] += 1
            c['opc'] += len(s.opc)
            c['args'] += 8 * len(s.args)
            c['alloc'] += alloc(s.fzbytes())
            c['pyc'] += alloc(len(s.pyc))
            c['summ'] += s.summary
        rows.append([k, c['n'], c['opc'], c['args'], c['alloc'], c['pyc'], c['summ'],
                     '%.0f' % ((c['opc'] + c['args']) / c['n'])])
        if k == '*':
            tot = c
    show('By kind (bytes; alloc = the fz allocation as pymalloc/malloc round it)',
         rows, ['kind', 'count', 'opcodes', 'args', 'fz alloc', 'pyc alloc',
                'summaries', 'mean fz'])
    if buffers:
        print('  per code buffer (%d): fz %.0f B, pyc %.0f B, summaries %.0f B, '
              '%.2f snapshots' % (buffers, tot['alloc'] / buffers, tot['pyc'] / buffers,
                                  tot['summ'] / buffers, tot['n'] / buffers))
    return tot


def distribution(snaps):
    z = sorted(s.fzbytes() for s in snaps)
    q = [z[int(f * (len(z) - 1))] for f in (0, .1, .25, .5, .75, .9, .99, 1)]
    toks = sorted(len(s.tokens) for s in snaps)
    print('\nSize (fz bytes): min %d, p10 %d, p25 %d, median %d, p75 %d, p90 %d, '
          'p99 %d, max %d; items per snapshot median %d, max %d' %
          tuple(q + [toks[len(toks) // 2], toks[-1]]))
    big = sum(x for x in z if x > 512)
    print('  over 512 B (system malloc): %s of snapshots, %s of bytes' %
          (pct(sum(1 for x in z if x > 512), len(z)), pct(big, sum(z))))
    cats = collections.Counter()
    for s in snaps:
        for t, f in zip(s.args, s.flags):
            cats['ct' if t & 3 == 1 else 'vt' if t & 2 else 'rt'] += 1
        for t in s.tokens:
            o = t[0][-1] if t[0][-1] < 128 else t[0][-1] - 256
            cats['null' if o == OPC_NULL else 'link' if o <= -4 else 'item'] += 1
        cats['hint'] += s.opc.count(bytes([256 + OPC_HINT]))
    n = sum(cats[k] for k in ('ct', 'vt', 'rt'))
    print('  arguments: %s compile-time, %s virtual, %s run-time (%d); opcodes: '
          '%d items, %d NULL, %d links, %d hints' %
          (pct(cats['ct'], n), pct(cats['vt'], n), pct(cats['rt'], n), n,
           cats['item'], cats['null'], cats['link'], cats['hint']))


def duplicates(snaps):
    def canon(s):
        a = tuple((v, f) if f != 0xff else (x,) for x, v, f in zip(s.args, s.vals, s.flags))
        return (s.opc, a)
    raw = collections.Counter(s.blob() for s in snaps)
    can = collections.Counter(canon(s) for s in snaps)
    pyc = collections.Counter(s.pyc for s in snaps)
    dup_raw = sum(len(b) * (c - 1) for b, c in raw.items())
    cs = collections.Counter()
    for s in snaps:
        cs[canon(s)] += s.fzbytes()
    dup_can = sum(b - b // can[k] for k, b in cs.items())
    tot = sum(s.fzbytes() for s in snaps)
    print('\nExact duplicates: %d distinct of %d (%s of fz bytes are repeats); '
          'with constants compared by value and flags, %d distinct; pyc_data: '
          '%d distinct' % (len(raw), len(snaps), pct(dup_raw, tot), len(can), len(pyc)))
    print('  sharing one copy of each by value would save %s of fz bytes '
          '(less a count per copy); by address, %s' % (pct(dup_can, tot), pct(dup_raw, tot)))
    kc = collections.Counter()
    seen = set()
    for s in snaps:
        b = s.blob()
        if b in seen:
            kc[s.kind] += 1
        seen.add(b)
    if kc:
        print('  repeats by kind: ' + ', '.join('%s %d' % kv for kv in sorted(kc.items())))


def zstd_sizes(train, test, level=19, dict_size=None):
    """total zstd bytes of each of 'test' alone, without and with a
    dictionary trained on 'train' (None if no zstd command)"""
    if shutil.which('zstd') is None:
        return None
    d = tempfile.mkdtemp(prefix='snapstudy')
    try:
        for name, ss in (('train', train), ('test', test)):
            os.mkdir(os.path.join(d, name))
            for i, s in enumerate(ss):
                with open(os.path.join(d, name, str(i)), 'wb') as f:
                    f.write(s.blob())
        res = []
        dic = os.path.join(d, 'dict')
        files = sorted(glob.glob(os.path.join(d, 'train', '*')))
        subprocess.run(['zstd', '-q', '--train', '--maxdict=%d' % (dict_size or 32768),
                        '-o', dic] + files, check=True, timeout=600,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for extra in ([], ['-D', dic]):
            out = os.path.join(d, 'out%d' % len(extra))
            os.mkdir(out)
            subprocess.run(['zstd', '-q', '-%d' % level, '--no-check', '--no-dictID',
                            '--output-dir-flat', out] + extra +
                           sorted(glob.glob(os.path.join(d, 'test', '*'))),
                           check=True, timeout=600)
            res.append(sum(os.path.getsize(p) for p in glob.glob(os.path.join(out, '*'))))
        res.append(open(dic, 'rb').read())
        return res
    finally:
        shutil.rmtree(d)


def compressors(snaps):
    train, test = snaps[:len(snaps) // 2], snaps[len(snaps) // 2:]
    raw = sum(s.fzbytes() for s in test)
    rows = []

    def deflate(b, zdict=None):
        c = zlib.compressobj(9, zlib.DEFLATED, -15, 9, zlib.Z_DEFAULT_STRATEGY,
                             **({'zdict': zdict} if zdict else {}))
        return len(c.compress(b) + c.flush())
    lz = [{'id': lzma.FILTER_LZMA2, 'preset': 9}]
    rows.append(['deflate -9 (raw)', sum(deflate(s.blob()) for s in test)])
    rows.append(['lzma2 -9 (raw)', sum(len(lzma.compress(s.blob(), lzma.FORMAT_RAW,
                                                          filters=lz)) for s in test)])
    z = zstd_sizes(train, test)
    if z:
        rows.append(['zstd -19', z[0]])
        rows.append(['zstd -19, 32 kB dictionary', z[1]])
        rows.append(['deflate -9, same dictionary', sum(deflate(s.blob(), z[2][-32768:])
                                                        for s in test)])
    # the whole half as one stream: what sharing across snapshots could give
    whole = b''.join(s.blob() for s in test)
    rows.append(['lzma2 -9, all as one stream', len(lzma.compress(whole, lzma.FORMAT_RAW,
                                                                  filters=lz))])
    show('General compressors on the second half of the snapshots taken (%d, the '
         'dictionary trained on the first), %d fz bytes' % (len(test), raw),
         [[r[0], r[1], pct(r[1], raw)] for r in rows], ['compressor', 'bytes', 'of raw'])


def entropy(snaps):
    def h0(counter):
        n = sum(counter.values())
        return -sum(c * math.log2(c / n) for c in counter.values()), n
    ob = collections.Counter()
    av = collections.Counter()
    tk = collections.Counter()
    for s in snaps:
        ob.update(s.opc)
        av.update(s.args)
        tk.update(s.tokens)
    bo, no = h0(ob)
    ba, na = h0(av)
    bt, nt = h0(tk)
    raw = sum(s.fzbytes() for s in snaps)
    print('\nOrder-0 entropy: opcodes %.2f bits per byte (%d bytes -> %.0f); '
          'arguments %.2f bits per 8-byte Source (%d distinct of %d -> %.0f bytes); '
          'whole items %.2f bits each (%d distinct of %d -> %.0f bytes, %s of fz '
          'bytes, a lower bound with a corpus-wide table of %d items)' %
          (bo / no, no, bo / 8, ba / na, len(av), na, ba / 8, bt / nt, len(tk), nt,
           bt / 8, pct(bt / 8, raw), len(tk)))


def delta_cost(base, target):
    """bytes of 'target' as copy/insert edits of whole tokens of 'base',
    and the number of edits: copy = op byte + offset + length varints,
    insert = op byte + length varint + the tokens' bytes as stored"""
    from difflib import SequenceMatcher
    sm = SequenceMatcher(None, base.tokens, target.tokens, autojunk=False)
    cost = edits = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            cost += 1 + vlen(i1) + vlen(i2 - i1)
            edits += 1
        elif tag in ('replace', 'insert'):
            cost += 1 + vlen(j2 - j1) + sum(target.toksize[j1:j2])
            edits += 1
    return cost, edits


def tails(snaps):
    """the end of the read order (the first vlocals, written first) shared
    with an earlier snapshot, whole items: a snapshot would end in a
    'continue in that one' item (an opcode and a pointer to both of its
    streams there, 17 bytes), the shared one kept alive by a count (8)"""
    by_serial = {s.serial: s for s in snaps if s.kind != 'Z'}
    last_co = {}
    recent = collections.defaultdict(list)
    tot = sum(s.fzbytes() for s in snaps)
    rows = []
    res = collections.defaultdict(lambda: [0, 0, 0])
    alloc_now = [0, 0]

    def shared(b, s):
        x, y = b.tokens, s.tokens
        i = 0
        m = min(len(x), len(y)) - 1  # never the top count
        while i < m and x[-1 - i] == y[-1 - i]:
            i += 1
        return sum(s.toksize[len(y) - i:]) if i else 0
    for s in snaps:
        cands = {'a predecessor buffer': by_serial.get(s.pred - 1) if s.pred else None,
                 'c same code, nearest earlier': last_co.get(s.co)}
        best = 0
        for name, b in cands.items():
            if b is not None and b is not s:
                g = shared(b, s)
                res[name][0] += 1
                res[name][1] += g
                if g > 17 + 8:
                    res[name][2] += g - 17 - 8
                best = max(best, g)
        for b in recent[s.co][-8:]:
            best = max(best, shared(b, s))
        g = shared(by_serial[s.pred - 1], s) if s.pred and s.pred - 1 in by_serial else 0
        alloc_now[0] += alloc(s.fzbytes())
        alloc_now[1] += alloc(s.fzbytes() - g + 25) if g > 25 else alloc(s.fzbytes())
        r = res['d best of a, c and the 8 latest of the same code']
        r[0] += 1
        r[1] += best
        if best > 25:
            r[2] += best - 25
        last_co[s.co] = s
        recent[s.co].append(s)
    for name in sorted(res):
        r = res[name]
        rows.append([name, r[0], r[1], pct(r[1], tot), r[2], pct(r[2], tot)])
    show('Shared ends of the read order (the first vlocals), whole items',
         rows, ['base', 'with base', 'shared bytes', 'of fz', 'net saving', 'of fz'])
    print('  allocated, sharing with the predecessor (25 bytes for the marker): '
          '%d -> %d (%s)' % (alloc_now[0], alloc_now[1], pct(alloc_now[1], alloc_now[0])))
    return alloc_now


def keyframes(snaps, limit=0.5):
    """a form the readers could follow: a snapshot is either whole (a key)
    or a delta against a key, never a delta of a delta, so that a copy can
    point straight into the key's streams. The delta is in the snapshot's
    own two streams: its inserted items as they are, and each copy one
    pseudo-item (an opcode byte and one Source packing the key's opcode
    and argument offsets and the opcode count: 9 bytes); the key's address
    is one more argument (8). The base is the predecessor if it is a key,
    else the predecessor's key; a delta over 'limit' of the size is stored
    whole and becomes a key."""
    from difflib import SequenceMatcher
    by_serial = {}
    key_of = {}
    tot = sum(s.fzbytes() for s in snaps)
    stored = keys = copies = 0
    for s in snaps:
        size = s.fzbytes()
        p = by_serial.get(s.pred - 1) if s.pred else None
        base = key_of.get(id(p)) if p is not None else None
        cost = None
        if base is not None:
            sm = SequenceMatcher(None, base.tokens, s.tokens, autojunk=False)
            cost, n = 8, 0
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag == 'equal':
                    cost += 9
                    n += 1
                elif tag in ('replace', 'insert'):
                    cost += sum(s.toksize[j1:j2])
        if cost is None or cost > limit * size:
            stored += size
            keys += 1
            key_of[id(s)] = s
        else:
            stored += cost
            copies += n
            key_of[id(s)] = base
        if s.kind != 'Z':
            by_serial[s.serial] = s
    print('\nKeys and deltas (a delta only against a key, %d%% limit): %d keys of %d '
          'snapshots, %d copies; %d of %d fz bytes stored (%s), allocations as '
          'rounded not counted' % (100 * limit, keys, len(snaps), copies, stored, tot,
                                    pct(stored, tot)))


def deltas(snaps):
    by_serial = {s.serial: s for s in snaps if s.kind != 'Z'}
    last_at, last_co = {}, {}
    recent = collections.defaultdict(list)
    res = collections.defaultdict(lambda: [0, 0, 0, 0, 0, 0])
    for s in snaps:
        size = s.fzbytes()
        cands = {
            'a predecessor buffer': by_serial.get(s.pred - 1) if s.pred else None,
            'b same code, same position': last_at.get((s.co, s.pos)),
            'c same code, nearest earlier': last_co.get(s.co),
        }
        best = None
        for name, b in sorted(cands.items()):
            r = res[name]
            if b is None or b is s:
                r[0] += 1
                r[1] += size
                continue
            c, e = delta_cost(b, s)
            c += 8  # the base's address
            c = min(c, size)
            r[2] += 1
            r[3] += size
            r[4] += c
            r[5] += e <= 4
            if best is None or c < best:
                best = c
        r = res['d best of a-c and the 8 latest of the same code']
        for b in recent[s.co][-8:]:
            c, _ = delta_cost(b, s)
            c = min(c + 8, size)
            best = c if best is None else min(best, c)
        if best is None:
            r[0] += 1
            r[1] += size
        else:
            r[2] += 1
            r[3] += size
            r[4] += best
        last_at[(s.co, s.pos)] = s
        last_co[s.co] = s
        recent[s.co].append(s)
    tot = sum(s.fzbytes() for s in snaps)
    rows = []
    for name in sorted(res):
        r = res[name]
        rows.append([name, r[2], r[3], r[4], pct(r[4], r[3]), r[0],
                     pct(r[4] + r[1], tot), pct(r[5], r[2]) if name[0] != 'd' else '-'])
    show('Deltas as copy/insert edits of whole items (with 8 bytes for the base)',
         rows, ['base', 'with base', 'their bytes', 'as deltas', 'of theirs', 'no base',
                'all, of fz', '<=4 edits'])


def best_table(count, size, code, k, scale):
    """the k fragments that save most, net of their place in the table"""
    def gain(f):
        return (size[f] - code) * count[f] * scale - size[f]
    top = sorted(count, key=lambda f: -gain(f))[:k]
    return {f for f in top if gain(f) > 0}


def parse(snaps, table, code):
    """greedy longest-first: per entry, its uses and the bytes they save"""
    lens = sorted({len(f) for f in table}, reverse=True)
    used = collections.defaultdict(lambda: [0, 0])
    for s in snaps:
        t = s.tokens
        i = 0
        while i < len(t):
            for n in lens:
                f = tuple(t[i:i + n])
                if f in table:
                    u = used[f]
                    u[0] += 1
                    u[1] += sum(s.toksize[i:i + n]) - code
                    i += n
                    break
            else:
                i += 1
    return used


def fragments(snaps, ks=(16, 256, 4096)):
    """top-K tables: whole sub-arrays, and runs of n items (n-grams of the
    item stream), each occurrence replaced by an escape byte and the index
    (2 bytes up to 256 entries, 3 up to 65536), the table stored once.
    Chosen on the first half of the snapshots taken, measured on the
    second, as a table a process built while compiling would be: the
    arguments are addresses, so no table can be made before the run."""
    train, test = snaps[:len(snaps) // 2], snaps[len(snaps) // 2:]
    tot = sum(s.fzbytes() for s in test)
    rows = []
    # whole sub-arrays
    spans = {s.idx: tree(s) or [] for s in snaps}
    sub = collections.Counter()
    for s in train:
        for a, b in spans[s.idx]:
            sub[tuple(s.tokens[a:b])] += 1
    size = {f: sum(len(t[0]) + 8 * (len(t) - 1) for t in f) for f in sub}

    def apply_sub(ss, table, code):
        used = collections.defaultdict(lambda: [0, 0])
        for s in ss:
            taken_to = -1
            for a, b in sorted(spans[s.idx]):  # outermost first
                if a < taken_to:
                    continue
                f = tuple(s.tokens[a:b])
                if f in table:
                    used[f][0] += 1
                    used[f][1] += sum(s.toksize[a:b]) - code
                    taken_to = b
        return used
    for k in ks:
        code = 2 if k <= 256 else 3
        table = best_table(sub, size, code, k, 1.0)
        used = apply_sub(train, table, code)
        table = {f for f in table if used[f][1] > size[f]}
        saved = sum(u[1] for u in apply_sub(test, table, code).values())
        saved -= sum(size[f] for f in table)
        rows.append(['whole sub-arrays, K=%d' % k, saved, pct(saved, tot)])
    # n-grams of the item stream, chosen on a sample, applied greedily
    rnd = random.Random(2)
    sample = rnd.sample(train, min(len(train), 4000))
    cnt = collections.Counter()
    ns = (2, 3, 4, 6, 8, 12, 16, 24, 32, 48)
    for s in sample:
        t = s.tokens
        for n in ns:
            end = {}
            for i in range(len(t) - n + 1):
                f = tuple(t[i:i + n])
                if end.get(f, 0) <= i:  # occurrences that don't overlap
                    cnt[f] += 1
                    end[f] = i + n
    scale = len(train) / len(sample)
    gsize = {f: sum(len(t[0]) + 8 * (len(t) - 1) for t in f) for f in cnt}
    for k in ks:
        code = 2 if k <= 256 else 3
        table = best_table(cnt, gsize, code, k, scale)
        for _ in range(2):  # the second time without entries that didn't pay
            used = parse(train, table, code)
            table = {f for f in table if used[f][1] > gsize[f]}
        saved = sum(u[1] for u in parse(test, table, code).values())
        saved -= sum(gsize[f] for f in table)
        rows.append(['runs of 2-48 items, K=%d' % k, saved, pct(saved, tot)])
    # opcode bytes alone: what a table of opcode n-grams could save at most
    oc = collections.Counter()
    for s in sample:
        for n in (2, 4, 8):
            for i in range(len(s.opc) - n + 1):
                oc[s.opc[i:i + n]] += 1
    opc_tot = sum(len(s.opc) for s in snaps)
    show('Fragment tables, chosen on the first half, on the second (net of the '
         'table; %d fz bytes; all %d snapshots have %d opcode bytes)' %
         (tot, len(snaps), opc_tot), rows, ['scheme', 'bytes saved', 'of fz'])
    top = oc.most_common(5)
    print('  most frequent opcode runs (sample of %d): ' % len(sample) +
          ', '.join('%s x%d' % (list(struct.unpack('%db' % len(b), b)), c) for b, c in top))


def prefixes(snaps):
    """common prefix and suffix of the stored streams with the predecessor"""
    by_serial = {s.serial: s for s in snaps if s.kind != 'Z'}
    n = p_opc = s_opc = p_arg = s_arg = tot = same_pyc = 0
    for s in snaps:
        b = by_serial.get(s.pred - 1) if s.pred else None
        if b is None:
            continue
        n += 1
        tot += s.fzbytes()
        same_pyc += s.pyc == b.pyc

        def lcp(x, y):
            i = 0
            m = min(len(x), len(y))
            while i < m and x[i] == y[i]:
                i += 1
            return i
        # the read order starts at the stack end; the allocation's two ends
        # are the first vlocals (write order)
        p_opc += lcp(s.opc, b.opc)
        s_opc += lcp(s.opc[::-1], b.opc[::-1])
        p_arg += 8 * lcp(s.args, b.args)
        s_arg += 8 * lcp(s.args[::-1], b.args[::-1])
    if n:
        print('\nShared with the predecessor (%d snapshots, %d fz bytes): read-order '
              'prefix %s of opcodes and %s of arguments; prefix in write order (the '
              'first vlocals) %s and %s' % (
                  n, tot, pct(p_opc, tot), pct(p_arg, tot), pct(s_opc, tot), pct(s_arg, tot)))
        print('  pyc_data the same as the predecessor\'s: %s' % pct(same_pyc, n))


def analyse(paths, buffers):
    every = []
    for path in paths:
        snaps = load(path)
        print('=' * 78 + '\n%s: %d snapshots' % (os.path.basename(path), len(snaps)))
        report(snaps, buffers if len(paths) == 1 else None)
        every.append(snaps)
    if len(paths) > 1:
        allsnaps = [s for ss in every for s in ss]
        print('=' * 78 + '\nall files: %d snapshots' % len(allsnaps))
        by_kind(allsnaps, None)
        distribution(allsnaps)
        compressors(allsnaps)


def report(snaps, buffers):
    by_kind(snaps, buffers)
    distribution(snaps)
    duplicates(snaps)
    entropy(snaps)
    compressors(snaps)
    prefixes(snaps)
    tails(snaps)
    deltas(snaps)
    keyframes(snaps)
    keyframes(snaps, 0.25)
    fragments(snaps)


# ------------------------------------------------------------------- corpus

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
                  os.path.join(ROOT, 'tools', 'pyperformance', 'bm_%s.py' % n), '1', '0'])
             for n in ('chaos', 'go', 'deltablue')]
    jobs.append(('memrepro', [os.path.join(ROOT, 'tools', 'memrepro.py'), ROOT, '500']))
    os.makedirs(outdir, exist_ok=True)

    def limit():
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (4000000000, 4000000000))
    running = []
    for name, args in jobs + [None, None]:
        if name is not None:
            out = os.path.join(outdir, name + '.snap')
            if os.path.exists(out):
                os.unlink(out)
            env = dict(os.environ, NI_SNAPDUMP=out, PYTHONHASHSEED='0')
            log = open(os.path.join(outdir, name + '.out'), 'w')
            running.append((name, subprocess.Popen([py] + args, env=env, cwd=ROOT,
                                                   stdout=log, stderr=log,
                                                   preexec_fn=limit)))
        while running and (len(running) >= 2 or name is None):
            n, p = running.pop(0)
            print(n, p.wait(timeout=900), flush=True)
            if name is None:
                break


if __name__ == '__main__':
    if sys.argv[1:2] == ['dump']:
        dump(sys.argv[2])
    elif sys.argv[1:2] == ['analyse']:
        a = sys.argv[2:]
        b = None
        if '--buffers' in a:
            i = a.index('--buffers')
            b = int(a[i + 1])
            del a[i:i + 2]
        analyse(a, b)
    else:
        raise SystemExit(__doc__)
