from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="artifacts/v11")
    parser.add_argument("--output-json", default="artifacts/v11_validation.json")
    parser.add_argument("--output-md", default="artifacts/v11_validation.md")
    args = parser.parse_args()

    root = Path(args.root)
    markets: dict[str, dict] = {}
    for name in ("HYPE", "XAU"):
        market_dir = root / name
        markets[name] = {
            "summary": load_json(market_dir / "summary.json"),
            "walk_forward": load_json(market_dir / "walk_forward.json"),
        }

    report = {
        "version": "v1.1",
        "validation_method": "locked-parameter chronological rolling OOS",
        "markets": markets,
    }
    Path(args.output_json).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    lines = [
        "v1.1 uses one locked parameter set on both markets; no per-market optimization is run.",
        "",
        "| Market | Return | Max DD | PF | Avg R | OOS positive folds | OOS median return |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, payload in markets.items():
        summary = payload["summary"]
        walk = payload["walk_forward"]
        lines.append(
            f"| {name} | {summary['total_return_pct']:.2f}% | "
            f"{summary['max_drawdown_pct']:.2f}% | {summary['profit_factor']:.2f} | "
            f"{summary['average_r']:.3f} | "
            f"{walk['positive_folds']}/{walk['folds']} | "
            f"{walk['median_return_pct']:.2f}% |"
        )

    Path(args.output_md).write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
