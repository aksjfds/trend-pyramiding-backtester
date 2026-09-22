# Trend Pyramiding Backtester

一个独立的 Python 回测项目，用来验证：

**顺势金字塔加仓（Pyramiding / Anti-Martingale） + 结构/ATR 止损 + 移动止盈。**

项目重点不是预测“哪个币会涨”，而是：当多头信号出现后，怎样只向盈利仓加仓、限制总风险，并尽量保留趋势行情的右尾收益。

## 核心规则

- 只做多。
- 信号使用最新已确认收盘 K 线。实盘首笔开仓候选不会随新 K 自动生成；只有用户在网页点击“生成候选”时才扫描交易所当前全部受支持的 USDT 永续合约；符合条件的候选按 OKX 24 小时成交量从高到低排列。随后用户选择候选开仓，worker 再次核验行情、预算、最小张数和止损后提交订单。
- 默认四档风险权重：30% / 30% / 20% / 20%。
- 首笔开仓支持两种人工入口：从策略生成的候选中确认，或直接指定任一当前支持的 USDT 永续合约。无需预先保存交易币种列表；只有实际开仓的币种才加入策略管理。指定币种不要求满足候选信号，但仍按最新已收盘 K 线计算策略初始止损和资金张数。首仓成交后，策略自动接管该币种；只给盈利仓加仓，价格相对上次成交继续上涨 0.75 ATR 后允许下一档加仓。
- 初始止损取结构止损与 2 ATR 止损中更紧的一项。
- 单次完整交易的开放风险默认不超过账户权益的 1%。
- 达到 1R 后允许把止损抬到保本区域，并启用结构/ATR 移动止盈。
- 最大名义仓位默认不超过账户权益的 100%，不使用杠杆。
- 回测计入双边手续费和滑点。

## Buy & Hold 基准

每次回测都会自动运行一个独立的 1x Buy & Hold 基准，用于回答“策略是否优于直接持有”。

基准规则：

- 使用与策略相同的初始资金；
- 第一根 K 线开盘一次性买入；
- 最后一根 K 线收盘一次性卖出；
- 不加杠杆；
- 使用与策略相同的 fee_bps 和 slippage_bps；
- 不进行中途加仓、减仓或择时。

每次回测会输出：

- summary.json — 策略指标
- equity_curve.csv — 策略资金曲线
- trades.csv — 策略逐笔交易
- events.csv — 策略执行事件
- benchmark_summary.json — Buy & Hold 指标
- benchmark_equity_curve.csv — Buy & Hold 资金曲线
- comparison.json — 策略相对基准的比较

comparison.json 包含：

- excess_return_pct：策略收益率 - Buy & Hold 收益率
- drawdown_advantage_pct：Buy & Hold 最大回撤绝对值 - 策略最大回撤绝对值
- sharpe_delta：策略 Sharpe - Buy & Hold Sharpe
- final_equity_difference：两者最终权益差

## 安装

OKX 实盘功能已提供独立命令 `pyramid-okx`，支持逐仓 USDT 永续合约，周期、杠杆和本金使用比例可直接在顶部“策略参数”和“资金使用上限”卡片中修改；总额度包含保证金和费用预留。详见 [OKX 配置与运行说明](docs/OKX_LIVE.md)。账户只读检查与实盘启动分开，实盘需要明确点击网页的“启动实盘交易”，或执行 `pyramid-okx run --live`。

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

## 本地网页控制台

在终端运行：

```bash
cd /Users/a/code/trend-pyramiding-backtester
.venv/bin/pyramid-web --open
```

自动读取本机 `config/okx.credentials.toml` 中已保存的密钥，无需再次输入。密钥文件不提交 Git；Render 仍只使用环境变量。

网页地址为 `http://127.0.0.1:8765`。打开后自动只读加载交易账户余额，每分钟刷新，也可手动刷新。无需预选或保存交易币种；启动策略后，在开仓控制中直接指定币种，或按需生成全市场候选。打开网页不会自动启动交易。

该命令以前台方式持续占用当前终端；网页启动的策略进程也保持在同一终端会话，运行日志会同步输出到终端。按 Ctrl-C 会请求网页服务和策略安全停止。若 8765 端口已有旧网页进程，命令会直接报错，不再静默复用后台进程。网页支持启动、停止和查看；停止会等待当前操作完成，保留已有仓位和交易所止损。详见 [网页使用说明](docs/WEB.md)。

## 输入数据

CSV 至少包含：

```text
timestamp,open,high,low,close,volume
```

如果上游模型或人工判断已经给出多头信号，可以额外提供：

```text
signal_long
```

## 回测

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

如果不传 --signal-column，项目使用 EMA20 + 前 20 根高点突破作为默认演示入场信号。

## GitHub Actions / Release

每次 main 分支 push 或手动运行都会：

1. 安装项目；
2. Ruff 静态检查；
3. Pytest 单元测试；
4. 执行回测；
5. 执行 Buy & Hold 基准；
6. 对 regression fixture 运行性能回归阈值检查；
7. 生成 Release Markdown 报告；
8. 发布 Release 和完整 CSV/JSON 结果。

Release 不再生成或上传交易 K 线图。

Release 正文优先展示：

- 策略 vs Buy & Hold 总收益
- 超额收益
- 最终权益
- 最大回撤
- Sharpe
- 策略交易次数、胜率、Profit Factor、Average R
- 回测市场、区间、K 线数量与数据源

## 参数

全部默认参数位于 config/default.toml。

主要参数：

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

benchmarks/baseline.json 仅用于 CI regression gate，它不是交易基准。交易基准是每次运行时动态计算的 Buy & Hold。

## Render 部署

仓库包含 `render.yaml`。在 Render 创建 Blueprint，填写三个 OKX 环境变量即可部署带密码保护的网页控制台；网页密码由 Render 自动生成。使用付费单实例和持久化磁盘保存交易状态，重启后手动启动策略。详细步骤见 [Render 部署说明](docs/RENDER.md)。

长期运行和故障恢复说明：[本地与云端运行维护](docs/RELIABILITY.md)。
