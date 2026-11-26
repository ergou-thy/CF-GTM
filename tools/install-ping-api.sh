#!/usr/bin/env bash
# 国内 Ping API 安装器；仅管理下列固定路径，不改防火墙、主控或 DNS。
set -Eeuo pipefail
umask 077

SERVICE='cf-gtm-ping-api'
SERVICE_USER='cf-gtm-ping'
INSTALL_DIR='/opt/cf-gtm-ping-api'
CONFIG_DIR='/etc/cf-gtm-ping-api'
SOURCE_FILE='/opt/cf-gtm-ping-api/ping_api.py'
CONFIG_FILE='/etc/cf-gtm-ping-api/config.json'
UNIT_FILE='/etc/systemd/system/cf-gtm-ping-api.service'
INSTALL_MARKER='/opt/cf-gtm-ping-api/.managed-by-installer'
CONFIG_MARKER='/etc/cf-gtm-ping-api/.managed-by-installer'
LOCK_DIR='/run/lock/cf-gtm-ping-api.install.lock'
# 发布时由真实源码计算并固定；修改源码后必须同步此摘要。
SOURCE_SHA256='88a69d611746a79ac62094849ad61a58572d68affbcf2ce37181aadebc215c46'
SOURCE_OVERRIDE=''
UNINSTALL=0
ASSUME_YES=0
WORK_DIR=''
LOCK_HELD=0
MUTATED=0
COMMITTED=0
CREATED_USER=0
OLD_ACTIVE=0
OLD_ENABLED=0
OLD_INSTALL_DIR=0
OLD_CONFIG_DIR=0
MANAGED_FILES=("$SOURCE_FILE" "$CONFIG_FILE" "$UNIT_FILE" "$INSTALL_MARKER" "$CONFIG_MARKER")
PENDING_FILES=()

usage() {
    cat <<'EOF'
国内 Ping HTTP API 安装器（需要 root 和运行中的 systemd）

用法：
  bash install-ping-api.sh [--source /绝对路径/ping_api.py]
  bash install-ping-api.sh --uninstall [--yes]
  bash install-ping-api.sh --help

本脚本内嵌完整 Python 服务，无需下载第二个文件，也不依赖 GTM 站点。
内嵌源码及 --source 指定源码均核对固定 SHA256。
升级保留旧 token 和配置；显式设置的环境变量才覆盖对应项。

环境变量：
  GTM_API_TOKEN        32～256 位 A-Z/a-z/0-9/_/-；未设置时保留旧值或随机生成
  GTM_ALLOWED_CLIENTS  允许的真实来源 IP/CIDR，逗号分隔；空字符串清空白名单
  GTM_PUBLIC_HOST      公网域名或 IP，仅用于生成可回填的完整 URL（不含协议或端口）
  GTM_API_PORT         监听端口，默认 8788

所有 /ping 请求仍必须携带 token，白名单不能替代 token。
不会自动开放防火墙；HTTP 查询参数含 token，建议通过 HTTPS 反向代理使用。
卸载只删除本安装器拥有的固定文件，保留目录内其他文件。
EOF
}

fail() { printf '错误：%s\n' "$*" >&2; exit 1; }
info() { printf '%s\n' "$*"; }

while (($#)); do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --source) (($# >= 2)) || fail '--source 缺少路径'; SOURCE_OVERRIDE=$2; shift 2 ;;
        --uninstall) UNINSTALL=1; shift ;;
        --yes) ASSUME_YES=1; shift ;;
        *) fail "未知参数：$1" ;;
    esac
done
((EUID == 0)) || fail '请使用 root 运行安装器'
((ASSUME_YES == 0 || UNINSTALL == 1)) || fail '--yes 仅适用于 --uninstall'
[[ -z "$SOURCE_OVERRIDE" || "$UNINSTALL" == 0 ]] || fail '卸载时不能指定 --source'
command -v systemctl >/dev/null || fail '未找到 systemctl，仅支持 systemd 系统'
systemctl show-environment >/dev/null 2>&1 || fail 'systemd 未运行，无法安装服务'

# 拒绝固定路径上的符号链接，防止 root 写入或删除不相关的位置。
check_paths() {
    local item
    for item in /opt /etc /etc/systemd /etc/systemd/system "$INSTALL_DIR" "$CONFIG_DIR" "${MANAGED_FILES[@]}"; do
        [[ ! -L "$item" ]] || fail "固定路径不能是符号链接：$item"
    done
    for item in "$INSTALL_DIR" "$CONFIG_DIR"; do
        [[ ! -e "$item" || -d "$item" ]] || fail "目标不是目录：$item"
    done
    for item in "${MANAGED_FILES[@]}"; do
        [[ ! -e "$item" || -f "$item" ]] || fail "目标不是普通文件：$item"
    done
}

