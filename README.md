# Trend Pyramiding Backtester

一个独立的 Python 回测项目，用来验证这套交易执行/风险管理技术：

**顺势金字塔加仓（Pyramiding / Anti-Martingale） + 结构/ATR 止损 + 移动止盈。**

项目重点不是预测“哪个币会涨”，而是：当外部信号已经给出多头判断时，怎样只向盈利仓加仓、限制总风险，并让盈利仓尽可能继续运行。

## 核心规则

- 只做多。
- 信号在 K 线收盘后确认，订单最早在下一根 K 线开盘成交，避免同 K 线偷看未来。
- 默认四档风险权重：`30% / 30% / 20% / 20%`。
- 只给盈利仓加仓；默认需要价格相对上次成交继续上涨 `0.75 ATR`；也可打开 `require_add_breakout`，额外要求突破前 10 根 K 线高点。
- 初始止损取以下两者中更紧的一项：
  - 最近结构低点减去 `0.10 ATR`；
  - 入场信号收盘价减去 `2.0 ATR`。
- 单次完整交易的最坏开放风险默认限制为账户权益的 `1%`；每次加仓会重新检查“当前仓位打到止损的剩余风险”，不会因为加仓突破总风险预算。
- 达到 `1R` 后允许把止损抬到保本区域，并启用结构/ATR 移动止盈。
- 移动止损只会上移，不会下移。
- 默认最大现货名义仓位不超过账户权益的 100%，不使用杠杆。
- 回测计入双边手续费和滑点。

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Windows PowerShell：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
```

## 输入数据

CSV 至少包含：

```text
timestamp,open,high,low,close,volume
```

如果你的上游模型/人工判断已经给出“这里开始看涨”，建议额外提供布尔/0-1 列，例如：

```text
signal_long
```

这样本项目只负责执行和风险管理，不把预测模型与交易技术混在一起。

## 回测

```bash
pyramid-backtest backtest \
  --csv your_1h_data.csv \
  --config config/default.toml \
  --signal-column signal_long \
  --output-dir artifacts
```

如果不传 `--signal-column`，项目会使用一个简单的 EMA + 前高突破信号作为演示入口；它只是占位信号，不代表项目对方向预测的假设。

输出：

- `artifacts/summary.json`
- `artifacts/equity_curve.csv`
- `artifacts/trades.csv`
- `artifacts/events.csv`

## GitHub Actions

`.github/workflows/backtest.yml` 在每次 `push`、`pull_request` 和手动触发时执行：

1. 安装项目；
2. Ruff 静态检查；
3. Pytest 单元测试；
4. 生成固定、确定性的 1H 回归行情；
5. 执行完整策略回测；
6. 与 `benchmarks/baseline.json` 做最低收益 / 最大回撤 / 最低交易数回归检查；
7. 把本次 `summary / equity curve / trades / events` 上传为 Actions artifact。

固定行情用于 CI 的可复现性，因此同一代码版本不会因为实时行情变化导致 Actions 随机红灯。真实历史行情仍可以通过 CLI 直接回测。

GitHub 官方建议在 Python CI 中使用 `setup-python` 固定 Python 环境；Actions artifacts 可用于保存每次 workflow 产生的测试和回测结果。

## 参数

全部默认参数位于 `config/default.toml`。最关键的参数：

```toml
risk_per_trade = 0.01
risk_weights = [0.30, 0.30, 0.20, 0.20]
allocation_weights = [0.30, 0.30, 0.20, 0.20]
atr_stop_mult = 2.0
add_step_atr = 0.75
require_add_breakout = false
trail_atr_mult = 2.25
trail_activation_r = 1.0
break_even_r = 1.0
```

## 回测约束

当前版本是 bar-based 回测，因此同一根 K 线内部无法知道 high/low 的真实发生顺序。实现采用保守原则：已有止损优先于新增加仓；信号只在收盘后生成，下一根 K 线开盘成交。
