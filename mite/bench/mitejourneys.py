"""Synthetic mite journeys shaped like a large HTTP test, without the network.

Each journey runs a few named transactions; each transaction makes one to
three "HTTP requests", and for each one sends the same http_metrics message
that mite_http's response callback sends (same fields, same order), with
values drawn from a fixed, precomputed table so runs are reproducible.
About 1% of transactions raise an exception and 0.5% a MiteError, as real
tests do.

SLEEP = True makes each request yield to the event loop (as real I/O would),
so the runner benchmark has many journeys in flight at once.
"""

import asyncio
import random

from mite.exceptions import MiteError

SLEEP = False

_rng = random.Random(20261121)
_CODES = [200] * 90 + [201] * 3 + [304] * 3 + [404] * 2 + [500, 503]
_METHODS = ["GET"] * 8 + ["POST", "PUT"]
_HOSTS = [f"https://svc{i}.example.test" for i in range(8)]


def _metrics(n):
    out = []
    for i in range(n):
        total = _rng.lognormvariate(-3.0, 0.9)
        dns = _rng.random() * 0.002
        conn = 0.0 if _rng.random() < 0.9 else _rng.random() * 0.01
        tls = 0.0 if conn == 0.0 else conn + _rng.random() * 0.02
        pre = max(tls, conn, dns) + 0.0001
        fb = pre + total * 0.8
        out.append(
            dict(
                start_time=1763700000.0 + i * 0.001,
                effective_url=f"{_rng.choice(_HOSTS)}/api/v2/item/{_rng.randrange(100000)}?x=1",
                response_code=_rng.choice(_CODES),
                dns_time=dns,
                connect_time=conn,
                tls_time=tls,
                transfer_start_time=pre,
                first_byte_time=fb,
                total_time=total,
                primary_ip=f"10.1.{_rng.randrange(256)}.{_rng.randrange(256)}",
                method=_rng.choice(_METHODS),
                download_size=_rng.randrange(200, 200000),
                upload_size=0 if _rng.random() < 0.8 else _rng.randrange(100, 5000),
                queue_time=_rng.random() * 0.001,
                client_lag=_rng.random() * 0.0005,
                posttransfer_time=pre + 0.00005,
                http_version=2,
            )
        )
    return out


_METRICS = _metrics(4096)
# (n_requests, fail) decisions, precomputed so journeys draw no random numbers
_PLAN = [
    (1 + _rng.randrange(3), "exc" if r < 0.01 else "err" if r < 0.015 else None)
    for r in (_rng.random() for _ in range(8192))
]
_pos = [0, 0]

_TXN_NAMES = [
    "Login", "Home", "Search", "Browse category", "Product page", "Add to basket",
    "View basket", "Checkout", "Payment", "Confirm order", "Logout", "Account",
]


def _make_journey(jno):
    names = [_TXN_NAMES[(jno + k * 5) % len(_TXN_NAMES)] for k in range(5)]

    async def journey(ctx):
        for name in names:
            async with ctx.transaction(name):
                p = _pos[1]
                _pos[1] = (p + 1) & 8191
                n, fail = _PLAN[p]
                for _ in range(n):
                    if SLEEP:
                        await asyncio.sleep(0)
                    m = _pos[0]
                    _pos[0] = (m + 1) & 4095
                    ctx.send("http_metrics", **_METRICS[m])
                if fail == "exc":
                    raise KeyError(f"no such element: item-{p}")
                if fail == "err":
                    raise MiteError(f"bad status for {name}", status=500, url="/api")

    journey.__name__ = journey.__qualname__ = f"journey_{jno:02d}"
    return journey


N_JOURNEYS = 20
for _j in range(N_JOURNEYS):
    globals()[f"journey_{_j:02d}"] = _make_journey(_j)

JOURNEY_SPECS = [f"mitejourneys:journey_{j:02d}" for j in range(N_JOURNEYS)]


def reset():
    _pos[0] = _pos[1] = 0
