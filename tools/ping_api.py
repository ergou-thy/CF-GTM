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
