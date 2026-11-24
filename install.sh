#!/usr/bin/env bash
# CF-GTM 一键安装脚本
# 用法：curl -fsSL https://raw.githubusercontent.com/227696934/CF-GTM-/main/install.sh | bash
# 可选环境变量：CFGTM_DIR=/opt/cf-gtm  CFGTM_PORT=8787

set -e

REPO="227696934/CF-GTM-"
BRANCH="main"
DIR="${CFGTM_DIR:-/opt/cf-gtm}"
PORT="${CFGTM_PORT:-8787}"

RED='\033[31m'; GREEN='\033[32m'; YELLOW='\033[33m'; NC='\033[0m'
info(){ echo -e "${GREEN}[CF-GTM]${NC} $1"; }
warn(){ echo -e "${YELLOW}[CF-GTM]${NC} $1"; }
err(){ echo -e "${RED}[CF-GTM]${NC} $1"; }

if [ "$(id -u)" -ne 0 ]; then
  err "请用 root 运行：sudo bash install.sh"
  exit 1
fi

# ---------- 1. 安装依赖 ----------
info "检查系统依赖..."
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip curl tar >/dev/null
elif command -v yum >/dev/null 2>&1; then
  yum install -y -q python3 python3-pip curl tar >/dev/null
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y -q python3 python3-pip curl tar >/dev/null
else
  err "没识别到 apt/yum/dnf，请手动安装 python3、python3-venv、curl"
  exit 1
fi

# ---------- 2. 下载代码 ----------
if [ -f "$DIR/app.py" ]; then
  info "检测到 $DIR 已有代码，跳过下载"
else
  info "下载代码到 $DIR ..."
  mkdir -p "$DIR"
  TMP=$(mktemp -d)
  URL="https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz"
  if ! curl -fsSL --max-time 60 "$URL" -o "$TMP/src.tar.gz"; then
    warn "GitHub 直连失败，尝试镜像..."
    curl -fsSL --max-time 60 "https://ghproxy.net/$URL" -o "$TMP/src.tar.gz"
  fi
  tar xzf "$TMP/src.tar.gz" -C "$TMP"
  cp -r "$TMP"/*/* "$DIR"/
  rm -rf "$TMP"
fi
cd "$DIR"

# ---------- 3. Python 环境 ----------
info "创建虚拟环境并安装依赖..."
[ -x venv/bin/python ] || python3 -m venv venv
./venv/bin/pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# ---------- 4. 生成配置 ----------
if [ ! -f config.json ]; then
  info "生成默认配置..."
  cp config.example.json config.json
  ./venv/bin/python - <<'PY'
import json, secrets
p = "config.json"
d = json.load(open(p, encoding="utf-8"))
d["dry_run"] = False
d["web"]["host"] = "0.0.0.0"
d["web"]["port"] = 8787
d["web"]["password"] = secrets.token_urlsafe(9)
json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(d["web"]["password"])
PY
  PASS=$(./venv/bin/python -c "import json;print(json.load(open('config.json'))['web']['password'])")
else
  PASS=$(./venv/bin/python -c "import json;print(json.load(open('config.json'))['web']['password'])" 2>/dev/null || echo "见 config.json")
fi

# ---------- 5. systemd 服务 ----------
info "配置 systemd 服务..."
cat > /etc/systemd/system/cf-gtm.service <<EOF
[Unit]
Description=CF-GTM (Cloudflare DNS failover panel)
After=network.target

[Service]
Type=simple
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python $DIR/app.py
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable cf-gtm >/dev/null 2>&1
systemctl restart cf-gtm

sleep 3
if systemctl is-active --quiet cf-gtm; then
  info "服务已启动"
else
  err "服务启动失败，查看日志：journalctl -u cf-gtm -n 50"
  exit 1
fi

IP=$(curl -fsSL --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')

echo
echo "============================================================"
echo -e "${GREEN} CF-GTM 安装完成${NC}"
echo "============================================================"
echo " 访问地址：http://$IP:$PORT"
echo " 面板密码：$PASS"
echo " 配置目录：$DIR/config.json"
echo
echo " 下一步："
echo "   1. 打开上面的地址，用密码登录"
echo "   2. 右上角「主域名」填 CF Token、Zone ID、主域名"
echo "   3. 「节点管理」添加你的节点"
echo "   4. 「探测设置」可换成自己的国内探测接口"
echo
echo " 常用命令："
echo "   systemctl status cf-gtm      # 状态"
echo "   systemctl restart cf-gtm     # 重启"
echo "   journalctl -u cf-gtm -f      # 日志"
echo
echo -e "${YELLOW} 注意：8787 直接暴露公网不安全，建议加 Nginx 反代 + HTTPS${NC}"
echo "============================================================"
