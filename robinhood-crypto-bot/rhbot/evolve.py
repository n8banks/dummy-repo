"""Evolve a tiny neural trading policy, with guards against memorising history.

The idea is the same as evolving simulated creatures to walk: a population of
small neural "brains" is scored, the fittest breed and mutate, repeat. The
problem is that ~9 years of daily crypto prices contain only a handful of
distinct market regimes, and evolution is superb at memorising them. Three
guards make the result mean something:

1. Domain randomisation (the robotics trick for making sim-trained walkers
   work in the real world): every generation is scored under a different
   random trading cost and a random subset of coins, so no brain can survive
   by fitting one coin's history or one cost level. Elites are re-scored every
   generation, so a lucky score doesn't persist.
2. Consistency fitness: the mean of per-year log returns minus half their
   spread. A brain that makes money in most years beats one that got one
   lucky year.
3. A null control: `null_universe` scrambles each coin's daily returns in
   time, destroying any trend while keeping volatility and fat tails. Running
   the same evolution on that shows how good a champion looks when there is
   nothing to find. The real champion must clearly beat that bar.

Finally the champion is tested once on a holdout period evolution never saw.

The brain: 9 features per coin per day -> 6 tanh hidden units -> 1 score in
[-1, 1]. Buy when the score exceeds `enter`, sell when it falls below `exit`.
It runs inside the normal backtester and risk engine (costs, stops, sizing,
kill switch), so it can only choose *when* to be in a coin, not break the
risk rules.
"""

from __future__ import annotations

import json
import math
import random
import statistics
from dataclasses import dataclass, replace
from multiprocessing import Pool
from pathlib import Path

from . import backtest, indicators as ind, research
from .data import Candle
from .risk import RiskConfig
from .strategies import ENTER, EXIT, Strategy

N_FEAT = 9
N_HID = 6
N_W = (N_FEAT + 1) * N_HID + (N_HID + 1)
LOOKBACKS = (7, 14, 30, 60, 90)
WARMUP = 181


# -- features -----------------------------------------------------------------


def coin_features(candles: list[Candle], btc: list[Candle]) -> list[list[float] | None]:
    """Per-bar feature vectors using only data up to that bar."""
    closes = [c.close for c in candles]
    n = len(closes)
    logret = [0.0] + [math.log(closes[i] / closes[i - 1]) for i in range(1, n)]
    vol30: list[float | None] = [None] * n
    for i in range(31, n):
        w = logret[i - 29:i + 1]
        m = sum(w) / 30
        vol30[i] = math.sqrt(sum((x - m) ** 2 for x in w) / 29) or 1e-4
    btc_close = {c.ts: c.close for c in btc}
    btc_ema = ind.ema([c.close for c in btc], 100)
    btc_bull = {c.ts: (btc_ema[i] is not None and c.close > btc_ema[i]) for i, c in enumerate(btc)}
    btc_ts = [c.ts for c in btc]
    btc_idx = {t: i for i, t in enumerate(btc_ts)}

    out: list[list[float] | None] = [None] * n
    for i in range(WARMUP, n):
        v = vol30[i]
        f = [max(-3.0, min(3.0, math.log(closes[i] / closes[i - k]) / (v * math.sqrt(k)))) / 3
             for k in LOOKBACKS]
        hist = [x for x in vol30[i - 180:i] if x]
        f.append(max(-2.0, min(2.0, v / (sum(hist) / len(hist)) - 1)) / 2 if hist else 0.0)
        f.append(max(-1.0, closes[i] / max(closes[i - 90:i + 1]) - 1) * 2 + 1)
        t = candles[i].ts
        j = btc_idx.get(t)
        if j is None or j < 30:
            continue
        f.append(1.0 if btc_bull.get(t) else -1.0)
        f.append(max(-1.0, min(1.0, math.log(btc_close[t] / btc[j - 30].close) * 3)))
        out[i] = f
    return out


# -- genome / strategy --------------------------------------------------------------


