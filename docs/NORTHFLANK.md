# Northflank 部署

这是常驻后台交易程序，不需要 Web 端口。根目录已提供 `Dockerfile`，默认启动 `pyramid-okx watch`：每 5 分钟执行只读账户检查，日志不会输出密钥或余额，也不会改杠杆或下单。

## 创建服务

1. 把代码提交到你连接 Northflank 的 Git 仓库。API 密钥只在平台运行时环境填写，不要提交到仓库。
2. 在 Northflank 项目中创建 **Combined service**，选择仓库与分支，使用 **Dockerfile** 构建。Dockerfile 路径为 `/Dockerfile`，构建上下文为 `/`。
3. 实例数设为 **1**，不启用水平自动扩缩容或多实例。无需添加端口或公开域名。可先分配 1 vCPU / 512 MiB，之后根据实际内存占用调整。
4. 添加 **Single Read/Write** 持久化卷，挂载至 **`/data`**，容量可从 1 GiB 开始。该模式更新时先终止旧容器，避免两个交易进程同时运行。
5. 保留默认启动命令，设置下面的运行时凭据，先观察只读检查日志。

镜像以 UID/GID `10001:10001` 运行。Northflank 会按镜像用户组配置卷权限；若迁移的旧卷仍归其他用户所有，需要先调整卷内目录所有权，不要把服务长期改为 root。程序不会在缺少 `/data` 挂载时启动交易。

## API 密钥：仅使用环境变量

在 Northflank 服务的 **Environment** 页面添加以下 **Runtime variables**，值填写在平台的保密输入区域：

| 实盘变量名 | 内容 |
| --- | --- |
| `OKX_API_KEY` | OKX API Key |
| `OKX_API_SECRET` | OKX Secret Key |
| `OKX_API_PASSPHRASE` | 创建 API 时设置的 Passphrase |

也可以创建 **Secret Group**，Scope 选择 **Runtime**，限制为仅当前交易服务可用。保存后重启服务，使新变量生效。先保留默认 `pyramid-okx watch`，或运行 `pyramid-okx check` 验证只读连接。

模拟盘单独填写 `OKX_DEMO_API_KEY`、`OKX_DEMO_API_SECRET`、`OKX_DEMO_API_PASSPHRASE`，并使用 `--demo`。两套变量互不回退；所选环境的三个值均须非空，否则程序在连接 OKX 前报错，仅提示缺失变量名。

程序只读取进程环境变量，不自动加载 `.env`。已有部署升级时，应先填齐运行时变量，移除原有密钥文件挂载及对应路径变量，再重启服务。

密钥只配置为运行时变量，不要放入 Build arguments、Dockerfile、策略配置或日志。限制平台密钥查看和容器终端权限，不要打印完整环境变量。镜像不包含 API 密钥。

如果 API Key 绑定了 IP，需要在 OKX 核对 Northflank 实际出口 IP；应用的公开域名并不是出口 IP。部署地区和 API 域名也应与你的 OKX 账户匹配。

## 运行参数

这些环境变量已在镜像设置，通常无需修改：

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `PYRAMID_CONFIG` | `/app/config/okx.toml` | 策略运行配置路径 |
| `PYRAMID_STATE_DIR` | `/data/state` | 仓位、订单意图、暂停标记和事件记录 |
| `PYRAMID_STATE_MOUNT` | `/data` | 必须实际挂载的持久化卷 |
| `PYRAMID_REQUIRE_PERSISTENT_STATE` | `true` | 缺少持久化卷时拒绝交易 |
| `PYRAMID_HEARTBEAT_FILE` | `/tmp/pyramid-heartbeat.json` | 当前进程的健康状态，不包含账户数据 |

`--state-dir` 可覆盖状态路径，但仍必须位于持久化卷内。状态目录属于部署环境，不参与策略指纹；迁移原有状态不因路径改变而失效。

币对筛选、1H、2 倍逐仓、20% 额度仍由 `config/okx.toml` 管理。需要改参数时可提交配置变更，或挂载运行时配置文件并设置 `PYRAMID_CONFIG`；其 `strategy_config` 路径相对于该配置文件所在目录，需同时提供对应策略配置。已有持仓时不要随意更换参数或删除状态。

## 健康检查

在 Northflank Advanced / Health checks 添加 **CMD** 检查：

| 项目 | 配置 |
| --- | --- |
| Liveness command | `pyramid-health` |
| Readiness command | `pyramid-health --ready` |
| Initial delay | 120 秒 |
| Period | 30 秒 |
| Timeout | 10 秒 |
| Failure threshold | 3 |

健康命令只读当前容器内的心跳文件，不访问 OKX，也不会下单。默认容忍 600 秒无进展。Readiness 表示最近的检查或交易循环成功；Liveness 用来识别进程退出或卡住。因交易异常而暂停时，进程可存活，但 Readiness 不通过。

## 检查与启用

默认只读部署成功时，日志应出现：

```json
{"event":"read_only_check","authenticated":true,"orders_enabled":false}
```

可在 Northflank 容器终端运行 `pyramid-okx check` 查看余额和账户检查结果，`pyramid-okx status` 查看本地状态。只读部署正常不代表余额满足最小下单量，也不代表真实订单/止损已验证。

用户准备启用后，在服务的 Docker **CMD override** 中设置对应命令：

```text
pyramid-okx run --demo
```

或实盘：

```text
pyramid-okx run --live
```

不要把实盘 `run` 配置为构建步骤、健康检查、定时 Job 或并行服务。本文不自动部署、启用实盘或提交测试订单。

## 更新、暂停与恢复

- 容器收到 SIGTERM/SIGINT 时，会停止发起新入场，允许当前订单完成核对与止损确认，再退出。已有交易所端止损保留，不自动平仓。Northflank 的终止宽限期可设为 300 秒；若平台最终强制终止，重启仍依赖持久化订单意图核对。
- 未处理异常会写入 `/data/state/okx-live.halted.json`（模拟盘对应 demo）。平台重启后看到标记即进入暂停，不因自动重启重新交易。
- 暂停时先检查日志、OKX 仓位、止损及待确认订单。解决后在容器终端执行 `pyramid-okx clear-halt`，再手动重启服务。清除标记不会清除仓位和订单记录，也不会直接恢复当前进程的交易。
- 不要删除 `/data/state/okx-live.json` 来解决错误。结果不明的订单仍按客户端订单号核对，不自动重发。
- 只运行一个使用该 API 账户的策略进程。文件锁只能约束共享同一卷的进程，不能阻止另一台机器或另一个独立卷同时操作账户。
- 如果本地已有运行状态，先停止本地进程，复制整个对应状态目录到持久化卷，保持配置一致并确认权限；不要同时在本地和云端运行。
- 事件日志当前保存在卷上的 `.events.jsonl`，没有自动轮转，需关注卷使用量。

## 本地构建检查

```bash
docker build -t trend-pyramiding-okx .
docker run --rm trend-pyramiding-okx pyramid-okx --help
docker run --rm trend-pyramiding-okx pyramid-okx scan
```

这些命令不使用本地 API 凭据，也不启动交易。

官方依据：[创建服务](https://northflank.com/docs/v1/application/getting-started/build-and-deploy-your-code)、[持久化卷](https://northflank.com/docs/v1/application/databases-and-persistence/add-a-volume)、[Secret Groups](https://northflank.com/docs/v1/application/secure/manage-secret-groups)、[运行时变量](https://northflank.com/docs/v1/application/secure/inject-secrets)。
