"""The mite benchmarks. Each calls mite's own classes and functions; only the
sockets are replaced, by objects whose recv/send are C callables where
possible (list.append, iterator __next__) so the stand-ins cost little.

Shape of the synthetic "large test":
  - 20 journeys x 5 transactions, 1-3 HTTP requests per transaction,
    http_metrics messages with every field mite_http sends;
  - 500 runners, 50 scenarios, ~50,000 concurrent journeys required;
  - all of mite's stats entry points registered, as a full install has them.
"""

import asyncio
import collections
import hashlib
import itertools
import os
import random
import types

import msgpack

import mite
import mite.zmq as mzmq
from mite.collector import Collector
from mite.config import ConfigManager
from mite.context import Context
from mite.controller import Controller
from mite.datapools import IterableDataPool, RecyclableIterableDataPool
from mite.runner import Runner
from mite.scenario import ScenarioManager
from mite.stats import Stats
from mite.utils import pack_msg, unpack_msg
from mite.volume_model import Constant
from mite.web.prometheus import PrometheusMetrics

import mitejourneys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
POOL = os.path.join(DATA, "pool.msgpack")
DUMPS = os.path.join(DATA, "dumps.msgpack")
TEST = "acme_peak_nft"
N_RUNNERS = int(os.environ.get("MITE_BENCH_RUNNERS", "500"))
N_SCENARIOS = 50


def _h():
    return hashlib.sha256()


def _fake_socket(**kw):
    return types.SimpleNamespace(**kw)


def _sender():
    """A real mite.zmq.Sender (pack_msg per message); its socket appends."""
    s = mzmq.Sender()
    out = []
    s._socket.close(linger=0)
    s._socket = _fake_socket(send=out.append)
    return s, out


def _receiver(listeners=(), raw_listeners=()):
    r = mzmq.Receiver(listeners=list(listeners), raw_listeners=list(raw_listeners))
    r._socket.close(linger=0)
    return r


def _stop_after(n):
    """A C-level stop_func: false n times, then true."""
    return itertools.chain(itertools.repeat(False, n), itertools.repeat(True)).__next__


def _load(path):
    with open(path, "rb") as f:
        return msgpack.unpackb(f.read(), use_list=True, raw=False,
                               strict_map_key=False)


# --------------------------------------------------------------------------
# data generation (run once, plain CPython)

def _run_sync(coro):
    try:
        coro.send(None)
    except StopIteration as e:
        return e.value
    raise RuntimeError("journey suspended")


def _controller_report(i):
    rng = random.Random(i)
    req = {s: 200 + (s * 37) % 1800 for s in range(1, N_SCENARIOS + 1)}
    return {
        "type": "controller_report",
        "time": 1763700000.0 + i,
        "test": TEST,
        "required": req,
        "actual": {s: v - rng.randrange(20) for s, v in req.items()},
        "num_runners": N_RUNNERS,
    }


def generate():
    os.makedirs(DATA, exist_ok=True)
    mitejourneys.reset()
    sender, out = _sender()
    runner = Runner(None, sender.send)
    config = {}
    ctx_ids = collections.Counter()
    pool = []
    i = 0
    while len(pool) < 30000:
        jno = i % mitejourneys.N_JOURNEYS
        rid = 1 + (i * 7) % N_RUNNERS
        ctx_ids[rid] += 1
        spec = mitejourneys.JOURNEY_SPECS[jno]
        ctx = Context(sender.send, config, id_data={
            "test": TEST, "runner_id": rid, "journey": spec,
            "context_id": ctx_ids[rid], "scenario_id": jno + 1,
            "scenario_data_id": None}, should_stop_func=runner.should_stop)
        _run_sync(runner._execute(ctx, jno + 1, None, spec, None))
        pool.extend(out)
        out.clear()
        i += 1
        if i % 200 == 0:     # a controller report about once a second's worth
            pool.append(pack_msg(_controller_report(i)))
    with open(POOL, "wb") as f:
        f.write(msgpack.packb(pool, use_bin_type=True))
    # stats dumps, as one stats process sends them (2500 messages per 0.25 s)
    stats_sender, dumps = _sender()
    stats = Stats(sender=stats_sender.send)
    stats.dump_timeout = float("inf")
    for k in range(0, len(pool), 2500):
        for raw in pool[k:k + 2500]:
            stats.process(unpack_msg(raw))
        stats_sender.send(stats.dump())
    with open(DUMPS, "wb") as f:
        f.write(msgpack.packb(dumps, use_bin_type=True))
    types_ = collections.Counter(unpack_msg(r)["type"] for r in pool)
    print(f"pool: {len(pool)} messages, {sum(map(len, pool))/len(pool):.0f} bytes "
          f"average, {dict(types_)}; {len(dumps)} stats dumps; "
          f"{len(stats._all_stats)} stats processors")