@dataclass
class Genome:
    w: list[float]
    enter: float = 0.3
    exit: float = -0.1

    @staticmethod
    def random(rng: random.Random) -> "Genome":
        return Genome([rng.gauss(0, 0.5) for _ in range(N_W)], rng.uniform(0, 0.6), rng.uniform(-0.6, 0.2))

    def score(self, f: list[float]) -> float:
        w, k = self.w, 0
        hidden = []
        for _ in range(N_HID):
            s = w[k + N_FEAT]
            for x in range(N_FEAT):
                s += w[k + x] * f[x]
            hidden.append(math.tanh(s))
            k += N_FEAT + 1
        s = w[k + N_HID]
        for h in range(N_HID):
            s += w[k + h] * hidden[h]
        return math.tanh(s)

    def mutate(self, rng: random.Random, sigma: float) -> "Genome":
        w = [x + rng.gauss(0, sigma) if rng.random() < 0.3 else x for x in self.w]
        enter = min(0.95, max(-0.5, self.enter + rng.gauss(0, sigma / 3)))
        exit_ = min(enter - 0.05, max(-0.95, self.exit + rng.gauss(0, sigma / 3)))
        return Genome(w, enter, exit_)

    def cross(self, other: "Genome", rng: random.Random) -> "Genome":
        w = [a if rng.random() < 0.5 else b for a, b in zip(self.w, other.w)]
        enter = rng.choice((self.enter, other.enter))
        return Genome(w, enter, min(enter - 0.05, rng.choice((self.exit, other.exit))))

    def to_json(self) -> dict:
        return {"w": self.w, "enter": self.enter, "exit": self.exit}

    @staticmethod
    def from_json(d: dict) -> "Genome":
        return Genome(d["w"], d["enter"], d["exit"])


class NeuralStrategy(Strategy):
    name = "evolved"
    warmup = WARMUP

    def __init__(self, genome: Genome, tables_by_list: dict[int, dict[int, list[float]]]):
        self.genome = genome
        # id(candle list) -> {ts: feature vector}; the backtester and trader pass
        # the very list objects the tables were built from.
        self.tables = tables_by_list

    def prepare(self, candles):
        super().prepare(candles)
        self.ts = [c.ts for c in candles]
        self.features = self.tables.get(id(candles), {})

    def signal(self, i, in_position, bars_held):
        f = self.features.get(self.ts[i])
        if f is None:
            return None
        s = self.genome.score(f)
        if not in_position and s > self.genome.enter:
            return ENTER
        if in_position and s < self.genome.exit:
            return EXIT
        return None


def feature_tables(universe: dict[str, list[Candle]]) -> dict[str, dict[int, list[float]]]:
    btc = universe["BTC-USD"]
    out = {}
    for sym, cs in universe.items():
        feats = coin_features(cs, btc)
        out[sym] = {c.ts: f for c, f in zip(cs, feats) if f is not None}
    return out


def make_factory(genome: Genome, universe: dict[str, list[Candle]],
                 tables: dict[str, dict[int, list[float]]] | None = None):
    """Zero-arg strategy factory for the backtester / trader."""
    tables = tables or feature_tables(universe)
    by_list = {id(universe[s]): tables[s] for s in universe if s in tables}
    return lambda: NeuralStrategy(genome, by_list)


def load_genome(path: str | Path) -> Genome:
    return Genome.from_json(json.loads(Path(path).read_text())["genome"])


# -- fitness --------------------------------------------------------------------------


@dataclass
class EvalSpec:
    universe: dict[str, list[Candle]]
    tables: dict[str, dict[int, list[float]]]
    symbols: list[str]
    spread: float
    start: int  # measure from here (after warmup)


_SPEC: EvalSpec | None = None


def _init(spec: EvalSpec) -> None:
    global _SPEC
    _SPEC = spec


def run_genome(genome: Genome, spec: EvalSpec, risk: RiskConfig | None = None,
               kill_switch: bool = False) -> backtest.Result:
    uni = {s: spec.universe[s] for s in spec.symbols}
    tables = {s: spec.tables[s] for s in spec.symbols}
    risk = risk or RiskConfig()
    if not kill_switch:
        risk = replace(risk, max_drawdown=0.999)
    res = backtest.run(uni, make_factory(genome, uni, tables), risk,
                       backtest.CostModel(spread_pct=spec.spread, stop_slippage=0.005),
                       bars_per_year=365)
    keep = [(t, e) for t, e in res.equity_curve if t >= spec.start]
    res.equity_curve, res.flows = keep, [0.0] * len(keep)
    res.start_equity = keep[0][1] if keep else res.start_equity
    res.trades = [t for t in res.trades if t.entry_ts >= spec.start]
    return res


