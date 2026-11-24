# CF-GTM · Cloudflare 流量调度面板

> 桃花酱出品 · Telegram：[@taohuayuang](https://t.me/taohuayuang)

用 Cloudflare 免费 DNS 实现一个轻量版「全局流量管理」：

- 固定主域名（CNAME / A），节点故障时自动改 DNS
- 国内 ping + 本地 TCP 双探测，判断「正常 / 被墙 / 异常」
- 三种调度模式：主备切换、按权重轮换、多 A 记录轮询
- 节点增删、权重等级、上下排序、单独开关「参与自动切换」
- 自动切换时间段、切换冷却
- 单文件 Flask + 内置 Web 控制台，无数据库、无外网依赖
- 自带登录密码，可部署在宝塔 / 任意 Linux

---

## 截图

> 可自行补充：登录页、节点列表、探测设置

---

## 工作原理

```
浏览器 / 客户端
      │ 解析
      ▼
Cloudflare DNS（主域名 ddns.example.com，CNAME 到某个节点）
      ▲
      │ 脚本检测到异常后调用 CF API 修改 CNAME 目标
      │
CF-GTM（部署在一台能上网的机器上，香港 / 国内均可）
      │
      ├── 每 N 秒探测所有节点
      │     ├── 国内 ping（xxapi 或自建接口）
      │     └── 本地 TCP 端口
      │
      └── Web 控制台（登录后查看状态、改配置）
```

> 注意：探测「国内墙不墙」最好用**国内网络**的探测源。脚本本身可以跑在香港/境外，探测请求由国内 API 或自建国内探针发起。

---

## 快速开始

### 方式零：一键脚本（最简单，1 分钟）

在一台 Linux 服务器上（Debian/Ubuntu/CentOS 都行）用 root 执行：

```bash
curl -fsSL https://raw.githubusercontent.com/ergou-thy/CF-GTM-/main/install.sh | bash
```

脚本会自动：

1. 安装 python3 / venv / 依赖；
2. 从 GitHub 拉取代码到 `/opt/cf-gtm`；
3. 生成 `config.json`（随机面板密码、正式模式）；
4. 注册 systemd 服务并启动。

安装完成后会打印：

```
访问地址：http://你的服务器IP:8787
面板密码：xxxxxxxx
```

**不需要手动改配置文件**，直接打开地址登录，然后在面板里完成：

- 右上角「主域名」→ 填 CF Token、Zone ID、主域名；
- 「节点管理」→ 添加你的节点；
- 「探测设置」→ 可换成自己的国内探测接口。

自定义安装目录或端口：

```bash
CFGTM_DIR=/opt/cf-gtm CFGTM_PORT=8787 bash install.sh
```

> 8787 直接暴露公网不够安全，生产环境建议加 Nginx 反代 + HTTPS（见方式一）。

---

### 方式一：宝塔面板 + Supervisor（推荐生产）

> 宝塔环境也可以直接用一键脚本：脚本会自动识别宝塔（检测 `/www/server/panel`），把代码拉到 `/www/wwwroot/cf-gtm` 并写入 Supervisor 配置，不用手动上传文件。
>
> ```bash
> curl -fsSL https://raw.githubusercontent.com/ergou-thy/CF-GTM-/main/install.sh | bash
> ```
>
> 装完在宝塔 → Supervisor 管理器里能看到 `cf-gtm` 进程。下面的手动步骤留作参考。

#### 1. 上传代码

把 `app.py`、`requirements.txt`、`config.example.json` 上传到服务器：

```bash
mkdir -p /www/wwwroot/cf-gtm
cd /www/wwwroot/cf-gtm
# 上传文件后：
cp config.example.json config.json
```

#### 2. 安装 Python 依赖

```bash
# Debian/Ubuntu
apt-get update && apt-get install -y python3-venv python3-pip

# 创建虚拟环境
python3 -m venv venv
./venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
```

#### 3. 修改配置

编辑 `config.json`（首次启动也会自动生成）：

```json
{
  "cloudflare": {
    "api_token": "你的CF_TOKEN",
    "zone_id": "你的ZONE_ID",
    "record_name": "ddns.example.com",
    "record_type": "CNAME"
  },
  "probe": {
    "interval_seconds": 60,
    "fail_threshold": 2,
    "decision_mode": "api_only",
    "api_url": "https://v2.xxapi.cn/api/ping?url={target}"
  },
  "nodes": [
    {"name": "日本1", "target": "jp1.example.com", "port": 443, "weight": 3, "enabled": true},
    {"name": "新加坡1", "target": "sg1.example.com", "port": 443, "weight": 1, "enabled": true}
  ],
  "mode": "weighted",
  "auto_switch": true,
  "dry_run": false,
  "web": {"host": "0.0.0.0", "port": 8787, "password": "换成你的密码"}
}
```

各字段说明见下方「配置说明」。

#### 4. 添加 Supervisor 守护

宝塔 → 软件商店 → **Supervisor 管理器** → 添加守护进程：

| 项 | 填 |
|---|---|
| 名称 | cf-gtm |
| 运行目录 | /www/wwwroot/cf-gtm |
| 启动命令 | /www/wwwroot/cf-gtm/venv/bin/python /www/wwwroot/cf-gtm/app.py |
| 用户 | root |
| 进程数 | 1 |
| 开机自启 | 勾选 |

或者直接把仓库里的 `deploy/cf-gtm.ini` 放到宝塔的 Supervisor 配置目录：

```bash
cp deploy/cf-gtm.ini /www/server/panel/plugin/supervisor/profile/
/www/server/panel/pyenv/bin/supervisorctl -c /etc/supervisor/supervisord.conf reread
/www/server/panel/pyenv/bin/supervisorctl -c /etc/supervisor/supervisord.conf update
```

#### 5. Nginx 反向代理

宝塔 → 网站 → 添加站点（域名如 `gtm.example.com`，不建数据库/PHP）→ 设置 → 反向代理：

- 目标 URL：`http://127.0.0.1:8787`
- 发送域名：`$host`

或者用仓库里的 `deploy/nginx.conf.example`。

#### 6. 开放访问

- 防火墙放行 80 / 443；
- **不要**把 8787 暴露公网（只给本机反代）；
- 建议在 Cloudflare 上给面板域名开小黄云 + 强制 HTTPS。

访问 `https://gtm.example.com`，用 `web.password` 登录。

---

### 方式二：普通 Linux（systemd）

```bash
# 1. 上传代码到 /opt/cf-gtm
cd /opt/cf-gtm
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp config.example.json config.json
# 编辑 config.json

# 2. 安装 systemd 服务
cp deploy/cf-gtm.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cf-gtm

# 3. 检查
systemctl status cf-gtm
curl -s http://127.0.0.1:8787/login -o /dev/null -w '%{http_code}\n'
```

### 方式三：本地直接跑（调试）

```bash
pip install -r requirements.txt
python app.py
# 浏览器打开 http://127.0.0.1:8787
```

---

## 配置说明

| 字段 | 说明 |
|---|---|
| `cloudflare.api_token` | CF API Token，权限 `Zone → DNS → Edit` |
| `cloudflare.zone_id` | 域名 Overview 页右侧的 Zone ID |
| `cloudflare.record_name` | 要切换的主域名记录，如 `ddns.example.com` |
| `cloudflare.record_type` | `CNAME`（推荐）或 `A` |
| `probe.interval_seconds` | 探测间隔，最小 10 秒，建议 30~60 |
| `probe.fail_threshold` | 连续失败几次才切换/摘除，建议 2 |
| `probe.decision_mode` | `api_only`（只看国内 ping）/ `smart`（TCP 优先）/ `tcp_only` |
| `probe.api_url` | 探测接口，必须含 `{target}` 占位符 |
| `probe.record_ttl` | round_robin 模式创建的 A 记录 TTL，最低 60 |
| `nodes[].name` | 节点显示名 |
| `nodes[].target` | 节点 DDNS 域名或 IP |
| `nodes[].port` | TCP 探测端口 |
| `nodes[].weight` | 权重：4=最高 / 3=高 / 2=中 / 1=低 |
| `nodes[].enabled` | 是否参与自动切换（`true/false`） |
| `mode` | `failover` / `weighted` / `round_robin` |
| `auto_switch` | 自动切换总开关 |
| `auto_window` | 生效时间段（支持跨天） |
| `cooldown_minutes` | 两次切换的最小间隔 |
| `scheduler.slice_seconds` | weighted 模式「自动切换时间」 |
| `dry_run` | 演练模式：只记录不真改 |
| `web.host/port/password` | 面板监听与密码 |

### 三种模式怎么选

| 模式 | 行为 | 适合 |
|---|---|---|
| `failover` | 单条记录，主备切换 | 节点可能挂/被墙，要可用性 |
| `weighted` | 单条记录，按权重时间片轮换 | 想带一点负载均衡 |
| `round_robin` | 多条 A 记录等概率 | 节点都稳定，纯分摊（需主域名是 A 记录） |

> CNAME 主域名只能用 `failover` / `weighted`；`round_robin` 需要 A 记录。

### 权重说明

权重是**本地配置**，只影响 weighted 模式的轮换比例，不会写入 Cloudflare。
权重相同 = 严格轮流；不同 = 按比例轮换，不是随机。

---

## 探测源

### 默认：xxapi（第三方，免费）

```
https://v2.xxapi.cn/api/ping?url={target}
```

返回 `code=200` 表示通，`code=-2` 表示失败。免费接口有限流，适合小规模。

### 推荐：自建国内探针

见 [`probe-server/`](probe-server/)，部署在国内服务器上，返回同样的 JSON 格式，然后用：

```
http://你的国内IP:8080/ping?host={target}
```

自建探针用 TCP 探测，比 ICMP 更准，也不受限流影响。

---

## Cloudflare 准备

1. Cloudflare → My Profile → API Tokens → Create Token
2. 模板选 **Edit zone DNS**，权限 `Zone → DNS → Edit`
3. Zone Resources 选你的域名
4. 复制 Token 填到 `config.json`
5. 域名 Overview 页右侧复制 **Zone ID**

主域名记录先手工建好（比如 `ddns.example.com CNAME jp1.example.com`），脚本启动后会读取它。

---

## 安全建议

- 面板必须设密码（首次启动会自动生成随机密码，见 `config.json`）；
- 8787 只监听 `127.0.0.1`，用 Nginx 反代；
- 公网访问建议套 HTTPS，或给面板加 Cloudflare Access / IP 白名单；
- `config.json` 含 CF Token 和密码，**不要提交到公开仓库**（`.gitignore` 已排除）。

---

## 常见问题

**Q：面板显示「CF 异常」怎么办？**
检查 Token 是否有 `DNS:Edit` 权限、Zone ID 是否正确、服务器能否访问 `api.cloudflare.com`。

**Q：切换要多久？**
探测间隔 + 连续失败次数 + DNS TTL。默认 60s + 2 次 + 60s ≈ 2~3 分钟。

**Q：xxapi 挂了怎么办？**
`smart` 模式会退化为只看 TCP；也可以换成自建探针。

**Q：想先演练不真改？**
`config.json` 里 `dry_run` 设为 `true` 后重启，日志里会显示 `[DRY-RUN]`。

**Q：宝塔里看不到站点？**
站点需要通过面板创建（反向代理项目），直接写 nginx 配置不会出现在列表里。

---

## License

MIT