# --------------------------------------------------------------------------

class Bench:
    check_ops = 0

    def run(self, clock):
        raise NotImplementedError

    def digest(self):
        return self.h.hexdigest()


class Codec(Bench):
    """unpack_msg + pack_msg of each runner message (msgpack, in C)."""
    BATCH = 3000
    check_ops = 30000

    def __init__(self, check):
        self.pool = _load(POOL)
        self.h = _h()
        self.check = check

    def run(self, clock):
        src = itertools.cycle(self.pool)
        while not clock.done:
            outs = [pack_msg(unpack_msg(raw)) for raw in itertools.islice(src, self.BATCH)]
            if self.check:
                for o in outs:
                    self.h.update(o)
            clock.tick(self.BATCH)


def _strip_times(msg):
    return {k: v for k, v in msg.items()
            if k not in ("time", "start_time", "end_time")}


class ContextSend(Bench):
    """The runner's per-journey work without the event loop: Context,
    Runner._execute, transaction() and Context.send, pack_msg per message."""
    BATCH = 100      # journeys
    check_ops = 20000

    def __init__(self, check):
        mitejourneys.reset()
        self.sender, self.out = _sender()
        self.runner = Runner(None, self.sender.send)
        self.h = _h()
        self.check = check

    def run(self, clock):
        runner, out, send = self.runner, self.out, self.sender.send
        config = {}
        ids = itertools.count(1)
        specs = mitejourneys.JOURNEY_SPECS
        i = 0
        while not clock.done:
            for _ in range(self.BATCH):
                jno = i % 20
                ctx = Context(send, config, id_data={
                    "test": TEST, "runner_id": 1, "journey": specs[jno],
                    "context_id": next(ids), "scenario_id": jno + 1,
                    "scenario_data_id": None}, should_stop_func=runner.should_stop)
                _run_sync(runner._execute(ctx, jno + 1, None, specs[jno], None))
                i += 1
            n = len(out)
            if self.check:
                for raw in out:
                    self.h.update(repr(_strip_times(unpack_msg(raw))).encode())
            out.clear()
            clock.tick(n)


class RunnerLoop(Bench):
    """mite.runner.Runner.run() under uvloop, talking to a real Controller
    through mite.cli.test's DirectRunnerTransport (as `mite scenario test`
    does); journeys yield to the loop once per request. Ops = messages."""
    check_ops = 60000

    def __init__(self, check):
        import uvloop
        from mite.cli.test import DirectRunnerTransport
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        mitejourneys.reset()
        mitejourneys.SLEEP = True
        random.seed(1)
        self.check = check
        cm = ConfigManager()
        sm = ScenarioManager(spawn_rate=None, config_manager=cm)
        for j, spec in enumerate(mitejourneys.JOURNEY_SPECS):
            if check:   # a fixed number of journeys: the pools run dry
                dp = IterableDataPool([()] * 300)
            else:
                dp = IterableDataPool(itertools.repeat(()))
            sm.add_scenario(spec, dp, Constant(duration=10**9, tps=100))
        self.sm = sm
        self.controller = Controller(TEST, sm, cm)
        self.sender, self.out = _sender()
        self.runner = Runner(DirectRunnerTransport(self.controller), self.sender.send,
                             loop_wait_max=1.0)
        self.counts = collections.Counter()

    def run(self, clock):
        out = self.out

        async def ticker():
            while True:
                await asyncio.sleep(0.05)
                n = len(out)
                if self.check:
                    for raw in out:
                        m = unpack_msg(raw)
                        self.counts[(m["type"], m["journey"], m.get("transaction"),
                                     m.get("had_error"), m.get("response_code"))] += 1
                out.clear()
                if clock.tick(n) and not self.check:
                    self.sm._scenarios.clear()     # as StopVolumeModel would
                    return

        async def main():
            t = asyncio.create_task(ticker())
            await self.runner.run()
            t.cancel()

        asyncio.run(main())
        n = len(out)
        if self.check:
            for raw in out:
                m = unpack_msg(raw)
                self.counts[(m["type"], m["journey"], m.get("transaction"),
                             m.get("had_error"), m.get("response_code"))] += 1
        clock.tick(n)

    def digest(self):
        h = _h()
        h.update(repr(sorted(self.counts.items(), key=repr)).encode())
        return h.hexdigest()


