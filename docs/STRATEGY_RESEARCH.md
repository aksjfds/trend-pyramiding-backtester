# 固定参数策略迭代：confirmed-pyramid

这是一项回测候选，通过 `pyramid-backtest backtest --strategy confirmed-pyramid` 启用。默认 `classic` 和 OKX 实盘策略保持原样。两种策略使用同一个 `config/default.toml`，不引入币种专属配置或日期判断。

## 规则变化

- 原突破信号再要求收盘位于当前 K 线上半部，下一根开盘执行；显式传入的外部信号也需要通过该确认。
- 已有仓位的止损达到加权平均入场价后，才允许继续加仓。这不包含手续费，不能视为保证不亏。
- 复用累计档位中已释放的止损风险，每次加仓不超过原配置最大单档额度，并继续遵守总风险、名义仓位、单档资金分配和现金约束。当前配置下，后两档的风险额度最多由总预算的 20% 提至 30%；风险分配规则变了，配置数值未变。

止损计算及只能上移、只向盈利仓加仓、手续费、滑点和下一根开盘成交规则沿用原版。

## HYPE 固定区间结果

使用项目 Release `backtest-v0.21.1` 的 HYPE perpetual 1h 数据：2026-05-01T00:00:00Z 至 2026-09-21T00:59:59.999000Z，共 3,433 根完整 K 线。

| 指标 | classic | confirmed-pyramid |
| --- | ---: | ---: |
| Total return | 20.7063% | 22.1308% |
| Max drawdown | -5.3521% | -5.1026% |
| Sharpe | 3.3122 | 3.2543 |
| Trades | 62 | 59 |
| Profit factor | 2.3340 | 2.7183 |

收益增加 1.4245 个百分点，最大回撤绝对值减少 0.2496 个百分点。7 月收益及整体 Sharpe 略差；收益和回撤改善不意味着所有指标都改善。

手续费/滑点翻倍后：原版收益 17.3770%、最大回撤绝对值 5.4503%；新版收益 18.9721%、最大回撤绝对值 5.1437%。项目 XAU 数据中新版亏损缩小，但依然亏损。

先用 5–7 月比较通用规则，再查看后段并继续迭代组合。后段属于回顾性分段检查，不能宣称独立样本外验证。规则选择仍可能过拟合，需要未来数据验证。回测沿用无杠杆现金模型，不含永续资金费与实际盘口，最大回撤按小时收盘净值计算。

## 复现

本次下载的数据与完整报告保存在本地 `artifacts/hype-strategy-20260921/`（未纳入 Git）。输入数据的来源和 SHA-256 在 `source/provenance.json`。首次下载可在[原项目 Release](https://github.com/aksjfds/trend-pyramiding-backtester/releases/tag/backtest-v0.21.1)取得同名 CSV。

```bash
pyramid-backtest backtest \
  --csv artifacts/hype-strategy-20260921/source/HYPE_1h.csv \
  --config config/default.toml \
  --strategy confirmed-pyramid \
  --output-dir artifacts/hype-confirmed
```

完整原版/新版、前后分段、双倍成本比较：

```bash
python scripts/compare_strategies.py \
  --csv artifacts/hype-strategy-20260921/source/HYPE_1h.csv \
  --split 2026-08-01T00:00:00Z \
  --output-dir artifacts/hype-recheck
```

比较脚本要求连续、排序且无重复的小时数据。分段从相同本金开始，保留之前数据预热指标，不携带前段仓位。输出完整净值、交易、事件、配置/数据哈希和比较表。
