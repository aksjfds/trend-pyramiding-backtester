from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle


def load_events(path: str | Path) -> pd.DataFrame:
    try:
        events = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=["trade_id", "timestamp", "event", "price"])
    if events.empty:
        return events
    events["timestamp"] = pd.to_datetime(events["timestamp"], utc=True)
    return events


def load_extreme_trades(path: str | Path) -> tuple[set[int], dict[int, str]]:
    trades = pd.read_csv(path)
    if trades.empty:
        return set(), {}

    trades["net_pnl"] = pd.to_numeric(trades["net_pnl"], errors="raise")
    best_idx = trades["net_pnl"].idxmax()
    worst_idx = trades["net_pnl"].idxmin()

    best_id = int(trades.loc[best_idx, "trade_id"])
    worst_id = int(trades.loc[worst_idx, "trade_id"])

    selected = {best_id, worst_id}
    labels = {best_id: "max_profit"}
    if worst_id != best_id:
        labels[worst_id] = "max_loss"
    return selected, labels


def resample_daily(market: pd.DataFrame) -> pd.DataFrame:
    return (
        market.set_index("timestamp")
        .resample("1D", closed="left", label="left")
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .dropna(subset=["open", "high", "low", "close"])
        .reset_index()
    )


def draw_candles(ax: plt.Axes, frame: pd.DataFrame) -> None:
    width = 0.66
    for x, row in enumerate(frame.itertuples(index=False)):
        open_price = float(row.open)
        high = float(row.high)
        low = float(row.low)
        close = float(row.close)
        color = "#16a34a" if close >= open_price else "#dc2626"

        ax.vlines(x, low, high, color=color, linewidth=0.8, alpha=0.9)
        bottom = min(open_price, close)
        height = abs(close - open_price)
        if height == 0:
            height = max(abs(close) * 1e-5, 1e-8)
            bottom -= height / 2
        ax.add_patch(
            Rectangle(
                (x - width / 2, bottom),
                width,
                height,
                facecolor=color,
                edgecolor=color,
                linewidth=0.5,
                alpha=0.9,
            )
        )


def add_trade_arrows(
    ax: plt.Axes,
    frame: pd.DataFrame,
    events: pd.DataFrame,
    trade_labels: dict[int, str],
) -> None:
    if events.empty:
        return

    positions = {
        timestamp.floor("D"): idx
        for idx, timestamp in enumerate(frame["timestamp"])
    }

    visible_low = float(frame["low"].min())
    visible_high = float(frame["high"].max())
    price_range = visible_high - visible_low
    if price_range <= 0:
        price_range = max(abs(visible_high) * 0.01, 1.0)

    gap = price_range * 0.022
    arrow_length = price_range * 0.055
    colors = {
        "max_profit": "#16a34a",
        "max_loss": "#dc2626",
    }

    for row in events.itertuples(index=False):
        trade_id = int(row.trade_id)
        trade_type = trade_labels.get(trade_id)
        if trade_type is None:
            continue

        x = positions.get(row.timestamp.floor("D"))
        if x is None:
            continue

        candle = frame.iloc[x]
        if row.event == "entry":
            tip_y = float(candle["low"]) - gap
            tail_y = tip_y - arrow_length
        elif row.event == "exit":
            tip_y = float(candle["high"]) + gap
            tail_y = tip_y + arrow_length
        else:
            continue

        ax.annotate(
            "",
            xy=(x, tip_y),
            xytext=(x, tail_y),
            arrowprops={
                "arrowstyle": "-|>",
                "color": colors[trade_type],
                "linewidth": 2.0,
                "mutation_scale": 14,
                "shrinkA": 0,
                "shrinkB": 0,
            },
            annotation_clip=False,
            zorder=7,
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True)
    parser.add_argument("--events", required=True)
    parser.add_argument("--trades", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--days-per-chart", type=int, default=180)
    args = parser.parse_args()

    if args.days_per_chart < 30:
        raise SystemExit("--days-per-chart must be >= 30")

    market = pd.read_csv(args.csv)
    market["timestamp"] = pd.to_datetime(market["timestamp"], utc=True)
    market = market.sort_values("timestamp").reset_index(drop=True)
    daily_market = resample_daily(market)

    selected_trade_ids, trade_labels = load_extreme_trades(args.trades)
    events = load_events(args.events)
    if not events.empty and selected_trade_ids:
        events = events[
            events["trade_id"].astype(int).isin(selected_trade_ids)
            & events["event"].isin(["entry", "exit"])
        ].copy()
    else:
        events = events.iloc[0:0].copy()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    chart_count = 0
    for start in range(0, len(daily_market), args.days_per_chart):
        stop = min(start + args.days_per_chart, len(daily_market))
        chunk = daily_market.iloc[start:stop].copy().reset_index(drop=True)
        if chunk.empty:
            continue

        start_time = chunk["timestamp"].iloc[0]
        end_time = chunk["timestamp"].iloc[-1] + pd.Timedelta(days=1)
        chunk_events = events[
            (events["timestamp"] >= start_time) & (events["timestamp"] < end_time)
        ].copy()

        fig, ax = plt.subplots(figsize=(18, 8))
        draw_candles(ax, chunk)
        add_trade_arrows(ax, chunk, chunk_events, trade_labels)

        tick_count = min(12, len(chunk))
        if tick_count > 1:
            tick_positions = [
                round(i * (len(chunk) - 1) / (tick_count - 1))
                for i in range(tick_count)
            ]
        else:
            tick_positions = [0]
        tick_labels = [
            chunk["timestamp"].iloc[pos].strftime("%Y-%m-%d")
            for pos in tick_positions
        ]
        ax.set_xticks(tick_positions)
        ax.set_xticklabels(tick_labels, rotation=35, ha="right", fontsize=8)

        legend_handles = [
            Line2D(
                [0],
                [0],
                color="#16a34a",
                linewidth=2.0,
                label="Max-profit trade",
            ),
            Line2D(
                [0],
                [0],
                color="#dc2626",
                linewidth=2.0,
                label="Max-loss trade",
            ),
        ]

        ax.legend(handles=legend_handles, loc="upper left", ncol=2, fontsize=8)
        ax.grid(alpha=0.16, linewidth=0.5)
        ax.set_ylabel("Price")
        ax.set_xlim(-1, len(chunk))

        price_low = float(chunk["low"].min())
        price_high = float(chunk["high"].max())
        price_range = price_high - price_low
        if price_range <= 0:
            price_range = max(abs(price_high) * 0.01, 1.0)
        ax.set_ylim(
            price_low - price_range * 0.11,
            price_high + price_range * 0.11,
        )

        ax.set_title(
            "Backtest trade history (1D candles) | "
            f"{start_time.strftime('%Y-%m-%d')} - "
            f"{(end_time - pd.Timedelta(days=1)).strftime('%Y-%m-%d')}"
        )

        fig.tight_layout()
        chart_count += 1
        output = output_dir / f"trade_history_1d_{chart_count:03d}.png"
        fig.savefig(output, dpi=150, bbox_inches="tight")
        plt.close(fig)

    if chart_count == 0:
        raise SystemExit("no chart was generated")

    print(f"generated {chart_count} daily trade-history chart(s)")


if __name__ == "__main__":
    main()