class _FakeRunners:
    """The REP socket of a ControllerServer, with N_RUNNERS runners behind
    it. Each request reports that a quarter of each scenario's journeys
    finished (checking their datapool items back in) and asks for more."""

    def __init__(self, clock, h, n_runners, specs, dp_sids):
        self.clock, self.h = clock, h
        self.n = n_runners
        self.work = [None] * (n_runners + 1)     # runner -> {sid: count}
        self.dpids = [None] * (n_runners + 1)    # runner -> {sid: deque}
        self.dp_sids = dp_sids
        self.order = itertools.cycle(range(n_runners))
        self.ids = {}                             # slot -> runner id
        self.cur = None
        self.handled = 0

    def recv(self):
        slot = next(self.order)
        self.cur = slot
        if slot not in self.ids:
            return pack_msg((1, None))
        rid = self.ids[slot]
        work = self.work[rid]
        dpids = self.dpids[rid]
        completed = []
        for sid in list(work):
            c = work[sid]
            done = (c + 3) >> 2
            if sid in dpids:
                q = dpids[sid]
                for _ in range(done):
                    completed.append((sid, q.popleft()))
            if c - done:
                work[sid] = c - done
            else:
                del work[sid]
        return pack_msg((2, [rid, work, completed, None]))

    def send(self, raw):
        self.handled += 1
        if self.h is not None:
            self.h.update(raw)
        reply = unpack_msg(raw)
        slot = self.cur
        if slot not in self.ids:
            rid = reply[0]
            self.ids[slot] = rid
            self.work[rid] = {}
            self.dpids[rid] = collections.defaultdict(collections.deque)
        else:
            rid = self.ids[slot]
            work = self.work[rid]
            dpids = self.dpids[rid]
            for sid, dpid, spec, args in reply[0]:
                work[sid] = work.get(sid, 0) + 1
                if dpid is not None:
                    dpids[sid].append(dpid)
        if self.handled % 250 == 0:
            self.clock.tick(250)


class ControllerBench(Bench):
    """mite.zmq.ControllerServer.run() and the controller_report loop from
    mite.__main__.controller, under uvloop, serving 500 runners on 50
    scenarios (10 with datapools). Ops = requests answered."""
    check_ops = 15000

    def __init__(self, check):
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        random.seed(1)
        self.check = check
        self.h = _h() if check else None
        cm = ConfigManager()
        cm.set("base_url", "https://svc.example.test")
        sm = ScenarioManager(spawn_rate=None, config_manager=cm)
        self.dp_sids = set()
        for s in range(1, N_SCENARIOS + 1):
            spec = mitejourneys.JOURNEY_SPECS[s % 20]
            dp = None
            if s % 5 == 0:
                dp = RecyclableIterableDataPool([(f"user{k}", "pw") for k in range(20000)])
                self.dp_sids.add(s)
            sm.add_scenario(spec, dp, Constant(duration=10**9, tps=200 + (s * 37) % 1800))
        self.controller = Controller(TEST, sm, cm)
        self.server = mzmq.ControllerServer("inproc://mite-bench-controller")
        self.report_sender, self.reports = _sender()

    def run(self, clock):
        controller, sender = self.controller, self.report_sender
        fake = _FakeRunners(clock, self.h, N_RUNNERS, None, self.dp_sids)
        self.server._sock.close(linger=0)
        self.server._sock = fake

        # copied from mite.__main__.controller (a closure there)
        async def controller_report():
            while True:
                if controller.should_stop():
                    return
                await asyncio.sleep(1)
                controller.report(sender.send)

        async def main():
            rep = asyncio.create_task(controller_report())
            await self.server.run(controller, lambda: clock.done)
            rep.cancel()

        asyncio.run(main())


