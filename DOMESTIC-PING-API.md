# 国内 Ping API 独立部署说明

这是给桃花源 GTM 使用的国内 ICMP 探测接口。将 `cf-gtm-ping-api.sh` 安装到国内服务器，主控通过 HTTP(S) 请求接口，由这台国内服务器 Ping 节点。脚本内嵌完整 Python 服务，不从 GTM 站点下载源码，不需要向 GTM 提供服务器 SSH 账号。

GTM 主控的安装、Cloudflare 配置与调度模式见 [README](README.md)。本脚本只管理独立 API，不改主控、Cloudflare DNS、防火墙或云安全组。

## 安装

要求 root、运行中的 systemd，支持 Debian / Ubuntu 与 RHEL 系 Linux。使用系统 Python 3.6 或更新版本、Ping 和 curl，脚本通过系统包管理器补齐依赖；服务仅用 Python 标准库，无需 pip 或数据库。

在国内服务器的 root 终端执行：

```bash
curl -fsSL --retry 2 https://raw.githubusercontent.com/ergou-thy/CF-GTM/main/cf-gtm-ping-api.sh -o /tmp/cf-gtm-ping-api.sh && bash /tmp/cf-gtm-ping-api.sh
```

无法访问 GitHub Raw 时，手动下载该 `.sh` 文件并上传后执行即可，不需要其他文件。控制台下载的 `tools/install-ping-api.sh` 与根目录脚本内容相同。

按提示填写：

1. **国内服务器公网域名或 IP**：用于生成回填地址，不包含协议、端口或路径。请填写主控实际能访问的地址；使用主机名默认值时尤其要核对可达性。
2. **允许来源 IP/CIDR**：可选，多个用逗号分隔；建议填写 GTM 主控的实际出口公网地址。新装留空不限制来源，但 `/ping` 仍必须携带 token。

默认监听 `0.0.0.0:8788`，服务名 `cf-gtm-ping-api`，由独立低权限账号运行并开机启动。请自行在防火墙和云安全组中**仅允许 GTM 主控访问 TCP 8788**。

### 可选安装变量

| 变量 | 含义 |
|---|---|
| `GTM_PUBLIC_HOST` | 公网域名或 IP，只用于生成回填 URL，不改变监听地址。 |
| `GTM_API_PORT` | 监听端口，默认 `8788`。 |
| `GTM_ALLOWED_CLIENTS` | 来源 IP/CIDR，逗号分隔；显式设为空字符串会清空白名单。 |
| `GTM_API_TOKEN` | 自定义 32～256 位 `A-Z/a-z/0-9/_/-` 密钥；不设置时新装随机生成，升级保留旧值。 |

例如，在执行前将中文占位内容换成真实值：

```bash
GTM_PUBLIC_HOST='国内服务器公网域名或IP' \
GTM_ALLOWED_CLIENTS='主控实际出口公网IP/32' \
GTM_API_PORT=8788 \
bash /tmp/cf-gtm-ping-api.sh
```

## 回填 GTM

安装完成会打印含独立密钥的完整地址：

```text
http://国内服务器:8788/ping?host={target}&token=安装时生成的密钥
```

1. 复制脚本**实际输出**的地址，填入“调度策略 → 健康探测 → 国内探测接口”。
2. 保留 `{target}` 原样，由 GTM 替换成节点目标。旧参数别名 `url={target}` 也支持，但不能同时传 `host` 和 `url`。
3. 健康判定选择 **仅国内 ICMP（`api_only`）**，单次超时建议 **至少 6 秒**。
4. 保存后点击“立即探测”，确认结果正常，再按需启用自动切换。需要验证切换计划时先开启演练模式。

接口地址包含密钥，不要公开粘贴、截图或提交到仓库。公网传输建议使用受信任的 HTTPS 反向代理，并将回填 URL 改为代理地址；同时限制后端 `8788`，避免绕过代理。若配置来源白名单，应按 API 实际看到的连接来源设置：服务不信任 `X-Forwarded-For` 等转发头，同机反代可能显示为本机来源，主控来源限制应同时在代理或防火墙落实。

## 接口行为

- 只接受公网单播 IP，或所有 A/AAAA 解析结果都为公网 IP 的域名；不接受内网目标、完整 URL、附带端口或 shell 参数。
- 每次执行两包 ICMP 探测，总预算 5 秒，默认最多 8 个并发请求。
- `code=200`：完整统计中收到回包，`data.time` 为平均延迟（如 `12.3ms`），`data.server` 为服务名称。
- `code=-2`：完成两包探测且零回包。
- `code=-1` 或 HTTP 错误：解析、权限、系统、鉴权、参数或其他无法确认的结果；GTM 应按未知处理，不能当作明确的两包全丢。
- `/health` 仅返回本机服务是否能响应，不执行公网 Ping，不验证节点或国内网络的实际可达性。

所有 `/ping` 请求都需要 token，来源白名单不能替代密钥。ICMP 可达性不等于应用端口可用，需结合自己的业务和 GTM 判定模式选择探测方法。

## 维护与卸载

```bash
# 状态与最近日志
systemctl status cf-gtm-ping-api --no-pager
journalctl -u cf-gtm-ping-api -n 50 --no-pager

# 重启
systemctl restart cf-gtm-ping-api

# 本机健康检查；自定义端口时相应调整
curl --noproxy '*' -fsS http://127.0.0.1:8788/health

# 卸载，按提示确认；若 /tmp 脚本已清理，先重新下载
bash /tmp/cf-gtm-ping-api.sh --uninstall
```

**更新**：重新下载新版脚本并执行同一安装命令。默认保留原密钥、端口和来源限制，只有显式设置的 `GTM_*` 变量才覆盖相应配置。更新前备份配置；发生安装、启动或健康检查失败时，安装器会尝试恢复更新前的受管文件和服务状态，仍应检查最终服务状态。

固定文件位置：

```text
/opt/cf-gtm-ping-api/ping_api.py
/etc/cf-gtm-ping-api/config.json
/etc/systemd/system/cf-gtm-ping-api.service
```

配置文件权限为 `0600`，保存 token，请限制读取并保护备份。服务自身不记录带密钥的请求 URL；如加反向代理，也应避免在访问日志中保存含 token 的完整查询串。卸载只删除安装器拥有的固定文件和空目录，保留目录中的其他文件。

## 排查顺序与验证范围

先用 `systemctl`、`journalctl` 和本机 `/health` 检查服务，再检查主控到 API 的网络、防火墙、token 和来源白名单，最后在 GTM 点击“立即探测”验证公网目标。HTTP 可访问但结果未知时，检查系统 Ping 权限、DNS 解析和目标是否满足公网限制；结果为 `-2` 时也应确认节点是否允许 ICMP。

本轮使用当前发布文件在 Windows / Python 3.12 上核验：**67 项适用测试中 66 项通过、1 项跳过**，另有 **4 项内嵌源码、脚本副本、固定摘要及 Python 3.6 语法一致性检查通过**。旧版要求“从 GTM 下载源码”的静态断言不适用于当前内嵌实现，未计入适用测试；Windows 无 POSIX 配置权限检查因此跳过。

此前已有 **76 项 Linux 隔离测试**通过记录，覆盖 API、单文件安装、升级保留配置与失败恢复等路径，本轮未重跑 Linux 安装流程。这不等同于在所有发行版或真实国内服务器上完成部署；实际公网连通性、系统 ICMP 权限及安全组需要安装后验证。
