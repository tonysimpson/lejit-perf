"""Which str objects a program makes and how each is used before it dies
(FINDINGS.md "Virtual strings: design study").

Plain CPython 3.7 only (not Ni): a sys.settrace tracer with opcode events
reads the value stack through the frame (ceval sets f_stacktop before each
opcode event), so it sees every instruction's inputs and, at the next
event, its outputs. A str an instruction leaves with a reference count of
1 is fresh (made by it, not cached or shared); so is an item of a list
that a str method (split...) made, taken by FOR_ITER, UNPACK_SEQUENCE or
an index. Each fresh str is followed by address until its address is
reused by another fresh str, which means it died. Every instruction that
takes it as an input is a use, classified by opcode and the other
operands (a dict key, a str method's receiver, a store...). Uses are
'virtual' (an operation a virtual string could serve: a str method on it,
a slice or index, compare, 'in', a dict lookup, a dict store over an
existing key, len, concatenation, formatting, truth), 'neutral' (stack and
local moves) or escapes (a store into anything, a new dict key, return,
yield, an argument to a Python function or another C function, 'is').
A str with no escape died unseen: a virtual string never materialised
would have done. Its parent is the fresh str the instruction that made it
read (the receiver of a str method, the sliced string); chains are runs of
non-escaping parents.

    python3.7 tools/strstudy.py bench word_counts 300
    python3.7 tools/strstudy.py pyperf tools/pyperformance/bm_go.py
    python3.7 tools/strstudy.py adv str_methods 14
    python3.7 tools/strstudy.py ... --budget 3000000 --json OUT

Tracing stops after --budget opcode events (default 3M). Counts are per
1,000 opcode events traced; about 250,000 events a second.
"""
import collections
import ctypes
import dis
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_vp = ctypes.c_void_p
_ss = ctypes.c_ssize_t
_u32 = ctypes.c_uint32


def rd(a):
    return _vp.from_address(a).value or 0


def obj(p):
    return ctypes.cast(p, ctypes.py_object).value


T_STR, T_DICT, T_LIST, T_TUPLE, T_SLICE, T_SET = (
    id(str), id(dict), id(list), id(tuple), id(slice), id(set))
T_MDESCR = id(type(str.strip))
T_BUILTIN = id(type(len))
T_FUNC = id(types.FunctionType)
T_METHOD = id(types.MethodType)
T_LISTITER = id(type(iter([])))
T_TUPLEITER = id(type(iter(())))
T_STRITER = id(type(iter('')))
SPLITTERS = {'split', 'rsplit', 'splitlines', 'partition', 'rpartition'}
CMP = dis.cmp_op
OPNAME = dis.opname
# what a virtual string's receiver could serve (C helpers on a view)
VMETHODS = {'strip', 'lstrip', 'rstrip', 'lower', 'upper', 'casefold',
            'startswith', 'endswith', 'find', 'rfind', 'index', 'rindex',
            'count', 'split', 'rsplit', 'partition', 'rpartition',
            'splitlines', 'isdigit', 'isalpha', 'isspace', 'isalnum',
            'isupper', 'islower', 'isdecimal', 'isnumeric', 'isidentifier',
            'replace', 'join', 'encode', 'title', 'capitalize', 'swapcase',
            'zfill', 'ljust', 'rjust', 'center', 'format', 'translate',
            '__contains__', '__len__', '__getitem__', '__eq__', '__hash__'}
VIRTUAL = {'slice', 'index', 'compare', 'contains', 'dict-get',
           'dict-set-existing', 'len', 'truth', 'drop', 'concat',
           'build-string', 'format-arg', 'format', 'ord', 'hash', 'iterate',
           'typetest', 'convert', 'repeat', 'method', 'method-arg'}
NEUTRAL = {'local', 'stack', 'str()'}


