"""Measure how long θ-crossing cross-venue gaps persist, from the dislocation log.

Reads data/dislocations.jsonl (net edge per evaluation, correct-polarity,
fee-adjusted) and, per (pair, orientation), extracts WINDOWS: maximal runs where
net edge >= θ. Reports the duration distribution — the single fact that decides
whether "no edge" is a speed problem (windows are sub-second) or a patience
problem (windows last seconds and we're already fast enough).

    uv run python scripts/analyze_dislocations.py [path] [--theta-micros N]
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict

THETA = 20_000          # default θ = $0.02; override with --theta-micros
MAX_GAP_MS = 5_000      # a >5s data gap ends a window (pair stopped updating)


def main() -> None:
    path = "data/dislocations.jsonl"
    theta = THETA
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--theta-micros" and i + 1 < len(args):
            theta = int(args[i + 1])
        elif not a.startswith("--"):
            path = a

    by_key: dict[tuple[str, str], list[tuple[int, int, bool]]] = defaultdict(list)
    n = 0
    try:
        with open(path) as f:
            for line in f:
                if not line.strip():
                    continue
                d = json.loads(line)
                by_key[(d["pair"], d["or"])].append((d["ts"], d["net"], d["fire"]))
                n += 1
    except FileNotFoundError:
        print(f"no log at {path} yet — enable dislocation_log and let it run")
        return

    windows: list[tuple[str, str, int, int, int]] = []  # pair, or, dur_ms, peak, samples
    for (pair, orient), samples in by_key.items():
        samples.sort()
        start = peak = cnt = None  # type: ignore[assignment]
        last_ts = None
        for ts, net, _fire in samples:
            gap = last_ts is not None and ts - last_ts > MAX_GAP_MS
            if net >= theta and start is not None and gap:
                windows.append((pair, orient, last_ts - start, peak, cnt))  # type: ignore[arg-type]
                start = None
            if net >= theta:
                if start is None:
                    start, peak, cnt = ts, net, 0
                peak = max(peak, net)  # type: ignore[type-var]
                cnt += 1  # type: ignore[operator]
                above_ts = ts
            elif start is not None:
                windows.append((pair, orient, above_ts - start, peak, cnt))  # type: ignore[arg-type]
                start = None
            last_ts = ts
        if start is not None:
            windows.append((pair, orient, above_ts - start, peak, cnt))  # type: ignore[arg-type]

    print(f"samples: {n} across {len(by_key)} (pair,orientation) series")
    print(f"θ = ${theta/1e6:.4f}\n")
    if not windows:
        print("NO θ-crossing windows found — cross-venue prices stayed within fees "
              "the entire time (efficient; speed would not have helped).")
        return

    durs = sorted(w[2] for w in windows)
    peaks = [w[3] for w in windows]

    def pct(xs: list[int], p: float) -> int:
        return xs[min(len(xs) - 1, int(p * len(xs)))]

    print(f"θ-crossing windows: {len(windows)}")
    print(f"  duration ms   p50={pct(durs,0.5)}  p90={pct(durs,0.9)}  "
          f"max={durs[-1]}  min={durs[0]}")
    med = sorted(peaks)[len(peaks) // 2] / 1e6
    print(f"  peak edge     max=${max(peaks)/1e6:.3f}  median=${med:.3f}")
    instant = sum(1 for d in durs if d == 0)
    print(f"  instantaneous (single-sample, <one update apart): {instant}/{len(windows)}")
    print("\n  interpretation:")
    print("   - windows lasting SECONDS -> we are already fast enough; edge is just rare")
    print("   - windows mostly instantaneous/<200ms -> sub-second; speed (colo) is the lever")
    print("   - (and none at all -> efficient even in motion; different strategy needed)")
    print("\n  top windows:")
    for pair, orient, dur, peak, cnt in sorted(windows, key=lambda w: -w[2])[:8]:
        print(f"    {dur:>7}ms  peak=${peak/1e6:.3f}  {cnt} samples  {orient}  {pair[:44]}")


if __name__ == "__main__":
    main()