verify_ownership() {
    if [[ -d "$INSTALL_DIR" ]]; then
        [[ -f "$INSTALL_MARKER" ]] || fail "目录缺少本安装器标记，停止操作：$INSTALL_DIR"
        [[ $(head -n 1 "$INSTALL_MARKER") == 'cf-gtm-ping-api:v1' ]] || fail '安装标记不匹配'
    fi
    if [[ -d "$CONFIG_DIR" ]]; then
        [[ -f "$CONFIG_MARKER" ]] || fail "目录缺少本安装器标记，停止操作：$CONFIG_DIR"
        [[ $(head -n 1 "$CONFIG_MARKER") == 'cf-gtm-ping-api:v1' ]] || fail '配置标记不匹配'
    fi
    if [[ -f "$UNIT_FILE" ]]; then
        [[ -f "$INSTALL_MARKER" && -f "$CONFIG_MARKER" ]] || fail '现有同名 unit 不属于本安装器'
        grep -qxF '# Managed by cf-gtm-ping-api installer' "$UNIT_FILE" || fail '现有 unit 的管理标记不匹配'
    fi
}

cleanup() {
    local status=$? i destination temporary saved_uid saved_gid saved_mode
    trap - EXIT ERR INT TERM
    if ((MUTATED == 1 && COMMITTED == 0)); then
        printf '安装未完成，正在恢复原配置和服务……\n' >&2
        systemctl stop "$SERVICE" >/dev/null 2>&1 || true
        for ((i=0; i<${#MANAGED_FILES[@]}; i++)); do
            destination=${MANAGED_FILES[$i]}
            if [[ -f "$WORK_DIR/backup-$i" ]]; then
                if temporary=$(mktemp "${destination}.rollback.XXXXXX"); then
                    PENDING_FILES+=("$temporary")
                    cp -a -- "$WORK_DIR/backup-$i" "$temporary" && mv -fT -- "$temporary" "$destination" || status=1
                else
                    status=1
                fi
            else
                rm -f -- "$destination" || status=1
            fi
        done
        i=0
        for destination in "$INSTALL_DIR" "$CONFIG_DIR"; do
            if [[ -f "$WORK_DIR/directory-$i" ]]; then
                read -r saved_uid saved_gid saved_mode < "$WORK_DIR/directory-$i"
                chown -- "$saved_uid:$saved_gid" "$destination" && chmod -- "$saved_mode" "$destination" || status=1
            fi
            i=$((i + 1))
        done
        # 清除本轮创建的单文件临时路径，再尝试移除新建的空目录。
        for temporary in "${PENDING_FILES[@]}"; do rm -f -- "$temporary" || status=1; done
        systemctl daemon-reload >/dev/null 2>&1 || true
        if ((OLD_ENABLED == 1)); then
            systemctl enable "$SERVICE" >/dev/null 2>&1 || true
        else
            systemctl disable "$SERVICE" >/dev/null 2>&1 || true
        fi
        if ((OLD_ACTIVE == 1)); then
            systemctl start "$SERVICE" >/dev/null 2>&1 || printf '警告：旧服务恢复启动失败，请检查 systemctl status。\n' >&2
        fi
        if ((CREATED_USER == 1)); then userdel "$SERVICE_USER" >/dev/null 2>&1 || true; fi
        if ((OLD_INSTALL_DIR == 0)); then rmdir -- "$INSTALL_DIR" 2>/dev/null || true; fi
        if ((OLD_CONFIG_DIR == 0)); then rmdir -- "$CONFIG_DIR" 2>/dev/null || true; fi
    fi
    if [[ -n "$WORK_DIR" ]]; then
        # 只清理由 mktemp 创建、带自身标记的临时目录。
        case "$WORK_DIR" in
            /var/tmp/cf-gtm-ping-api.??????)
                if [[ -f "$WORK_DIR/.installer-temporary" && ! -L "$WORK_DIR" ]]; then
                    rm -rf -- "$WORK_DIR"
                fi ;;
        esac
    fi
    if ((LOCK_HELD == 1)); then rmdir -- "$LOCK_DIR" 2>/dev/null || true; fi
    exit "$status"
}

# 先取得锁，再检查安装状态，避免并行升级在检查之后改变目标。
for lock_parent in /run /run/lock; do
    [[ ! -L "$lock_parent" ]] || fail '安装锁父目录不能是符号链接'
done
mkdir -p /run/lock
[[ ! -L "$LOCK_DIR" ]] || fail '安装锁路径不安全'
mkdir "$LOCK_DIR" 2>/dev/null || fail '另一个安装/卸载可能正在运行，请检查安装锁'
LOCK_HELD=1
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
check_paths
verify_ownership

if ((UNINSTALL == 1)); then
    [[ -f "$INSTALL_MARKER" && -f "$CONFIG_MARKER" ]] || fail '未找到完整安装标记，拒绝卸载'
    info "将仅删除以下固定文件及空目录："
    printf '  %s\n' "${MANAGED_FILES[@]}" "$INSTALL_DIR" "$CONFIG_DIR"
    if ((ASSUME_YES == 0)); then
        [[ -t 0 ]] || fail '非交互卸载需要显式指定 --uninstall --yes'
        read -r -p '确认卸载请输入 yes：' confirmation
        [[ "$confirmation" == 'yes' ]] || fail '已取消卸载'
    fi
    managed_uid=$(sed -n 's/^uid=\([0-9][0-9]*\)$/\1/p' "$INSTALL_MARKER")
    owned_user=$(sed -n 's/^created_user=\([01]\)$/\1/p' "$INSTALL_MARKER")
    [[ "$managed_uid" =~ ^[0-9]+$ && "$owned_user" =~ ^[01]$ ]] || fail '安装标记损坏，拒绝卸载'
    systemctl disable --now "$SERVICE"
    # 上面已逐一确认绝对路径和管理标记，不递归删除安装目录。
    rm -f -- "${MANAGED_FILES[@]}"
    systemctl daemon-reload
    systemctl reset-failed "$SERVICE" >/dev/null 2>&1 || true
    rmdir -- "$INSTALL_DIR" "$CONFIG_DIR" 2>/dev/null || true
    if [[ "$owned_user" == 1 ]] && id "$SERVICE_USER" >/dev/null 2>&1; then
        [[ $(id -u "$SERVICE_USER") == "$managed_uid" ]] || fail '服务用户 UID 已变化，保留该用户'
        userdel "$SERVICE_USER" || info '服务已卸载，但服务用户删除失败，请人工检查。'
    fi
    COMMITTED=1
    info '卸载完成；未修改防火墙、主控或 DNS。'
    exit 0
fi

# 仅使用发行版自身包管理器；不使用 pip 或第三方安装源。
if command -v apt-get >/dev/null 2>&1; then
    if ! command -v python3 >/dev/null || ! command -v ping >/dev/null || ! command -v curl >/dev/null; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get update
        apt-get install -y python3 iputils-ping curl ca-certificates
    fi
elif command -v dnf >/dev/null 2>&1; then
    if ! command -v python3 >/dev/null || ! command -v ping >/dev/null || ! command -v curl >/dev/null; then
        dnf install -y python3 iputils curl ca-certificates
    fi
elif command -v yum >/dev/null 2>&1; then
    if ! command -v python3 >/dev/null || ! command -v ping >/dev/null || ! command -v curl >/dev/null; then
        yum install -y python3 iputils curl ca-certificates
    fi
else
    fail '仅支持 Debian/Ubuntu 或 RHEL 系发行版'
fi
for dependency in python3 ping curl sha256sum useradd userdel getent install mktemp stat chown chmod; do
    command -v "$dependency" >/dev/null || fail "缺少必要系统工具：$dependency"
done
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 6) else 1)' || fail '需要 Python 3.6 或更新版本'
PYTHON=$(command -v python3)
PING=$(command -v ping)
WORK_DIR=$(mktemp -d /var/tmp/cf-gtm-ping-api.XXXXXX)
printf 'cf-gtm-ping-api\n' > "$WORK_DIR/.installer-temporary"

