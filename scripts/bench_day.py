"""Time a one-day replay for every reference strategy (SYNTHETIC DATA).

Usage: uv run python scripts/bench_day.py [hours]
"""

import sys
import time

from pmsim.data.synthetic import SyntheticConfig, generate
from pmsim.engine.backtest import Backtest
from pmsim.strategies.registry import BUILDERS, build_strategy


def main(hours: float = 24.0) -> None:
    t0 = time.perf_counter()
    data = generate(SyntheticConfig(duration_hours=hours))
    print(
        f"synthetic {hours:g} h: {len(data.events):,} events, generated in "
        f"{time.perf_counter() - t0:.1f} s"
    )
    for name in sorted(BUILDERS):
        t0 = time.perf_counter()
        res = Backtest(build_strategy(name, {}, data.universe), data).run()
        secs = time.perf_counter() - t0
        rate = len(data.events) / secs
        print(f"{name:16s} {secs:6.1f} s  {rate:9,.0f} events/s  fills {len(res.fills):6,}")


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 24.0)
