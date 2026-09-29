import random

import pytest

from rhbot import backtest, evolve
from rhbot.data import synthetic_universe


@pytest.fixture(scope="module")
def uni():
    hourly = synthetic_universe(["BTC-USD", "ETH-USD", "SOL-USD"], bars=24 * 700, seed=3)
    from rhbot.data import resample
    return {s: resample(c, 86400) for s, c in hourly.items()}


def test_genome_score_bounded_and_mutation_keeps_thresholds_ordered():
    rng = random.Random(0)
    g = evolve.Genome.random(rng)
    for _ in range(50):
        f = [rng.uniform(-1, 1) for _ in range(evolve.N_FEAT)]
        assert -1 <= g.score(f) <= 1
        g = g.mutate(rng, 0.5).cross(evolve.Genome.random(rng), rng)
        assert g.exit < g.enter
    assert evolve.Genome.from_json(g.to_json()).w == g.w


def test_features_use_only_the_past(uni):
    btc = uni["BTC-USD"]
    full = evolve.coin_features(uni["ETH-USD"], btc)
    cut = 400
    part = evolve.coin_features(uni["ETH-USD"][:cut], btc[:cut])
    for i in range(evolve.WARMUP, cut):
        assert part[i] == full[i]  # later data never changes an earlier feature


def test_neural_strategy_runs_in_backtester(uni):
    g = evolve.Genome.random(random.Random(1))
    res = backtest.run(uni, evolve.make_factory(g, uni), bars_per_year=365)
    assert res.metrics()["final_equity"] > 0


def test_null_universe_keeps_returns_but_scrambles_order(uni):
    fake = evolve.null_universe(uni, 7)
    for s in uni:
        a = sorted(round(uni[s][i].close / uni[s][i - 1].close, 12) for i in range(1, len(uni[s])))
        b = sorted(round(fake[s][i].close / fake[s][i - 1].close, 12) for i in range(1, len(fake[s])))
        assert a == pytest.approx(b)
        assert [c.close for c in fake[s]] != [c.close for c in uni[s]]


def test_evolve_smoke(uni):
    start, end = uni["BTC-USD"][200].ts, uni["BTC-USD"][-1].ts
    champ, hist = evolve.evolve(uni, start, end, generations=2, pop=6, seed=2, workers=2,
                                log=lambda s: None)
    assert len(hist) == 2 and len(champ.w) == evolve.N_W