# 服务源码内嵌在单个脚本中；不访问 GTM 或额外的源码下载地址。
if [[ -n "$SOURCE_OVERRIDE" ]]; then
    [[ "$SOURCE_OVERRIDE" == /* && -f "$SOURCE_OVERRIDE" && ! -L "$SOURCE_OVERRIDE" ]] || fail '--source 必须是非符号链接的绝对文件路径'
    cp -- "$SOURCE_OVERRIDE" "$WORK_DIR/ping_api.py"
else
    cat > "$WORK_DIR/ping_api.py" <<'__GTM_EMBEDDED_PYTHON_20261126__'
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国内服务器运行的兼容 Ping HTTP API；仅使用 Python 3 标准库。"""
import argparse
import hmac
import http.client
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

VERSION = "1.0.0"
DEFAULT_CONFIG = "/etc/cf-gtm-ping-api/config.json"
MAX_REQUEST_BYTES = 12288
MAX_LINE_BYTES = 4096
MAX_REQUEST_LINES = 34
HEADER_TIMEOUT = 3.0
PING_TIMEOUT = 5.0
MAX_DNS_RESULTS = 32
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
SUMMARY_RE = re.compile(
    r"^(\d+) packets transmitted, (\d+) (?:packets )?received, "
    r"(?:\+(\d+) errors, )?([\d.]+)% packet loss(?:,.*)?$", re.MULTILINE)
RTT_RE = re.compile(
    r"^(?:rtt|round-trip) min/avg/max/(?:mdev|stddev) = "
    r"([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms\s*$", re.MULTILINE)
# 明确列出特殊地址，避免不同 Python 小版本的 is_global 数据库差异。
V4_DENY = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4"))
V6_GLOBAL = ipaddress.ip_network("2000::/3")
V6_DENY = tuple(ipaddress.ip_network(n) for n in (
    "2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20"))


class ConfigError(ValueError):
    """配置错误；对外仅显示固定消息。"""


class TargetError(ValueError):
    """目标或解析结果不符合公网限制。"""


class RequestLimitError(ValueError):
    """请求超过固定资源限制。"""


def is_public_ip(address):
    """仅接受可直接路由的单播公网地址；不允许映射、隧道或特殊地址。"""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if not ip.is_global or ip.is_reserved or ip.is_multicast or ip.is_loopback:
        return False
    if ip.is_unspecified or ip.is_link_local or ip.is_private:
        return False
    if ip.version == 4:
        return not any(ip in net for net in V4_DENY)
    return ip in V6_GLOBAL and not any(ip in net for net in V6_DENY)


def normalize_target(value):
    """接受域名或字面 IP；不接受 URL、端口、范围标识和数字缩写地址。"""
    if not isinstance(value, str) or not value or len(value) > 253:
        raise TargetError()
    if value != value.strip() or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise TargetError()
    if any(c in value for c in "/\\@?#%[]"):
        raise TargetError()
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        if ":" in value or re.fullmatch(r"[0-9.]+", value):
            raise TargetError()
        name = value[:-1] if value.endswith(".") else value
        labels = name.split(".")
        if len(labels) < 2 or any(not LABEL_RE.fullmatch(label) for label in labels):
            raise TargetError()
        return name.lower()
    if not is_public_ip(str(ip)):
        raise TargetError()
    return str(ip)


class BoundedResolver:
    """限制 libc DNS 等待与线程数量；挂起解析不会让后续请求无限创建线程。"""
    def __init__(self, workers=8, timeout=1.0):
        self.slots = threading.BoundedSemaphore(workers)
        self.timeout = timeout

    def __call__(self, *arguments):
        if not self.slots.acquire(blocking=False):
            raise socket.gaierror()
        finished = threading.Event()
        result = []

        def worker():
            try:
                result.append(socket.getaddrinfo(*arguments))
            except Exception:
                result.append(None)
            finally:
                self.slots.release()
                finished.set()

        try:
            thread = threading.Thread(target=worker, name="ping-api-dns", daemon=True)
            thread.start()
        except Exception:
            self.slots.release()
            raise socket.gaierror() from None
        except BaseException:
            self.slots.release()
            raise
        if not finished.wait(self.timeout) or not result or result[0] is None:
            raise socket.gaierror()
        return result[0]


DNS_RESOLVER = BoundedResolver()


def resolve_public_target(value, resolver=None):
    """校验所有 A/AAAA 结果；向 ping 只交付校验后的字面 IP。"""
    host = normalize_target(value)
    try:
        ip = ipaddress.ip_address(host)
        return str(ip)
    except ValueError:
        pass
    if resolver is None:
        resolver = DNS_RESOLVER
    try:
        rows = resolver(host, None, socket.AF_UNSPEC, socket.SOCK_DGRAM,
                        socket.IPPROTO_UDP)
        if not rows or len(rows) > MAX_DNS_RESULTS:
            raise TargetError()
        addresses = []
        for family, _kind, _proto, _canon, sockaddr in rows:
            if family not in (socket.AF_INET, socket.AF_INET6) or not sockaddr:
                raise TargetError()
            text = sockaddr[0]
            if not isinstance(text, str) or "%" in text or not is_public_ip(text):
                raise TargetError()
            ip = ipaddress.ip_address(text)
            if (family == socket.AF_INET) != (ip.version == 4):
                raise TargetError()
            addresses.append(ip)
        # 优先 IPv4，所有结果已检查；不再让外部工具自行解析原域名。
        return str(sorted(set(addresses), key=lambda x: (x.version, int(x)))[0])
    except (OSError, ValueError, TypeError, IndexError, OverflowError):
        raise TargetError() from None


def parse_ping_result(returncode, stdout, stderr):
    """只有完整且自洽的两包统计才判定成功或全丢包，其他一律未知。"""
    unknown = (-1, None)
    if returncode not in (0, 1) or not isinstance(stdout, str) or stderr.strip():
        return unknown
    summaries = list(SUMMARY_RE.finditer(stdout))
    if len(summaries) != 1:
        return unknown
    transmitted, received, errors, loss = summaries[0].groups()
    try:
        transmitted, received = int(transmitted), int(received)
        loss = float(loss)
        if transmitted != 2 or not 0 <= received <= 2 or int(errors or 0):
            return unknown
        if not math.isfinite(loss) or abs(loss - (2 - received) * 50.0) > 0.1:
            return unknown
        timings = list(RTT_RE.finditer(stdout))
        if received == 0:
            return (-2, None) if returncode == 1 and not timings else unknown
        if returncode != 0:
            return unknown
        if len(timings) != 1:
            return unknown
        minimum, average, maximum, deviation = map(float, timings[0].groups())
        if not all(math.isfinite(n) and 0 <= n <= 60000
                   for n in (minimum, average, maximum, deviation)):
            return unknown
        if not minimum <= average <= maximum:
            return unknown
        return 200, "{:.1f}ms".format(average)
    except (ValueError, OverflowError):
        return unknown


def probe(target, ping_path, runner=None, resolver=None):
    """外部进程禁用 shell、固定参数和语言环境、设置独立硬超时。"""
    if runner is None:
        runner = subprocess.run
    try:
        deadline = time.monotonic() + PING_TIMEOUT
        address = resolve_public_target(target, resolver)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return -1, None
        result = runner(
            [ping_path, "-n", "-q", "-c", "2", "-W", "1", address],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=remaining, check=False, shell=False,
            encoding="ascii", errors="replace",
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "LANG": "C"})
        return parse_ping_result(result.returncode, result.stdout, result.stderr)
    except (TargetError, OSError, subprocess.SubprocessError, ValueError, TypeError):
        return -1, None


class Config:
    def __init__(self, values):
        if not isinstance(values, dict):
            raise ConfigError()
        allowed_keys = {"token", "allowed_clients", "bind", "port", "server_name",
                        "max_workers", "ping_path", "public_host"}
        if set(values) - allowed_keys:
            raise ConfigError()
        token = values.get("token", "")
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
            raise ConfigError()
        self.token = token.encode("ascii")
        self.bind = values.get("bind", "0.0.0.0")
        try:
            if not isinstance(self.bind, str) or "%" in self.bind:
                raise ValueError()
            ipaddress.ip_address(self.bind)
        except ValueError:
            raise ConfigError() from None
        self.port = values.get("port", 8788)
        self.max_workers = values.get("max_workers", 8)
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ConfigError()
        if type(self.max_workers) is not int or not 1 <= self.max_workers <= 8:
            raise ConfigError()
        self.server_name = values.get("server_name", "国内 Ping 节点")
        if (not isinstance(self.server_name, str) or not 1 <= len(self.server_name) <= 80
                or any(ord(c) < 32 or ord(c) == 127 for c in self.server_name)):
            raise ConfigError()
        clients = values.get("allowed_clients", [])
        if not isinstance(clients, list) or len(clients) > 32:
            raise ConfigError()
        try:
            if any(not isinstance(item, str) or len(item) > 64 for item in clients):
                raise ValueError()
            self.allowed_clients = tuple(ipaddress.ip_network(item, strict=False)
                                         for item in clients)
        except ValueError:
            raise ConfigError() from None
        self.ping_path = values.get("ping_path") or shutil.which("ping")
        if not isinstance(self.ping_path, str) or not os.path.isabs(self.ping_path):
            raise ConfigError()

    def authorized(self, supplied_token, source):
        # 无论白名单是否存在，都先以恒定时间比较 token；白名单不信任代理头。
        matches = hmac.compare_digest(supplied_token.encode("utf-8"), self.token)
        try:
            address = ipaddress.ip_address(source)
            if address.version == 6 and address.ipv4_mapped:
                address = address.ipv4_mapped
            permitted = not self.allowed_clients or any(
                address.version == network.version and address in network
                for network in self.allowed_clients)
        except ValueError:
            permitted = False
        return matches and permitted


def load_config(path):
    descriptor = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
            raise ConfigError()
        if os.name == "posix":
            # root 安装器需要验证由专用服务用户持有的旧配置。
            if stat.S_IMODE(info.st_mode) != 0o600:
                raise ConfigError()
            if os.geteuid() != 0 and info.st_uid not in (0, os.geteuid()):
                raise ConfigError()
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = None
            return Config(json.load(handle))
    except (OSError, ValueError, UnicodeError):
        raise ConfigError("配置不可读或不符合安全要求") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


class BoundedReader:
    """请求头使用绝对截止时间、字节/行硬限制，防止慢连接长期占用线程。"""
    def __init__(self, connection):
        self.connection = connection
        self.deadline = time.monotonic() + HEADER_TIMEOUT
        self.buffer = bytearray()
        self.total = 0
        self.lines = 0

    def readline(self, limit=-1):
        maximum = min(MAX_LINE_BYTES, limit) if limit >= 0 else MAX_LINE_BYTES
        self.lines += 1
        if self.lines > MAX_REQUEST_LINES:
            raise RequestLimitError()
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                size = newline + 1
                if size > maximum:
                    raise RequestLimitError()
                result = bytes(self.buffer[:size])
                del self.buffer[:size]
                return result
            if len(self.buffer) >= maximum:
                raise RequestLimitError()
            remaining_time = self.deadline - time.monotonic()
            if remaining_time <= 0:
                raise socket.timeout()
            self.connection.settimeout(remaining_time)
            chunk = self.connection.recv(min(4096, MAX_REQUEST_BYTES - self.total + 1))
            if not chunk:
                if not self.buffer:
                    return b""
                result = bytes(self.buffer)
                self.buffer.clear()
                return result
            self.total += len(chunk)
            if self.total > MAX_REQUEST_BYTES:
                raise RequestLimitError()
            self.buffer.extend(chunk)

    def close(self):
        self.buffer.clear()


class PingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "PingAPI"
    sys_version = ""

    def setup(self):
        super().setup()
        self.rfile.close()
        self.rfile = BoundedReader(self.connection)
        self.request_version = "HTTP/1.0"
        self.requestline = ""
        self.command = None

    def handle(self):
        try:
            super().handle()
        except RequestLimitError:
            self.send_error(431)
        except (OSError, ValueError):
            # 不把解析细节、完整请求行或 token 写入日志/响应。
            try:
                self.send_error(400)
            except OSError:
                pass

    def version_string(self):
        return "PingAPI"

    def log_message(self, _format, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        # 标准库解析畸形 HTTP 版本时可能回退到 0.9；错误响应仍保留 JSON 头。
        if self.request_version == "HTTP/0.9":
            self.request_version = "HTTP/1.0"
        self.respond(code, {"code": -1, "msg": "请求不可用", "data": {}})

    def respond(self, status, payload):
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)
        self.close_connection = True

    def do_GET(self):
        if self.headers.get("Transfer-Encoding") is not None:
            self.send_error(400)
            return
        lengths = self.headers.get_all("Content-Length", [])
        if lengths and (len(lengths) != 1 or lengths[0] != "0"):
            self.send_error(400)
            return
        try:
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.fragment:
                raise ValueError()
            if parsed.path == "/health" and not parsed.query:
                self.respond(200, {"status": "ok"})
                return
            if parsed.path != "/ping":
                self.send_error(404)
                return
            query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
            if set(query) - {"host", "url", "token"}:
                raise ValueError()
            if any(len(values) != 1 for values in query.values()):
                raise ValueError()
            token = query.get("token", [""])[0]
            if not self.server.config.authorized(token, self.client_address[0]):
                self.respond(403, {"code": -1, "msg": "访问被拒绝", "data": {}})
                return
            if ("host" in query) == ("url" in query):
                raise ValueError()
            target = query.get("host", query.get("url"))[0]
            normalize_target(target)
        except (ValueError, TargetError, UnicodeError):
            self.send_error(400)
            return
        code, delay = probe(target, self.server.config.ping_path)
        messages = {200: "成功", -2: "完整探测无回包", -1: "探测结果未知"}
        data = {"server": self.server.config.server_name}
        if delay is not None:
            data["time"] = delay
        self.respond(200, {"code": code, "msg": messages[code], "data": data})


class BoundedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16
    allow_reuse_address = True

    def __init__(self, config, handler=PingHandler):
        self.config = config
        self.slots = threading.BoundedSemaphore(config.max_workers)
        if ":" in config.bind:
            self.address_family = socket.AF_INET6
        super().__init__((config.bind, config.port), handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            # 满载时不创建额外线程，也不读取带 token 的请求。
            body = b'{"code":-1,"msg":"busy","data":{}}'
            response = (b"HTTP/1.0 503 Service Unavailable\r\n"
                        b"Content-Type: application/json\r\nConnection: close\r\n"
                        b"Cache-Control: no-store\r\nContent-Length: "
                        + str(len(body)).encode("ascii") + b"\r\n\r\n" + body)
            try:
                request.settimeout(0.1)
                request.sendall(response)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # 默认实现会把异常堆栈打印到日志，这里仅输出不含请求数据的固定消息。
        print("请求处理失败", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description="国内 Ping HTTP API（Python 标准库）")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径，权限必须为 0600")
    parser.add_argument("--check-config", action="store_true", help="仅验证配置，不监听端口")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.check_config:
            print("配置检查通过")
            return 0
        with BoundedHTTPServer(config) as server:
            print("Ping API 已启动", flush=True)
            try:
                server.serve_forever(poll_interval=0.25)
            except KeyboardInterrupt:
                print("Ping API 已停止", flush=True)
        return 0
    except (ConfigError, OSError):
        print("启动失败：请检查配置、端口和系统依赖", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
__GTM_EMBEDDED_PYTHON_20261126__
fi
[[ "$SOURCE_SHA256" =~ ^[a-f0-9]{64}$ ]] || fail '安装器源码摘要无效，请使用完整发行版'
printf '%s  %s\n' "$SOURCE_SHA256" "$WORK_DIR/ping_api.py" | sha256sum --check --status || fail '源码 SHA256 不匹配，已停止安装'
python3 -B "$WORK_DIR/ping_api.py" --version >/dev/null

# 输入通过受限临时文件/环境交给 Python，禁止把配置当成 shell 代码执行。
[[ -d "$INSTALL_DIR" ]] && OLD_INSTALL_DIR=1
[[ -d "$CONFIG_DIR" ]] && OLD_CONFIG_DIR=1
i=0
for directory in "$INSTALL_DIR" "$CONFIG_DIR"; do
    [[ ! -d "$directory" ]] || stat -c '%u %g %a' -- "$directory" > "$WORK_DIR/directory-$i"
    i=$((i + 1))
done
for ((i=0; i<${#MANAGED_FILES[@]}; i++)); do
    [[ ! -f "${MANAGED_FILES[$i]}" ]] || cp -a -- "${MANAGED_FILES[$i]}" "$WORK_DIR/backup-$i"
done
systemctl is-active --quiet "$SERVICE" && OLD_ACTIVE=1 || true
systemctl is-enabled --quiet "$SERVICE" && OLD_ENABLED=1 || true

if [[ -t 0 && -t 1 ]]; then
    if [[ ! ${GTM_PUBLIC_HOST+x} ]]; then
        read -r -p '服务器公网域名或 IP（升级直接回车保留；新装回车使用主机名）：' interactive_host
        if [[ -n "$interactive_host" ]]; then export GTM_PUBLIC_HOST=$interactive_host; fi
    fi
    if [[ ! ${GTM_ALLOWED_CLIENTS+x} ]]; then
        read -r -p '允许来源 IP/CIDR，逗号分隔（回车保留原值，新装不限制来源，但仍须 token）：' interactive_clients
        if [[ -n "$interactive_clients" ]]; then export GTM_ALLOWED_CLIENTS=$interactive_clients; fi
    fi
fi
export GTM_INSTALL_CONFIG="$CONFIG_FILE" GTM_STAGE_CONFIG="$WORK_DIR/config.json" GTM_PING_PATH="$PING"
python3 - "$WORK_DIR/ping_api.py" <<'PY'
# 使用将要安装的服务校验配置；所有敏感值只存在环境与 0600 文件，不写 argv。
import importlib.util
import ipaddress
import json
import os
import re
import secrets
import socket
import sys
spec = importlib.util.spec_from_file_location('gtm_ping_installer_api', sys.argv[1])
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)
try:
    old_path = os.environ['GTM_INSTALL_CONFIG']
    if os.path.exists(old_path):
        api.load_config(old_path)
        with open(old_path, encoding='utf-8') as handle:
            values = json.load(handle)
    else:
        values = {'token': secrets.token_urlsafe(32), 'port': 8788,
                  'bind': '0.0.0.0', 'allowed_clients': [],
                  'server_name': '国内 Ping 节点', 'max_workers': 8}
    if 'GTM_API_TOKEN' in os.environ:
        values['token'] = os.environ['GTM_API_TOKEN']
    if 'GTM_API_PORT' in os.environ:
        port = os.environ['GTM_API_PORT']
        if not re.fullmatch(r'[0-9]{1,5}', port):
            raise ValueError()
        values['port'] = int(port)
    if 'GTM_ALLOWED_CLIENTS' in os.environ:
        raw = os.environ['GTM_ALLOWED_CLIENTS']
        values['allowed_clients'] = [part.strip() for part in raw.split(',')] if raw else []
    public_host = os.environ.get('GTM_PUBLIC_HOST', values.get('public_host', socket.gethostname()))
    if not isinstance(public_host, str) or not public_host or len(public_host) > 253:
        raise ValueError()
    try:
        ipaddress.ip_address(public_host)
    except ValueError:
        if not all(api.LABEL_RE.fullmatch(label) for label in public_host.split('.')):
            raise ValueError()
    if '%' in public_host:
        raise ValueError()
    values['public_host'] = public_host
    values['ping_path'] = os.environ['GTM_PING_PATH']
    api.Config(values)
    with open(os.environ['GTM_STAGE_CONFIG'], 'x', encoding='utf-8') as handle:
        json.dump(values, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    os.chmod(os.environ['GTM_STAGE_CONFIG'], 0o600)
except (OSError, ValueError, TypeError):
    print('配置生成失败：请检查 token、CIDR、端口、公网主机名以及旧配置权限。', file=sys.stderr)
    sys.exit(1)
PY
unset GTM_API_TOKEN GTM_INSTALL_CONFIG GTM_STAGE_CONFIG GTM_PING_PATH
python3 -B "$WORK_DIR/ping_api.py" --check-config --config "$WORK_DIR/config.json" >/dev/null

# 从这里开始的固定文件或账号变更均由 EXIT trap 回滚。
MUTATED=1
if id "$SERVICE_USER" >/dev/null 2>&1; then
    [[ -f "$INSTALL_MARKER" ]] || fail '同名服务用户已存在但不属于本安装器，拒绝复用'
    managed_uid=$(sed -n 's/^uid=\([0-9][0-9]*\)$/\1/p' "$INSTALL_MARKER")
    [[ $(id -u "$SERVICE_USER") == "$managed_uid" && "$managed_uid" != 0 ]] || fail '服务用户 UID 与标记不一致'
    original_created=$(sed -n 's/^created_user=\([01]\)$/\1/p' "$INSTALL_MARKER")
    [[ "$original_created" =~ ^[01]$ ]] || fail '服务用户标记无效'
else
    nologin=$(command -v nologin || true)
    [[ -n "$nologin" ]] || fail '缺少 nologin 系统工具'
    useradd --system --user-group --no-create-home --home-dir /nonexistent --shell "$nologin" "$SERVICE_USER"
    CREATED_USER=1
    original_created=1
fi
service_uid=$(id -u "$SERVICE_USER")
service_gid=$(id -g "$SERVICE_USER")
[[ "$service_uid" != 0 ]] || fail '不允许使用 root 运行服务'

# 只打开 ICMP datagram socket 验证权限，不向任何地址发包，也不改全局 sysctl。
capability_mode='raw'
if "$PYTHON" - "$service_uid" "$service_gid" <<'PY'
import errno
import os
import socket
import sys
os.setgroups([])
os.setgid(int(sys.argv[2]))
os.setuid(int(sys.argv[1]))
for family, proto in ((socket.AF_INET, socket.IPPROTO_ICMP),
                      (socket.AF_INET6, socket.IPPROTO_ICMPV6)):
    try:
        sock = socket.socket(family, socket.SOCK_DGRAM, proto)
        sock.close()
    except OSError as exc:
        if family == socket.AF_INET6 and exc.errno in (errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT):
            continue
        sys.exit(1)
PY
then capability_mode='datagram'; fi

cat > "$WORK_DIR/service" <<EOF
# Managed by cf-gtm-ping-api installer
[Unit]
Description=国内 Ping HTTP API
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
User=$SERVICE_USER
Group=$service_gid
ExecStart=$PYTHON -B -u $SOURCE_FILE --config $CONFIG_FILE
Restart=on-failure
RestartSec=3
TimeoutStopSec=6
KillMode=control-group
UMask=0077
Environment=PYTHONDONTWRITEBYTECODE=1
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true
SystemCallArchitectures=native
LimitNOFILE=128
LimitCORE=0
TasksMax=96
MemoryMax=128M
EOF
if [[ "$capability_mode" == datagram ]]; then
    # 保留 bounding set 的单一能力以兼容系统 ping 自带的文件 capability；不授予 ambient 能力。
    printf 'CapabilityBoundingSet=CAP_NET_RAW\nAmbientCapabilities=\n' >> "$WORK_DIR/service"
else
    printf 'CapabilityBoundingSet=CAP_NET_RAW\nAmbientCapabilities=CAP_NET_RAW\n' >> "$WORK_DIR/service"
fi
printf '\n[Install]\nWantedBy=multi-user.target\n' >> "$WORK_DIR/service"
printf 'cf-gtm-ping-api:v1\nuid=%s\ncreated_user=%s\n' "$service_uid" "$original_created" > "$WORK_DIR/marker"

install -d -o root -g "$service_gid" -m 0750 "$INSTALL_DIR" "$CONFIG_DIR"
atomic_install() {
    local source=$1 destination=$2 owner=$3 group=$4 mode=$5
    local temporary="${destination}.install.$$"
    [[ ! -e "$temporary" && ! -L "$temporary" ]] || fail '原子写入的临时路径已被占用'
    PENDING_FILES+=("$temporary")
    install -o "$owner" -g "$group" -m "$mode" -- "$source" "$temporary"
    mv -fT -- "$temporary" "$destination"
}
if ((OLD_ACTIVE == 1)); then
    systemctl stop "$SERVICE" >/dev/null 2>&1 || fail '无法停止现有服务，将恢复原安装'
fi
atomic_install "$WORK_DIR/ping_api.py" "$SOURCE_FILE" root "$service_gid" 0640
atomic_install "$WORK_DIR/config.json" "$CONFIG_FILE" "$service_uid" "$service_gid" 0600
atomic_install "$WORK_DIR/service" "$UNIT_FILE" root root 0644
atomic_install "$WORK_DIR/marker" "$INSTALL_MARKER" root root 0600
atomic_install "$WORK_DIR/marker" "$CONFIG_MARKER" root root 0600
systemctl daemon-reload
systemctl enable --now "$SERVICE"

# 仅检查本机最小健康端点；不执行真实公网 Ping，不输出配置或 token。
health_url=$("$PYTHON" - "$CONFIG_FILE" <<'PY'
import json
import sys
with open(sys.argv[1], encoding='utf-8') as handle:
    config = json.load(handle)
bind = config.get('bind', '0.0.0.0')
host = '127.0.0.1' if bind == '0.0.0.0' else ('::1' if bind == '::' else bind)
if ':' in host:
    host = '[' + host + ']'
print('http://{}:{}/health'.format(host, config['port']))
PY
)
healthy=0
for attempt in 1 2 3 4 5; do
    if curl --noproxy '*' --fail --silent --show-error --connect-timeout 1 --max-time 2 "$health_url" \
        2>/dev/null | "$PYTHON" -c 'import json,sys; sys.exit(0 if json.load(sys.stdin) == {"status":"ok"} else 1)' 2>/dev/null; then
        healthy=1
        break
    fi
    sleep 1
done
((healthy == 1)) || fail '本机健康检查失败，将恢复原安装'
systemctl is-active --quiet "$SERVICE" || fail '服务未保持运行，将恢复原安装'
COMMITTED=1
info "安装完成，ICMP 权限模式：$capability_mode。服务名称：$SERVICE"
"$PYTHON" - "$CONFIG_FILE" <<'PY'
import json
import sys
with open(sys.argv[1], encoding='utf-8') as handle:
    config = json.load(handle)
host = config['public_host']
if ':' in host:
    host = '[' + host + ']'
print('可直接回填到旧控制台的 URL（请保密）：')
print('http://{}:{}/ping?host={{target}}&token={}'.format(host, config['port'], config['token']))
print('兼容旧参数名：把 host={target} 换成 url={target} 即可。')
print('请自行确认公网域名/IP、防火墙和安全组；安装器不会修改它们。')
print('当前为 HTTP；跨公网传递 token 时应使用受信任 HTTPS 反向代理。')
PY
