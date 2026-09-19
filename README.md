# Trend Pyramiding Backtester

Python 回测项目，用来验证：

**顺势金字塔加仓（Pyramiding / Anti-Martingale） + 结构止损 + ATR/结构移动止盈。**

项目把“方向信号”和“执行/风险管理”分开。若外部模型已经提供 `signal_long`，项目直接使用外部信号；如果没有提供，则使用内置的默认趋势/突破信号。

## v1.1

v1.1 的目标不是在 HYPE 或 XAU 上搜索最优参数，而是降低快速 1H 信号噪声、提高加仓确认质量，并增加锁定参数的时间序列 OOS 验证。

默认内置信号现在为：

- 1H 收盘价高于 EMA20；
- 1H 收盘价突破此前 20 根 K 线最高价；
- 最近一根**已经完整收盘的 1D K线**收盘价高于日线 EMA20；
- 日线 EMA20 高于 3 个完整日线 K 线之前的 EMA20。

高周期数据只使用已经完整收盘的 K 线，通过 close-time 对齐，避免把尚未结束的日线信息泄漏到 1H 信号中。

交易管理：

- 只做多。
- 信号在 K 线收盘后确认，最早下一根 K 线开盘成交。
- 初始止损只由结构失效位决定：最近 8 根低点最低值减 `0.10 ATR`。
- 如果结构止损距离超过 `4 ATR`，跳过该交易，而不是人为把止损收紧。
- 默认四档风险/仓位权重：`30% / 30% / 20% / 20%`。
- 只给盈利仓加仓。
- 加仓需要同时满足：
  - 相对上次加仓价继续上涨至少 `0.75 ATR`；
  - 收盘价再次突破此前 20 根 K 线最高价。
- 单笔完整交易的开放风险预算仍限制为账户权益的 `1%`。
- 达到 `1.5R` 后才允许把止损抬到首笔入场价。
- 达到 `2R` 后才启动移动止盈。
- ATR trailing 默认放宽到 `3 ATR`；结构/ATR trailing 只会上移。
- 最大名义仓位不超过账户权益 100%，默认不使用杠杆。
- 回测计入固定手续费和滑点。

`atr_stop_mult` 仍保留在配置对象中以兼容旧配置，但 v1.1 不再用它人为收紧初始结构止损。

## 默认参数

`config/default.toml`：

```toml
initial_cash = 100000.0
fee_bps = 5.0
slippage_bps = 3.0
risk_per_trade = 0.01
max_position_pct = 1.0

atr_period = 14
structure_lookback = 8
structure_buffer_atr = 0.10
max_initial_stop_atr = 4.0

ema_period = 20
entry_breakout_lookback = 20
trend_filter_timeframe = "1D"
trend_filter_ema_period = 20
trend_filter_slope_lookback = 3

add_breakout_lookback = 20
add_step_atr = 0.75
require_add_breakout = true

trail_atr_mult = 3.0
trail_activation_r = 2.0
break_even_r = 1.5

risk_weights = [0.30, 0.30, 0.20, 0.20]
allocation_weights = [0.30, 0.30, 0.20, 0.20]
```

## 输入数据

CSV 至少包含：

```text
timestamp,open,high,low,close,volume
```

如果上游模型已经产生方向信号，可额外提供：

```text
signal_long
```

传入 `--signal-column signal_long` 后，内置的日线趋势过滤与默认突破信号不会覆盖外部信号；执行、加仓和风险管理仍使用 v1.1 规则。

## 普通回测

```bash
pyramid-backtest backtest \
  --csv your_1h_data.csv \
  --config config/default.toml \
  --output-dir artifacts
```

使用外部信号：

```bash
pyramid-backtest backtest \
  --csv your_1h_data.csv \
  --config config/default.toml \
  --signal-column signal_long \
  --output-dir artifacts
```

输出：

- `summary.json`
- `equity_curve.csv`
- `trades.csv`
- `events.csv`

## 锁定参数 Walk-Forward / Rolling OOS

v1.1 增加：

```bash
pyramid-backtest walk-forward \
  --csv your_1h_data.csv \
  --config config/default.toml \
  --folds 4 \
  --warmup-bars 720 \
  --output-dir artifacts
```

该命令**不在各 fold 内重新寻优**。同一组参数被锁定，然后按时间顺序在多个独立测试窗口评估。每个窗口只能使用测试开始之前的数据作为指标 warm-up。

输出：

- `walk_forward.json`
- `walk_forward_folds.csv`

这主要用于检查策略是否只在某一个完整历史区间表现好，而不是用于寻找单一“最佳参数”。

## GitHub Actions

每次 `push` / 手动运行都会：

1. 安装依赖；
2. Ruff；
3. Pytest；
4. 执行确定性 regression backtest；
5. 检查 regression gate；
6. 生成 1D 交易历史图；
7. 发布 Release。

提交消息为 `v1.1` 时，Actions 额外执行：

- HYPE 1H：2026-05-01 至当前已收盘数据；
- XAU (`xyz:GOLD`) 1H：2026-04-15 至 2026-08-01；
- 两个市场均使用**同一套锁定参数**；
- 两个市场均运行 4-fold rolling OOS；
- 汇总结果保存为 `v11_validation.json` 并随 Release 发布。

这样 v1.1 的代码更新不会通过针对单个市场重新调参来“证明”自己。

## 回测约束

当前引擎仍是 bar-based 回测，同一根 K 线内部无法知道 high/low 的真实发生顺序。因此：

- 已有止损优先于新增加仓；
- 收盘信号下一根开盘成交；
- same-bar stop 在开盘成交之后执行；
- 高周期过滤只使用完整收盘的高周期 K 线。

手续费/滑点目前仍为配置中的固定 bps。历史 funding 尚未自动抓取，因此永续合约长持仓的真实净收益仍可能与回测存在偏差。
