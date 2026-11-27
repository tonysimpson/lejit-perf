"""The stand-in pyperf (tools/pyperf37.py:fake_pyperf) as a module, for the
processes a benchmark starts that import its script again, such as
multiprocessing's forkserver and spawn workers."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import pyperf37  # noqa: E402

Runner = pyperf37.Runner
perf_counter = pyperf37.time.perf_counter


def python_implementation():
    return 'cpython'