def inout(name, arg):
    """(inputs popped, outputs pushed) of an opcode, 3.7"""
    if name in ('POP_TOP', 'PRINT_EXPR', 'RETURN_VALUE', 'STORE_FAST',
                'STORE_NAME', 'STORE_GLOBAL', 'STORE_DEREF',
                'POP_JUMP_IF_TRUE', 'POP_JUMP_IF_FALSE', 'LIST_APPEND',
                'SET_ADD', 'DELETE_ATTR'):
        return 1, 0
    if name in ('ROT_TWO',):
        return 2, 2
    if name in ('ROT_THREE',):
        return 3, 3
    if name == 'DUP_TOP':
        return 1, 2
    if name == 'DUP_TOP_TWO':
        return 2, 4
    if name.startswith('UNARY_') or name in ('GET_ITER', 'LOAD_ATTR',
                                             'YIELD_VALUE',
                                             'GET_YIELD_FROM_ITER'):
        return 1, 1
    if name.startswith(('BINARY_', 'INPLACE_')) or name == 'COMPARE_OP':
        return 2, 1
    if name == 'STORE_SUBSCR':
        return 3, 0
    if name in ('DELETE_SUBSCR', 'STORE_ATTR', 'MAP_ADD'):
        return 2, 0
    if name == 'UNPACK_SEQUENCE':
        return 1, arg
    if name == 'UNPACK_EX':
        return 1, (arg & 0xff) + (arg >> 8) + 1
    if name == 'LOAD_METHOD':
        return 1, 2
    if name.startswith('LOAD_'):
        return 0, 1
    if name in ('BUILD_TUPLE', 'BUILD_LIST', 'BUILD_SET', 'BUILD_STRING',
                'BUILD_SLICE') or name.endswith('_UNPACK'):
        return arg, 1
    if name == 'BUILD_MAP':
        return 2 * arg, 1
    if name == 'BUILD_CONST_KEY_MAP':
        return arg + 1, 1
    if name == 'FOR_ITER':
        return 1, 1          # the iterator stays below the item
    if name in ('JUMP_IF_TRUE_OR_POP', 'JUMP_IF_FALSE_OR_POP'):
        return 1, 0
    if name == 'CALL_FUNCTION':
        return arg + 1, 1
    if name == 'CALL_FUNCTION_KW':
        return arg + 2, 1
    if name == 'CALL_FUNCTION_EX':
        return 2 + (arg & 1), 1
    if name == 'CALL_METHOD':
        return arg + 2, 1
    if name == 'FORMAT_VALUE':
        return 1 + (1 if arg & 4 else 0), 1
    return 0, 0


PRODUCERS = {'CALL_FUNCTION', 'CALL_FUNCTION_KW', 'CALL_FUNCTION_EX',
             'CALL_METHOD', 'FORMAT_VALUE', 'BUILD_STRING', 'FOR_ITER',
             'UNPACK_SEQUENCE', 'UNPACK_EX', 'GET_ITER'}


class Rec:
    __slots__ = ('kind', 'length', 'state', 'parent', 'uses', 'children',
                 'returned', 'site')

    def __init__(self, kind, length, state, parent, site):
        self.kind = kind
        self.length = length
        self.state = state
        self.parent = parent
        self.uses = []
        self.children = 0
        self.returned = False
        self.site = site


def callee_name(c):
    if c is None:
        return '?'
    n = getattr(c, '__qualname__', None) or getattr(c, '__name__', None)
    return n or type(c).__name__