def fitness_of(res: backtest.Result, genome: Genome) -> float:
    years = research.yearly(res.equity_curve)
    if len(years) < 2:
        return -1.0
    logs = [math.log(max(1e-6, 1 + r)) for r in years.values()]
    l2 = sum(x * x for x in genome.w) / len(genome.w)
    return statistics.mean(logs) - 0.5 * statistics.pstdev(logs) - 0.002 * l2


def _score(genome: Genome) -> float:
    assert _SPEC is not None
    return fitness_of(run_genome(genome, _SPEC), genome)


# -- evolution --------------------------------------------------------------------------


def evolve(universe: dict[str, list[Candle]], train_start: int, train_end: int,
           generations: int = 25, pop: int = 32, seed: int = 1, workers: int = 4,
           log=print) -> tuple[Genome, list[float]]:
    """Returns the champion and the per-generation best (randomised) fitness."""
    rng = random.Random(seed)
    train = {s: [c for c in cs if c.ts < train_end] for s, cs in universe.items()}
    tables = feature_tables(train)
    coins = [s for s in train if len(train[s]) > WARMUP + 365]
    population = [Genome.random(rng) for _ in range(pop)]
    history = []
    sigma = 0.4
    for g in range(generations):
        # domain randomisation for this generation
        k = max(3, len(coins) - 2)
        syms = sorted(set(rng.sample([c for c in coins if c != "BTC-USD"], k - 1)) | {"BTC-USD"})
        spec = EvalSpec(train, tables, syms, rng.uniform(0.015, 0.025), train_start)
        with Pool(workers, initializer=_init, initargs=(spec,)) as p:
            scores = p.map(_score, population)
        ranked = sorted(zip(scores, range(pop)), reverse=True)
        best = ranked[0][0]
        history.append(best)
        log(f"gen {g + 1:2}/{generations}: best {best:+.4f}  median {statistics.median(scores):+.4f}  "
            f"cost {spec.spread:.2%}  coins {len(syms)}")
        elite = [population[i] for _, i in ranked[: pop // 4]]

        def pick():
            a, b = rng.sample(ranked[: pop // 2], 2)
            return population[max(a, b)[1]]

        children = []
        while len(children) < pop - len(elite) - 2:
            child = pick().cross(pick(), rng) if rng.random() < 0.5 else pick()
            children.append(child.mutate(rng, sigma))
        population = elite + children + [Genome.random(rng) for _ in range(2)]  # fresh blood
        sigma = max(0.1, sigma * 0.95)

    # pick the champion on the full training set at the nominal 2% cost
    spec = EvalSpec(train, tables, coins, 0.02, train_start)
    with Pool(workers, initializer=_init, initargs=(spec,)) as p:
        final = p.map(_score, population)
    champ = population[max(range(pop), key=lambda i: final[i])]
    return champ, history


def null_universe(universe: dict[str, list[Candle]], seed: int) -> dict[str, list[Candle]]:
    """Same coins, same daily return distribution, but each coin's returns
    shuffled in time (BTC's too), so there is no trend to find. Candle ranges
    are scaled around the shuffled closes so ATR stays realistic."""
    rng = random.Random(seed)
    out = {}
    for sym, cs in universe.items():
        rets = [cs[i].close / cs[i - 1].close for i in range(1, len(cs))]
        rng.shuffle(rets)
        price, new = cs[0].close, [cs[0]]
        for i, r in enumerate(rets, start=1):
            c = cs[i]
            o = price
            price *= r
            hi = max(o, price) * (c.high / max(c.open, c.close))
            lo = min(o, price) * (c.low / min(c.open, c.close))
            new.append(Candle(c.ts, o, hi, lo, price, c.volume))
        out[sym] = new
    return out


def save(path: str | Path, genome: Genome, meta: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps({"genome": genome.to_json(), **meta}, indent=2))