class StatsBench(Bench):
    """mite stats: Receiver._run (unpack_msg) -> Stats.process with every
    registered processor; dumps packed and sent every 0.25 s."""
    BATCH = 2500
    check_ops = 30000
    include = None

    def __init__(self, check):
        self.pool = _load(POOL)
        self.check = check
        self.sender, self.out = _sender()
        include = self.include.split(",") if self.include else None
        self.stats = Stats(sender=self.sender.send, include=include)
        if check:
            # dump at fixed points, not every 0.25 s, so output is comparable
            self.stats.dump_timeout = float("inf")
        self.recv = _receiver(listeners=[self.stats.process])
        self.prom = PrometheusMetrics()

    def run(self, clock):
        self.recv._socket = _fake_socket(recv=itertools.cycle(self.pool).__next__)
        while not clock.done:
            self.recv._run(_stop_after(self.BATCH))
            if self.check:
                self.sender.send(self.stats.dump())
                for raw in self.out:
                    self.prom.process(unpack_msg(raw))
            self.out.clear()
            clock.tick(self.BATCH)

    def digest(self):
        h = _h()
        h.update(self.prom.format().encode())
        return h.hexdigest()


class StatsHttpOnly(StatsBench):
    """As stats, but only the mite and mite_http processors loaded."""
    include = "mite,mite_http"


class StatsQuiet(StatsBench):
    """WHAT-IF, not mite as shipped: as stats, but each matcher_by_type
    matcher replaced by the same test without its logger.debug f-string."""

    def __init__(self, check):
        super().__init__(check)
        for p in self.stats._all_stats:
            targets = p.matcher.__closure__[0].cell_contents

            def type_matcher(msg, targets=targets):
                return msg.get("type", None) in targets
            p.matcher = type_matcher


class CollectorBench(Bench):
    """mite collector: Receiver._run with Collector.process_raw_message as a
    raw listener (the receiver still unpacks every message), writing to
    rolling files of 100,000 messages."""
    BATCH = 5000
    check_ops = 30000
    use_json = False

    def __init__(self, check):
        import shutil
        import tempfile
        self.pool = _load(POOL)
        self.check = check
        base = os.path.join(HERE, "tmp")
        os.makedirs(base, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="collector-", dir=base)
        self._rm = lambda: shutil.rmtree(self.dir, ignore_errors=True)
        self.col = Collector(target_dir=self.dir, roll_after=100000,
                             use_json=self.use_json)
        self.recv = _receiver(raw_listeners=[self.col.process_raw_message])

    def run(self, clock):
        self.recv._socket = _fake_socket(recv=itertools.cycle(self.pool).__next__)
        try:
            while not clock.done:
                self.recv._run(_stop_after(self.BATCH))
                clock.tick(self.BATCH)
            self.col._current.flush()
            if self.check:
                h = _h()
                for fn in sorted(os.listdir(self.dir)):
                    if fn == "current" or fn[0].isdigit():
                        with open(os.path.join(self.dir, fn), "rb") as f:
                            h.update(f.read())
                self.h = h
        finally:
            self.col._current.close()
            self._rm()


class CollectorJson(CollectorBench):
    """As collector, with --collector-use-json (unpack + json.dumps)."""
    use_json = True


class DuplicatorBench(Bench):
    """mite.zmq.Duplicator._run: one PULL in, three PUSH out."""
    BATCH = 20000
    check_ops = 100000

    def __init__(self, check):
        self.pool = _load(POOL)
        self.dup = mzmq.Duplicator("inproc://mite-bench-dup-in",
                                   [f"inproc://mite-bench-dup-{i}" for i in range(3)])
        self.h = _h()

    def run(self, clock):
        self.dup._in_socket = _fake_socket(recv=itertools.cycle(self.pool).__next__)
        # str.format takes (msg, flags=...) and is C; the messages are dropped
        self.dup._out_sockets = [(a, _fake_socket(send="".format))
                                 for a, _ in self.dup._out_sockets]
        while not clock.done:
            self.dup._run(_stop_after(self.BATCH))
            clock.tick(self.BATCH)
        self.h.update(b"ok")


