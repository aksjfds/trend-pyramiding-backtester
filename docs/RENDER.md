# 部署到 Render

项目提供 `render.yaml` Blueprint，运行同一个网页控制台：启动、停止、只读账户检查、持仓与止损、运行日志。OKX 密钥只读环境变量，不读取凭据文件或 `.env`，也不通过网页填写。

## 创建服务

1. 将本次改动提交并推送到你连接 Render 的 Git 仓库。
2. Render → **New → Blueprint**，连接仓库，选择包含 `render.yaml` 的分支。
3. 创建时填写 `OKX_API_KEY`、`OKX_API_SECRET`、`OKX_API_PASSPHRASE` 三个环境变量的实际值。
4. 确认付费实例和 1 GB 持久化磁盘后部署。默认单实例 `0.5c-512mb`，费用以 Render 创建页为准。
5. 服务显示 Live 后，到 **Environment** 查看自动生成的 `PYRAMID_WEB_PASSWORD`。打开服务的 HTTPS 地址，浏览器弹出登录框：用户名 **admin**，密码为该变量的值。
6. 先点击网页 **刷新账户**，确认账户与权限正常。再选择只读观察、模拟交易或实盘交易并点击启动。

部署成功只表示网页可用；首次部署、服务重启、更新代码后策略均为未运行，需要登录网页手动启动。健康检查不会访问 OKX 或启动交易。

## 环境变量

| 变量 | 用途 |
| --- | --- |
| `OKX_API_KEY` | 实盘 API Key |
| `OKX_API_SECRET` | 实盘 Secret Key |
| `OKX_API_PASSPHRASE` | 创建 API 时的 Passphrase |
| `PYRAMID_WEB_PASSWORD` | 网页密码，Blueprint 自动生成；手动设置时使用至少 24 字符的随机密码 |
| `PYRAMID_CONFIG` | 默认 `config/okx.toml`，策略配置仍在仓库中 |
| `PYRAMID_STATE_DIR` | `/var/data/pyramid`，订单意图、持仓、事件日志、暂停标记与锁文件目录 |
| `PYRAMID_DISK_PATH` | `/var/data`，必须是真实挂载的持久化磁盘 |
| `PORT` | Render 自动提供，默认 `10000` |
| `RENDER_EXTERNAL_URL` | Render 自动提供，用于验证访问域名与请求来源 |
| `PYRAMID_PUBLIC_URL` | 可选；使用自定义域名时设为完整 HTTPS 根地址，例如 `https://trade.example.com`。设置后仅接受该域名访问控制台 |

模拟盘另加 `OKX_DEMO_API_KEY`、`OKX_DEMO_API_SECRET`、`OKX_DEMO_API_PASSPHRASE`。密钥和网页密码都不要提交到 Git，也不要填入 `render.yaml`。Environment 中修改变量并重新部署后生效。网页密码与 OKX API 密钥是不同的值。

网页登录使用浏览器自带的 HTTP Basic 登录框，Render 在入口提供 HTTPS。浏览器可能缓存登录信息；共享电脑使用独立隐私窗口，用完关闭全部该隐私窗口。更换网页密码并重新部署后旧密码立即失效。

## 手动创建 Web Service

不使用 Blueprint 时，在 **New → Web Service** 中填写：

| 设置 | 值 |
| --- | --- |
| Language | Python 3 |
| Build Command | `pip install '.[cloud]'` |
| Start Command | `pyramid-render` |
| Health Check Path | `/healthz` |
| Instance | 付费实例，单实例 |
| Auto-Deploy | Off |
| Shutdown Delay | 300 秒 |
| Disk | 挂载 `/var/data`，1 GB 起 |

同时填写上表中的三个 OKX 变量、网页密码，以及两个磁盘目录变量。`.python-version` 指定 Python 3.14 的最新补丁版本。云端入口使用 Waitress，监听 `0.0.0.0:$PORT`；本机入口仍为 `pyramid-web --open`，仅监听回环地址。

## 状态保留与更新

Render 的普通文件系统会随重新部署丢失。必须使用付费实例和持久化磁盘；本项目在磁盘未挂载或状态目录不在磁盘内时拒绝启动，不会退回临时目录。不要用免费实例承载交易。

一个账户只运行一个交易实例。持久化磁盘限制服务单实例，应用也用文件锁阻止同一状态目录同时启动多个控制台或策略。不要额外创建另一套服务操作同一账户。

从本机迁移时，先正常停止本机策略。若已有策略持仓、未决订单或暂停记录，需将整个原 `state/` 目录内容迁移到云端 `/var/data/pyramid/`，核查账户后再启动，不能以空状态接管原策略持仓。密钥无需迁移文件，只填环境变量。

自动部署默认关闭。更新前在网页点击停止，等显示未运行后，使用 Render 的 **Manual Deploy → Deploy latest commit**。服务收到终止信号也会请求策略正常退出，等待当前订单核对和保护操作完成；Render 最多等待 300 秒，超时仍会强制结束。已有交易所止损保留，程序停止期间不再移动止损或加仓。

重新部署不会自动恢复交易。登录后查看策略记录并检查账户，再手动启动；未决订单或不一致状态仍由现有运行器核对，异常保留暂停标记。不要删状态文件绕过暂停。

页面最近 300 条日志在重启后清空；历史交易事件保存在磁盘的 `okx-live.events.jsonl`（模拟盘为 `okx-demo.events.jsonl`）。需要解除异常暂停时，先核查订单，再在 Render **Shell** 执行 `pyramid-okx clear-halt`；它会沿用环境变量指定的状态目录。模拟盘加 `--demo`。

若 OKX API 设置了 IP 白名单，把该 Render 服务显示的出站 IP 范围加入白名单，随后通过网页只读检查验证。部署地区需符合你的 OKX 账户访问要求。

Render 资料：[Web Services](https://render.com/docs/web-services)、[持久化磁盘](https://render.com/docs/disks)、[Blueprint 配置](https://render.com/docs/blueprint-spec)。

网页打开后自动只读加载余额，每分钟刷新。不再预选或保存交易币种；启动策略后，在开仓控制中直接指定币种，或点击“生成候选”扫描当前全部受支持的 USDT 永续合约。实际开仓后的币种与交易状态一起存放在持久化磁盘中。

运行阶段的断网恢复、停止行为与日志保留说明见 [运行维护说明](RELIABILITY.md)。临时行情问题不会触发网页健康检查失败或反复重启服务；订单不确定性仍保留暂停标记。网页服务意外退出时，所属策略子进程会请求安全退出，避免留下不受网页管理的进程。