class Study:
    def __init__(self, budget):
        self.budget = budget
        self.events = 0
        self.live = {}            # address -> Rec
        self.dead = []
        self.pending = {}         # frame id -> (name, arg, offset, ctx)
        self.splitlists = {}      # list/tuple address -> site
        self.splititers = {}      # iterator address -> site
        self.unchanged = collections.Counter()   # str method gave self
        self.nonfresh = collections.Counter()    # str outputs not fresh
        self.subclass = collections.Counter()
        self.splitlist_uses = collections.Counter()
        self.codes = {}

    # -- the stack -------------------------------------------------------
    @staticmethod
    def stack(frame, n):
        """the top n slots, TOS first"""
        top = rd(id(frame) + 72)
        if not top:
            return None
        return [rd(top - 8 * (i + 1)) for i in range(n)]

    def instr(self, code, off):
        c = self.codes.get(code)
        if c is None:
            c = self.codes[code] = code.co_code
        op = c[off]
        arg = c[off + 1]
        k = off
        while k >= 2 and c[k - 2] == dis.EXTENDED_ARG:
            k -= 2
            arg |= c[k + 1] << (8 * ((off - k) // 2))
        return OPNAME[op], arg

    # -- tracer ----------------------------------------------------------
    def globaltrace(self, frame, event, arg):
        if event == 'call':
            frame.f_trace_opcodes = True
            frame.f_trace_lines = False
            return self.trace
        return None

    def trace(self, frame, event, arg):
        fid = id(frame)
        if event == 'opcode':
            self.events += 1
            if self.events >= self.budget:
                sys.settrace(None)
                frame.f_trace = None
                return None
            p = self.pending.pop(fid, None)
            if p is not None:
                self.outputs(frame, p)
            name, oparg = self.instr(frame.f_code, frame.f_lasti)
            n_in, n_out = inout(name, oparg)
            ins = self.stack(frame, n_in) if n_in else []
            ctx = self.inputs(name, oparg, ins) if ins else None
            if name in PRODUCERS or (n_out == 1 and name.startswith(
                    ('BINARY_', 'INPLACE_'))):
                self.pending[fid] = (name, oparg, frame.f_lasti, n_out, ctx,
                                     frame.f_code)
        elif event in ('exception', 'return'):
            self.pending.pop(fid, None)
        return self.trace

    # -- inputs: uses ------------------------------------------------------
    def use(self, p, what):
        r = self.live.get(p)
        if r is None:
            return None
        if rd(p + 8) != T_STR or _ss.from_address(p + 16).value != r.length:
            self.finish(p)
            return None
        r.uses.append(what)
        return r

    def inputs(self, name, arg, ins):
        live = self.live
        hit = [p for p in ins if p in live]
        sl = [p for p in ins if p in self.splitlists]
        ctx = {}
        if sl:
            for p in sl:
                self.splitlist_uses[name] += 1
            if name == 'GET_ITER':
                ctx['splititer'] = self.splitlists[ins[0]]
            elif name in ('UNPACK_SEQUENCE', 'UNPACK_EX'):
                ctx['splititem'] = self.splitlists[ins[0]]
            elif name == 'BINARY_SUBSCR' and ins[1] in self.splitlists:
                ctx['splititem'] = self.splitlists[ins[1]]
        if name == 'FOR_ITER' and ins[0] in self.splititers:
            ctx['splititem'] = self.splititers[ins[0]]
        if name in ('CALL_METHOD', 'CALL_FUNCTION', 'CALL_FUNCTION_KW'):
            ctx.update(self.call_ctx(name, arg, ins))
        if not hit:
            return ctx
        t = [rd(p + 8) if p else 0 for p in ins]
        parents = []
        for pos, p in enumerate(ins):
            if p not in live:
                continue
            w = self.classify(name, arg, pos, ins, t, ctx)
            r = self.use(p, w)
            if r is not None and w in ('method', 'slice', 'index', 'concat',
                                       'format', 'build-string', 'repeat'):
                parents.append(r)
        if parents:
            ctx['parent'] = parents[0]
        return ctx

    def call_ctx(self, name, arg, ins):
        if name == 'CALL_METHOD':
            m = ins[arg + 1]
            if m:
                return {'callee': obj(m), 'selfpos': arg}
            return {'callee': obj(ins[arg]) if ins[arg] else None,
                    'selfpos': None}
        if name == 'CALL_FUNCTION':
            return {'callee': obj(ins[arg]) if ins[arg] else None,
                    'selfpos': None}
        return {'callee': obj(ins[arg + 1]) if ins[arg + 1] else None,
                'selfpos': None}

    def classify(self, name, arg, pos, ins, t, ctx):
        if name in ('STORE_FAST', 'LOAD_METHOD'):
            return 'local'
        if name in ('DUP_TOP', 'DUP_TOP_TWO', 'ROT_TWO', 'ROT_THREE'):
            return 'stack'
        if name == 'POP_TOP':
            return 'drop'
        if name in ('POP_JUMP_IF_TRUE', 'POP_JUMP_IF_FALSE',
                    'JUMP_IF_TRUE_OR_POP', 'JUMP_IF_FALSE_OR_POP',
                    'UNARY_NOT'):
            return 'truth'
        if name == 'BINARY_SUBSCR':
            if pos == 1:
                return 'slice' if t[0] == T_SLICE else 'index'
            if t[1] == T_DICT:
                return 'dict-get'
            return 'escape-call'
        if name == 'STORE_SUBSCR':
            if pos == 0 and t[1] == T_DICT:
                try:
                    return ('dict-set-existing' if obj(ins[0]) in obj(ins[1])
                            else 'dict-insert')
                except Exception:
                    return 'escape-call'
            return 'store'
        if name == 'COMPARE_OP':
            op = CMP[arg]
            if op in ('in', 'not in'):
                if pos == 1:
                    if t[0] in (T_DICT, T_SET) or (
                            t[0] and issubclass(type(obj(ins[0])),
                                                (dict, set, frozenset))):
                        return 'dict-get'
                    if t[0] == T_STR:
                        return 'contains'
                    if t[0] in (T_LIST, T_TUPLE):
                        return 'contains'
                    return 'escape-call'
                return 'contains'
            if op in ('is', 'is not'):
                return 'identity'
            return 'compare'
        if name in ('BINARY_ADD', 'INPLACE_ADD'):
            if t[0] == T_STR and t[1] == T_STR:
                return 'concat'
            return 'escape-call'
        if name in ('BINARY_MODULO', 'INPLACE_MODULO'):
            return 'format' if pos == 1 else 'format-arg'
        if name in ('BINARY_MULTIPLY', 'INPLACE_MULTIPLY'):
            return 'repeat'
        if name == 'FORMAT_VALUE':
            return 'format-arg'
        if name == 'BUILD_STRING':
            return 'build-string'
        if name in ('GET_ITER', 'UNPACK_SEQUENCE', 'UNPACK_EX'):
            return 'iterate'
        if name == 'RETURN_VALUE':
            return 'return'
        if name == 'YIELD_VALUE':
            return 'yield'
        if name in ('STORE_ATTR', 'STORE_GLOBAL', 'STORE_NAME',
                    'STORE_DEREF', 'BUILD_TUPLE', 'BUILD_LIST', 'BUILD_SET',
                    'BUILD_MAP', 'BUILD_CONST_KEY_MAP', 'LIST_APPEND',
                    'SET_ADD', 'MAP_ADD', 'BUILD_SLICE'):
            return 'store'
        if name in ('CALL_METHOD', 'CALL_FUNCTION', 'CALL_FUNCTION_KW'):
            c = ctx.get('callee')
            sp = ctx.get('selfpos')
            tc = id(type(c))
            if tc == T_MDESCR and getattr(c, '__objclass__', None) is str:
                return 'method' if pos == sp else 'method-arg'
            if tc == T_BUILTIN:
                s = getattr(c, '__self__', None)
                cn = c.__name__
                if type(s) is str:
                    return 'method-arg'
                if isinstance(s, (dict, set, frozenset)):
                    if cn in ('get', '__contains__', '__getitem__'):
                        return 'dict-get'
                    if cn in ('pop', 'discard', 'remove'):
                        return 'dict-get'
                    return 'store'
                if isinstance(s, list):
                    return 'store' if cn in ('append', 'insert', 'extend',
                                             'remove', '__setitem__') \
                        else 'escape-call'
                if cn == 'len':
                    return 'len'
                if cn in ('ord', 'hash'):
                    return cn
                if cn in ('isinstance', 'issubclass', 'callable', 'type'):
                    return 'typetest'
                if cn in ('print',):
                    return 'io'
                return 'c-arg:' + cn
            if tc == T_MDESCR:
                oc = getattr(c, '__objclass__', None)
                if oc in (dict, set) and c.__name__ in (
                        'get', '__contains__', '__getitem__', 'pop',
                        'discard'):
                    return 'dict-get'
                if oc in (list, dict, set):
                    return 'store'
                return 'c-arg:%s.%s' % (getattr(oc, '__name__', '?'),
                                        c.__name__)
            if c in (int, float):
                return 'convert'
            if c is str:
                return 'str()'
            if c is type:
                return 'typetest'
            if tc in (T_FUNC, T_METHOD):
                return 'py-arg'
            if isinstance(c, type):
                return 'ctor-arg'
            return 'c-arg:' + callee_name(c)
        return 'other:' + name

    # -- outputs: fresh strs -----------------------------------------------
    def producer(self, name, arg, ctx, frame_code):
        if name in ('CALL_METHOD', 'CALL_FUNCTION', 'CALL_FUNCTION_KW'):
            c = ctx.get('callee')
            tc = id(type(c))
            if tc == T_MDESCR and getattr(c, '__objclass__', None) is str:
                return 'str.' + c.__name__
            if tc == T_BUILTIN and type(getattr(c, '__self__', None)) is str:
                return 'str.' + c.__name__
            if tc in (T_FUNC, T_METHOD):
                return 'py-return'
            return 'call:' + callee_name(c)
        if name == 'BINARY_SUBSCR':
            return 'slice/index'
        if name in ('BINARY_ADD', 'INPLACE_ADD'):
            return 'concat'
        if name in ('BINARY_MODULO', 'INPLACE_MODULO'):
            return '%-format'
        if name in ('BINARY_MULTIPLY', 'INPLACE_MULTIPLY'):
            return 'repeat'
        if name == 'FORMAT_VALUE':
            return 'format-value'
        if name == 'BUILD_STRING':
            return 'f-string'
        return name

    def outputs(self, frame, p):
        name, arg, off, n_out, ctx, code = p
        if name == 'FOR_ITER' and frame.f_lasti != off + 2:
            return
        if not n_out:
            return
        outs = self.stack(frame, n_out)
        if not outs:
            return
        ctx = ctx or {}
        if name == 'GET_ITER':
            if 'splititer' in ctx and outs[0]:
                self.splititers[outs[0]] = ctx['splititer']
            return
        split = ctx.get('splititem')
        for q in outs:
            if not q:
                continue
            tq = rd(q + 8)
            if tq != T_STR:
                if tq in (T_LIST, T_TUPLE) and name == 'CALL_METHOD':
                    c = ctx.get('callee')
                    if getattr(c, '__name__', None) in SPLITTERS and \
                            _ss.from_address(q).value == 1:
                        self.splitlists[q] = (code.co_name, off)
                elif tq and name != 'FOR_ITER' and name.startswith(
                        ('CALL', 'BINARY', 'FORMAT', 'BUILD')):
                    try:
                        if isinstance(obj(q), str):
                            self.subclass[self.producer(name, arg, ctx,
                                                        code)] += 1
                    except Exception:
                        pass
                continue
            rc = _ss.from_address(q).value
            ln = _ss.from_address(q + 16).value
            st = _u32.from_address(q + 32).value
            r = self.live.get(q)
            kind = 'split-item' if split else self.producer(name, arg, ctx,
                                                             code)
            if r is not None and r.length == ln:
                if r.uses and r.uses[-1] in ('return', 'yield'):
                    r.returned = True
                    continue
                if rc != 1 and not split:
                    if name == 'CALL_METHOD' and kind.startswith('str.'):
                        self.unchanged[kind] += 1
                    continue
            fresh = rc == 1 or (split and rc == 2 and (st & 3) == 0)
            if not fresh:
                if kind.startswith('str.') and ctx.get('parent') is not None \
                        and ctx['parent'] is self.live.get(q):
                    self.unchanged[kind] += 1
                else:
                    self.nonfresh[kind] += 1
                continue
            if r is not None:
                self.finish(q)
            parent = ctx.get('parent')
            if parent is not None:
                parent.children += 1
            self.live[q] = Rec(kind, ln, st, parent, (code.co_name, off))

    def finish(self, p):
        r = self.live.pop(p, None)
        if r is not None:
            self.dead.append(r)

    # -- report -------------------------------------------------------------
    def report(self, title):
        for p in list(self.live):
            self.finish(p)
        k = max(self.events, 1) / 1000.0
        recs = self.dead
        out = {'title': title, 'events': self.events, 'fresh': len(recs)}

        def fate(r):
            esc = [u for u in r.uses if u not in VIRTUAL and u not in NEUTRAL]
            if esc:
                return 'escape', esc[0]
            if all(u in NEUTRAL for u in r.uses):
                return 'unused', None
            return 'virtual', None

        fates = {}
        for r in recs:
            fates[id(r)] = fate(r)
        by_kind = collections.defaultdict(collections.Counter)
        esc_by = collections.Counter()
        last_use = collections.Counter()
        for r in recs:
            f, e = fates[id(r)]
            by_kind[r.kind][f] += 1
            if e:
                esc_by[e.split(':')[0] if not e.startswith('c-arg')
                       else e] += 1
            if f == 'virtual':
                vu = [u for u in r.uses if u not in NEUTRAL]
                last_use[vu[-1]] += 1

        def depth(r):
            d = 1
            q = r.parent
            while q is not None and fates[id(q)][0] != 'escape':
                d += 1
                q = q.parent
            return d
        chains = collections.Counter()
        for r in recs:
            if r.children == 0:
                chains[min(depth(r), 5)] += 1
        n = len(recs)
        nv = sum(1 for r in recs if fates[id(r)][0] != 'escape')
        lens = sorted(r.length for r in recs) or [0]
        ascii_ = sum(1 for r in recs if r.state & 0x40)
        in_chain = sum(1 for r in recs
                       if fates[id(r)][0] != 'escape' and (
                           r.children or (r.parent is not None and
                                          fates[id(r.parent)][0] != 'escape')))
        out.update({
            'fresh_per_k': n / k,
            'nonescaping_per_k': nv / k,
            'nonescaping_frac': nv / n if n else 0,
            'in_chain_frac_of_nonescaping': in_chain / nv if nv else 0,
            'median_len': lens[len(lens) // 2],
            'ascii_frac': ascii_ / n if n else 0,
            'by_kind': {kk: dict(v) for kk, v in sorted(
                by_kind.items(), key=lambda x: -sum(x[1].values()))[:14]},
            'escapes': dict(esc_by.most_common(12)),
            'virtual_last_use': dict(last_use.most_common(12)),
            'chain_len_at_end': dict(sorted(chains.items())),
            'unchanged': dict(self.unchanged.most_common(8)),
            'nonfresh': dict(self.nonfresh.most_common(8)),
            'subclass': dict(self.subclass),
            'splitlist_uses': dict(self.splitlist_uses),
        })
        return out


def show(o):
    print('== %s: %d events traced, %d fresh str (%.1f per 1k events), '
          '%.1f%% die unseen (%.1f per 1k), %.0f%% of those in chains; '
          'median length %d, ASCII %.0f%%' % (
              o['title'], o['events'], o['fresh'], o['fresh_per_k'],
              100 * o['nonescaping_frac'], o['nonescaping_per_k'],
              100 * o['in_chain_frac_of_nonescaping'], o['median_len'],
              100 * o['ascii_frac']))
    for key in ('by_kind', 'escapes', 'virtual_last_use', 'chain_len_at_end',
                'unchanged', 'nonfresh', 'subclass', 'splitlist_uses'):
        print('  %-18s %s' % (key, o[key]))


def run(title, fn, budget):
    s = Study(budget)
    f = sys._getframe()
    assert rd(id(f) + 32) == id(f.f_code), 'frame layout is not 3.7\'s'
    sys.settrace(s.globaltrace)
    try:
        fn()
    finally:
        sys.settrace(None)
    return s.report(title)


def main():
    a = sys.argv[1:]
    budget = 3000000
    jout = None
    if '--budget' in a:
        i = a.index('--budget')
        budget = int(a[i + 1])
        del a[i:i + 2]
    if '--json' in a:
        i = a.index('--json')
        jout = a[i + 1]
        del a[i:i + 2]
    mode = a[0]
    if mode == 'bench':
        sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import bench
        fn = dict((n, f) for n, f, _ in bench.BENCHMARKS)[a[1]]
        n = int(a[2])
        o = run(a[1], lambda: fn(n), budget)
    elif mode == 'pyperf':
        sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import pyperf37
        benches = pyperf37.load(a[1])

        def go():
            for _, timed in benches:
                timed(1)
        o = run(os.path.basename(a[1]), go, budget)
    elif mode == 'adv':
        d = os.path.join(ROOT, 'tools', 'adversarial')
        if not os.path.exists(os.path.join(d, a[1] + '.py')):
            d = os.path.join(d, 'round2')
        sys.path.insert(0, d)
        m = __import__(a[1])
        n = int(a[2])
        o = run(a[1], lambda: (m.main(n, 0), m.main(n, 1)), budget)
    else:
        raise SystemExit(__doc__)
    show(o)
    if jout:
        with open(jout, 'w') as f:
            json.dump(o, f, indent=1)


if __name__ == '__main__':
    main()