class PromUpdate(Bench):
    """mite prometheus_exporter: Receiver._run -> PrometheusMetrics.process
    for each stats dump. Ops = dumps."""
    BATCH = 20
    check_ops = 240

    def __init__(self, check):
        self.dumps = _load(DUMPS)
        self.prom = PrometheusMetrics()
        self.recv = _receiver(listeners=[self.prom.process])

    def run(self, clock):
        self.recv._socket = _fake_socket(recv=itertools.cycle(self.dumps).__next__)
        while not clock.done:
            self.recv._run(_stop_after(self.BATCH))
            clock.tick(self.BATCH)

    def digest(self):
        h = _h()
        h.update(self.prom.format().encode())
        return h.hexdigest()


class PromFormat(Bench):
    """PrometheusMetrics.format() of the registry a long test builds up (one
    scrape). Ops = scrapes."""
    check_ops = 20

    def __init__(self, check):
        self.prom = PrometheusMetrics()
        for raw in _load(DUMPS):
            self.prom.process(unpack_msg(raw))
        self.h = _h()
        self.check = check

    def run(self, clock):
        while not clock.done:
            text = self.prom.format()
            if self.check:
                self.h.update(text.encode())
            clock.tick(1)


class Pipeline(Bench):
    """End to end in one process: journeys send messages (Context.send ->
    Sender.send, packed) -> duplicated to the collector and to stats
    (Receiver._run, unpack) -> stats dumps -> prometheus exporter
    (process), scraped once a second. Ops = runner messages."""
    BATCH = 50     # journeys
    check_ops = 20000

    def __init__(self, check):
        mitejourneys.reset()
        self.check = check
        self.sender, self.out = _sender()
        self.runner = Runner(None, self.sender.send)
        b = CollectorBench(False)
        self.col, self._rm = b.col, b._rm
        self.col_recv = b.recv
        self.stats_sender, self.dumps = _sender()
        self.stats = Stats(sender=self.stats_sender.send)
        if check:
            self.stats.dump_timeout = float("inf")
        self.stats_recv = _receiver(listeners=[self.stats.process])
        self.prom = PrometheusMetrics()
        self.prom_recv = _receiver(listeners=[self.prom.process])
        self.h = _h()

    def run(self, clock):
        import time
        runner, out, send = self.runner, self.out, self.sender.send
        config = {}
        ids = itertools.count(1)
        specs = mitejourneys.JOURNEY_SPECS
        i = 0
        next_scrape = time.time() + 1
        try:
            while not clock.done:
                for _ in range(self.BATCH):
                    jno = i % 20
                    ctx = Context(send, config, id_data={
                        "test": TEST, "runner_id": 1 + i % N_RUNNERS,
                        "journey": specs[jno], "context_id": next(ids),
                        "scenario_id": jno + 1, "scenario_data_id": None},
                        should_stop_func=runner.should_stop)
                    _run_sync(runner._execute(ctx, jno + 1, None, specs[jno], None))
                    i += 1
                n = len(out)
                # the duplicator hands the same bytes to both
                self.col_recv._socket = _fake_socket(recv=iter(out).__next__)
                self.col_recv._run(_stop_after(n))
                self.stats_recv._socket = _fake_socket(recv=iter(out).__next__)
                self.stats_recv._run(_stop_after(n))
                if self.check:
                    self.stats_sender.send(self.stats.dump())
                d = len(self.dumps)
                if d:
                    self.prom_recv._socket = _fake_socket(recv=iter(self.dumps).__next__)
                    self.prom_recv._run(_stop_after(d))
                    self.dumps.clear()
                if not self.check and time.time() >= next_scrape:
                    self.prom.format()
                    next_scrape += 1
                out.clear()
                clock.tick(n)
        finally:
            self.col._current.close()
            self._rm()

    def digest(self):
        h = _h()
        h.update(self.prom.format().encode())
        return h.hexdigest()


BENCHES = {
    "codec": Codec,
    "context_send": ContextSend,
    "runner": RunnerLoop,
    "controller": ControllerBench,
    "stats": StatsBench,
    "stats_http": StatsHttpOnly,
    "stats_quiet": StatsQuiet,
    "collector": CollectorBench,
    "collector_json": CollectorJson,
    "duplicator": DuplicatorBench,
    "prom_update": PromUpdate,
    "prom_format": PromFormat,
    "pipeline": Pipeline,
}
