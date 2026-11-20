"""Checks an NI_CODESTATS build (tools/samecode.py builds them): compiling
under the counters gives what CPython gives, and the counters record each
instruction's code.

    NI_MODULE_DIR=DIR python3.X tests/codestats.py

A function whose index -1 is folded while compiling, then compared and
sliced (enum's _is_sunder, which json's import compiles), crashed: the
counters left an error set while compiling, so the fold was refused the
first time and made in the respawn's replay. On a build without the
counters it says so and passes.
"""
import os
import sys

sys.path.insert(0, os.environ.get('NI_MODULE_DIR') or
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ni  # noqa: E402


def is_sunder(name):
    return (len(name) > 2 and
            name[0] == name[-1] == '_' and
            name[1:2] != '_' and
            name[-2:-1] != '_')


NAMES = ['_a_', '__x__', 'abc', '_x', '___', 'ab_c_', '_\xe9_', '_\U0001f600_']
EXPECTED = [True, False, False, False, False, False, True, True]


def main():
    if not ni.build_info['codestats']:
        print('ok   not a codestats build: nothing to check')
        return 0
    bad = 0
    for _ in range(5):
        got = [is_sunder(n) for n in NAMES]
        if got != EXPECTED:
            print('FAIL is_sunder gave %r' % got)
            bad += 1
            break
    import json  # noqa: F401  (enum, compiled)
    stats, lost, _, _ = ni._codestats()
    copies = [c for e in stats.values() for c in e[3]]
    if not all(isinstance(c[1], bytes) for c in copies) or not any(c[1] for c in copies):
        print('FAIL the counters recorded no code (%d copies)' % len(copies))
        bad += 1
    print('%-4s codestats: %d instructions, %d copies, %d bytes lost'
          % ('FAIL' if bad else 'ok', len(stats), len(copies), lost))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
