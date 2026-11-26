#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CF-GTM —— 基于 Cloudflare DNS 的轻量级全局流量管理（GTM 平替）
桃花酱出品 · @taohuayuang

failover：单条记录主备容灾；round_robin：多 A 记录；weighted：时间片权重轮换。
运行：pip install -r requirements.txt && python app.py
普通 import 不加载配置、不发起网络请求、不启动后台。WSGI 部署请显式调用 init_app。
"""

import os
import json
import time
import socket
import secrets
import threading
import copy
import hashlib
import hmac
import math
import ipaddress
import re
import stat
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import wraps
from string import Formatter
from urllib.parse import quote, urlsplit

import requests
from flask import Flask, request, jsonify, session, redirect, render_template, send_file
from flask.sessions import SecureCookieSessionInterface
from werkzeug.middleware.proxy_fix import ProxyFix

# ---------------------------------------------------------------- 基础路径

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
CF_API = "https://api.cloudflare.com/client/v4"
VERSION = "20261126-gtm4"

DEFAULT_CONFIG = {
    "cloudflare": {"api_token": "", "zone_id": "", "record_name": "main.example.com", "record_type": "CNAME"},
    "probe": {
        "interval_seconds": 60, "timeout_seconds": 10, "fail_threshold": 2,
        "decision_mode": "smart", "api_url": "https://v2.xxapi.cn/api/ping?url={target}", "record_ttl": 60,
    },
    "scheduler": {"slice_seconds": 300},
    "auto_window": {"enabled": False, "start": "00:00", "end": "23:59"},
    "cooldown_minutes": 0,
    "nodes": [
        {"name": "日本A", "target": "jp1.example.com", "port": 443, "weight": 1},
        {"name": "新加坡A", "target": "sg1.example.com", "port": 443, "weight": 1},
    ],
    "mode": "failover", "auto_switch": True, "dry_run": False,
    "web": {"host": "0.0.0.0", "port": 8787, "password": ""},
}

app = Flask(__name__)
app.config.update(MAX_CONTENT_LENGTH=131072, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
lock = threading.RLock()
operation_lock = threading.RLock()
persistence_lock = threading.RLock()
probe_lock = threading.Lock()
stop_event = threading.Event()
config = {}
_config_revision = 0
_initialized = False
_background_thread = None


def new_state():
    return {
        "current": "", "current_target": "", "record_id": "", "records": {}, "nodes": {},
        "logs": [], "started_at": "", "last_round": "", "cf_error": "", "cf_loaded": False,
        "dns_records": [], "owned_records": {}, "dns_scope": "", "unmanaged_records": [],
        "weighted": {"acc": {}, "last_slice": 0}, "last_switch_ts": 0,
        "probing": False, "last_probe_error": "", "simulation": {}, "cf_observed_at": "", "cf_stale": True,
    }


state = new_state()


class LoopbackProxy:
    """仅信任本机反代的一层协议头；绝不改写鉴权所见的来源 IP/主机。"""
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.proxied = ProxyFix(wrapped, x_for=0, x_proto=1, x_host=0, x_port=0, x_prefix=0)

    def __call__(self, environ, start_response):
        try:
            trusted = ipaddress.ip_address(environ.get("REMOTE_ADDR", "")).is_loopback
        except ValueError:
            trusted = False
        return (self.proxied if trusted else self.wrapped)(environ, start_response)


class SchemeSessionInterface(SecureCookieSessionInterface):
    def get_cookie_secure(self, application):
        return bool(application.config.get("SESSION_COOKIE_SECURE")) or request.is_secure


app.wsgi_app = LoopbackProxy(app.wsgi_app)
app.session_interface = SchemeSessionInterface()

# ---------------------------------------------------------------- 工具函数 / 原子配置


class ValidationError(ValueError):
    pass


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_error(error) -> str:
    message = str(error)
    with lock:
        sensitive = [config.get("cloudflare", {}).get("api_token"),
                     config.get("web", {}).get("password"), config.get("web", {}).get("secret")]
    for value in sensitive:
        if isinstance(value, str) and value:
            message = message.replace(value, "[已隐藏]")
    message = re.sub(r"Bearer\s+\S+", "Bearer [已隐藏]", message, flags=re.I)
    return message[:400]


def log(msg: str, level: str = "info") -> None:
    msg = safe_error(msg)
    entry = {"ts": now_str(), "level": level, "msg": msg}
    with lock:
        state["logs"].insert(0, entry)
        del state["logs"][200:]
    print(f"[{entry['ts']}] [{level.upper()}] {msg}", flush=True)
    try:
        with open(os.path.join(BASE_DIR, "gtm.log"), "a", encoding="utf-8") as stream:
            stream.write(f"[{entry['ts']}] [{level.upper()}] {msg}\n")
    except OSError:
        pass


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def text_value(value, name, maximum=4096, allow_empty=False):
    if not isinstance(value, str):
        raise ValidationError(f"{name} 必须是文本")
    value = value.strip()
    if (not value and not allow_empty) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValidationError(f"{name} 为空、过长或含控制字符")
    return value


def int_value(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValidationError(f"{name} 必须是整数")
    if isinstance(value, str) and not re.fullmatch(r"[0-9]+", value.strip()):
        raise ValidationError(f"{name} 必须是整数")
    try:
        number = int(value)
    except (ValueError, OverflowError):
        raise ValidationError(f"{name} 必须是整数") from None
    if not minimum <= number <= maximum:
        raise ValidationError(f"{name} 必须在 {minimum}..{maximum} 之间")
    return number


def bool_value(value, name):
    if not isinstance(value, bool):
        raise ValidationError(f"{name} 必须是布尔值")
    return value


def hostname(value, name="target", allow_ip=True):
    value = text_value(value, name, 253).rstrip(".")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        address = None
    if address is not None:
        if not allow_ip or address.version != 4:
            raise ValidationError(f"{name} 必须是域名或 IPv4 地址" if allow_ip else f"{name} 必须是域名")
        return str(address)
    try:
        value = value.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValidationError(f"{name} 域名非法") from None
    if len(value) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                   for label in value.split(".")):
        raise ValidationError(f"{name} 域名非法")
    if re.fullmatch(r"[0-9.]+", value):
        raise ValidationError(f"{name} IPv4 地址非法")
    return value


def validate_api_url(value):
    value = text_value(value, "api_url", 2048)
    try:
        fields = list(Formatter().parse(value))
    except ValueError:
        raise ValidationError("探测接口模板的大括号不完整") from None
    if not any(field == "target" for _, field, _, _ in fields):
        raise ValidationError("探测接口必须包含 {target} 占位符")
    if any(field is not None and (field != "target" or spec or conversion)
           for _, field, spec, conversion in fields):
        raise ValidationError("探测接口仅支持 {target} 占位符")
    try:
        parsed = urlsplit(value.format(target="example.com"))
        valid = parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password
        valid = valid and not parsed.fragment and (parsed.port is None or 1 <= parsed.port <= 65535)
    except ValueError:
        valid = False
    if not valid:
        raise ValidationError("探测接口必须是有效的 http/https 地址")
    return value


def validate_config(candidate):
    """只修改候选副本，完整通过后才允许持久化与发布。"""
    if not isinstance(candidate, dict):
        raise ValidationError("配置必须是对象")
    candidate = copy.deepcopy(candidate)
    for section in ("cloudflare", "probe", "scheduler", "auto_window", "web"):
        if not isinstance(candidate.get(section), dict):
            raise ValidationError(f"{section} 必须是对象")
    cf, probe, web = candidate["cloudflare"], candidate["probe"], candidate["web"]
    cf["api_token"] = text_value(cf.get("api_token", ""), "api_token", allow_empty=True)
    cf["zone_id"] = text_value(cf.get("zone_id", ""), "zone_id", 32, allow_empty=True)
    if cf["zone_id"] and not re.fullmatch(r"[a-fA-F0-9]{32}", cf["zone_id"]):
        raise ValidationError("zone_id 必须是 32 位十六进制标识")
    cf["record_name"] = hostname(cf.get("record_name"), "record_name", allow_ip=False)
    if cf.get("record_type") not in ("A", "CNAME"):
        raise ValidationError("record_type 仅支持 A / CNAME")
    if candidate.get("mode") not in ("failover", "round_robin", "weighted"):
        raise ValidationError("mode 非法")
    if candidate["mode"] == "round_robin" and cf["record_type"] != "A":
        raise ValidationError("round_robin 需要主域名使用 A 记录")
    for key, low, high in (("interval_seconds", 10, 86400), ("timeout_seconds", 3, 60), ("fail_threshold", 1, 10)):
        probe[key] = int_value(probe.get(key), key, low, high)
    probe["record_ttl"] = int_value(probe.get("record_ttl"), "record_ttl", 1, 86400)
    if probe["record_ttl"] != 1 and probe["record_ttl"] < 60:
        raise ValidationError("record_ttl 必须为 1（自动）或 60..86400")
    if probe.get("decision_mode") not in ("smart", "api_only", "tcp_only"):
        raise ValidationError("decision_mode 非法")
    probe["api_url"] = validate_api_url(probe.get("api_url"))
    candidate["scheduler"]["slice_seconds"] = int_value(candidate["scheduler"].get("slice_seconds"), "slice_seconds", 30, 86400)
    for key in ("auto_switch", "dry_run"):
        candidate[key] = bool_value(candidate.get(key), key)
    window = candidate["auto_window"]
    window["enabled"] = bool_value(window.get("enabled"), "auto_window.enabled")
    for key in ("start", "end"):
        if not isinstance(window.get(key), str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", window[key]):
            raise ValidationError(f"auto_window.{key} 必须为 HH:MM")
    candidate["cooldown_minutes"] = int_value(candidate.get("cooldown_minutes", 0), "cooldown_minutes", 0, 1440)
    nodes = candidate.get("nodes")
    if not isinstance(nodes, list) or len(nodes) > 200:
        raise ValidationError("nodes 必须是列表且最多 200 个节点")
    seen = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ValidationError("节点必须是对象")
        node["target"] = hostname(node.get("target"))
        if node["target"] in seen:
            raise ValidationError("节点 target 重复")
        seen.add(node["target"])
        node["name"] = text_value(node.get("name", node["target"]), "name", 80)
        node["port"] = int_value(node.get("port", 443), "port", 1, 65535)
        node["weight"] = int_value(node.get("weight", 1), "weight", 1, 1000)
        node["enabled"] = bool_value(node.get("enabled", True), "enabled")
    web["password"] = text_value(web.get("password", ""), "password", 1024, allow_empty=True)
    web["secret"] = text_value(web.get("secret", ""), "secret", 4096, allow_empty=True)
    web["host"] = text_value(web.get("host", "0.0.0.0"), "web.host", 253)
    web["port"] = int_value(web.get("port", 8787), "web.port", 1, 65535)
    if "cookie_secure" in web:
        web["cookie_secure"] = bool_value(web["cookie_secure"], "web.cookie_secure")
    return candidate


def atomic_json_write(path, payload):
    directory = os.path.dirname(os.path.abspath(path))
    descriptor, temporary = tempfile.mkstemp(prefix=".gtm-", suffix=".tmp", dir=directory)
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else 0o600
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        # 目录 fsync 在 Windows 等系统不可用；replace 完成后不能再宣称提交失败。
        try:
            directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_config(candidate=None) -> None:
    with persistence_lock:
        if candidate is None:
            with lock:
                candidate = copy.deepcopy(config)
        atomic_json_write(CONFIG_PATH, candidate)


def save_state() -> bool:
    try:
        with persistence_lock:
            with lock:
                payload = copy.deepcopy(state)
            atomic_json_write(STATE_PATH, payload)
        return True
    except (OSError, ValueError, TypeError):
        log("状态文件保存失败，已保留旧文件", "error")
        return False


def dns_scope(candidate=None):
    cf = (candidate or config)["cloudflare"]
    return "|".join((cf.get("zone_id", ""), cf.get("record_name", ""), cf.get("record_type", "")))


def commit_config(candidate):
    global config, _config_revision
    candidate = validate_config(candidate)
    with operation_lock:
        with lock:
            previous = config
            old_scope = dns_scope(previous) if previous else ""
        save_config(candidate)
        with lock:
            config = candidate
            _config_revision += 1
            scope_changed = old_scope != dns_scope(candidate)
            context_changed = scope_changed or any(previous.get(k) != candidate.get(k) for k in ("mode", "dry_run"))
            if context_changed:
                state.update(current="", current_target="", record_id="", records={}, dns_records=[],
                             cf_loaded=False, cf_error="", unmanaged_records=[], simulation={}, cf_observed_at="", cf_stale=True)
                state["last_switch_ts"] = 0
            if scope_changed:
                state["owned_records"] = {}
            state["dns_scope"] = dns_scope(candidate)
            state["weighted"] = {"acc": {}, "last_slice": 0}
            valid_targets = {n["target"] for n in candidate["nodes"]}
            state["nodes"] = {t: value for t, value in state["nodes"].items() if t in valid_targets}
            if any(previous.get("probe", {}).get(key) != candidate["probe"][key]
                   for key in ("decision_mode", "api_url", "timeout_seconds", "fail_threshold")):
                state["nodes"] = {}
            previous_nodes = {node["target"]: node for node in previous.get("nodes", [])}
            for node in candidate["nodes"]:
                old = previous_nodes.get(node["target"])
                if old is None or old.get("port", 443) != node["port"]:
                    state["nodes"].pop(node["target"], None)
        app.secret_key = candidate["web"]["secret"]
        app.config["SESSION_COOKIE_SECURE"] = candidate["web"].get("cookie_secure", False)
    return candidate


def load_config() -> None:
    global config, _config_revision
    exists = os.path.exists(CONFIG_PATH)
    if exists:
        with open(CONFIG_PATH, "r", encoding="utf-8") as stream:
            loaded = json.load(stream)
        if not isinstance(loaded, dict):
            raise ValidationError("配置文件必须是对象")
        candidate = deep_merge(DEFAULT_CONFIG, loaded)
    else:
        candidate = copy.deepcopy(DEFAULT_CONFIG)
    needs_save = not exists
    if not isinstance(candidate.get("web"), dict):
        raise ValidationError("web 必须是对象")
    for key in ("password", "secret"):
        if not candidate.get("web", {}).get(key):
            candidate.setdefault("web", {})[key] = secrets.token_urlsafe(24)
            needs_save = True
    candidate = validate_config(candidate)
    if needs_save:
        save_config(candidate)
    with lock:
        config = candidate
        _config_revision += 1
        state.update(current="", current_target="", record_id="", records={}, dns_records=[], nodes={},
                     cf_loaded=False, cf_error="", cf_stale=True, cf_observed_at="", owned_records={},
                     unmanaged_records=[], simulation={}, weighted={"acc": {}, "last_slice": 0},
                     last_switch_ts=0, dns_scope=dns_scope(candidate))
    # 旧 DNS 内容不能从配置或磁盘状态臆造；只恢复带作用域的显式所有权。
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as stream:
                saved = json.load(stream)
            if isinstance(saved, dict):
                with lock:
                    entries = saved.get("logs", [])
                    if isinstance(entries, list):
                        state["logs"] = [{"ts": str(item.get("ts", ""))[:32],
                                          "level": item.get("level") if item.get("level") in ("info", "warn", "error") else "info",
                                          "msg": safe_error(item.get("msg", ""))}
                                         for item in entries[:200] if isinstance(item, dict)]
                    previous_nodes = saved.get("nodes", {})
                    if isinstance(previous_nodes, dict):
                        for node in candidate["nodes"]:
                            previous = previous_nodes.get(node["target"])
                            if isinstance(previous, dict):
                                state["nodes"][node["target"]] = {
                                    "healthy": None, "fail": 0, "succ": 0, "api": {}, "tcp": {},
                                    "last_check": "", "pending_refresh": True,
                                    "previous_check": str(previous.get("last_check", ""))[:32],
                                    "previous_healthy": previous.get("healthy") if isinstance(previous.get("healthy"), bool) else None,
                                }
                                # 历史地址仅用于关联已从 CF 读取的记录、等待失败阈值，不能用于新建。
                                try:
                                    previous_ip = str(ipaddress.IPv4Address(previous.get("last_ip") or previous.get("ip")))
                                except (ValueError, TypeError):
                                    previous_ip = None
                                state["nodes"][node["target"]]["last_ip"] = previous_ip
                    last_switch = saved.get("last_switch_ts", 0)
                    if (saved.get("dns_scope", dns_scope(candidate)) == dns_scope(candidate)
                            and not isinstance(last_switch, bool) and isinstance(last_switch, (int, float))
                            and math.isfinite(last_switch) and 0 <= last_switch <= time.time()):
                        state["last_switch_ts"] = last_switch
                if saved.get("dns_scope") == dns_scope(candidate):
                    owned = saved.get("owned_records", {})
                    if isinstance(owned, dict):
                        with lock:
                            state["owned_records"] = {rid: {"ip": item["ip"]} for rid, item in owned.items()
                                                      if isinstance(rid, str) and isinstance(item, dict)
                                                      and isinstance(item.get("ip"), str)}
        except (OSError, ValueError, TypeError):
            log("忽略损坏的状态文件；当前解析将重新向 Cloudflare 查询", "warn")


def masked_token() -> str:
    tok = config["cloudflare"].get("api_token", "")
    return (tok[:6] + "..." + tok[-4:]) if len(tok) > 12 else ("***" if tok else "")

# ---------------------------------------------------------------- Cloudflare


def cf_headers() -> dict:
    return {"Authorization": f"Bearer {config['cloudflare'].get('api_token', '')}", "Content-Type": "application/json"}


def cf_request(method, suffix="", **kwargs):
    cf = config["cloudflare"]
    if not cf.get("api_token") or not cf.get("zone_id"):
        raise RuntimeError("未配置 api_token / zone_id")
    try:
        response = requests.request(method, f"{CF_API}/zones/{cf['zone_id']}/dns_records{suffix}",
                                    headers=cf_headers(), timeout=15, **kwargs)
        response.raise_for_status()
        body = response.json()
    except requests.RequestException as error:
        raise RuntimeError(f"Cloudflare 请求失败（{type(error).__name__}）") from None
    except (ValueError, TypeError):
        raise RuntimeError("Cloudflare 返回了无效 JSON") from None
    if not isinstance(body, dict) or body.get("success") is not True:
        errors = body.get("errors", []) if isinstance(body, dict) else []
        details = "; ".join(str(item.get("message", "请求失败")) for item in errors[:3] if isinstance(item, dict)) if isinstance(errors, list) else ""
        raise RuntimeError(safe_error("Cloudflare 操作失败" + (f"：{details}" if details else "")))
    return body


def cf_list_records(rtype: str = None):
    params = {"name": config["cloudflare"]["record_name"], "per_page": 100, "page": 1}
    if rtype:
        params["type"] = rtype
    output = []
    seen_ids = set()
    while True:
        body = cf_request("GET", params=dict(params))
        rows = body.get("result")
        if not isinstance(rows, list):
            raise RuntimeError("Cloudflare 记录列表格式非法")
        for record in rows:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
                raise RuntimeError("Cloudflare 记录缺少有效 id")
            if not isinstance(record.get("content"), str) or not isinstance(record.get("type"), str):
                raise RuntimeError("Cloudflare 记录格式非法")
            # 即使远端筛选异常，也不把其他名称/类型的记录交给写操作。
            if str(record.get("name", "")).lower().rstrip(".") != params["name"]:
                raise RuntimeError("Cloudflare 返回了其他名称的记录，已拒绝操作")
            if rtype and record["type"] != rtype:
                raise RuntimeError("Cloudflare 返回了其他类型的记录，已拒绝操作")
            if record["id"] in seen_ids:
                raise RuntimeError("Cloudflare 分页包含重复记录，已拒绝不完整结果")
            seen_ids.add(record["id"])
            output.append(record)
        info = body.get("result_info") or {}
        if not isinstance(info, dict):
            raise RuntimeError("Cloudflare 分页信息非法")
        if "total_pages" not in info and len(rows) >= params["per_page"]:
            raise RuntimeError("Cloudflare 未提供完整分页信息，已拒绝不完整结果")
        total_pages = info.get("total_pages", 1)
        if (isinstance(total_pages, bool) or not isinstance(total_pages, int) or not 0 <= total_pages <= 1000
                or (total_pages == 0 and (rows or params["page"] != 1))):
            raise RuntimeError("Cloudflare 分页信息非法")
        if params["page"] >= total_pages:
            total_count = info.get("total_count", len(output))
            if isinstance(total_count, bool) or not isinstance(total_count, int) or total_count != len(output):
                raise RuntimeError("Cloudflare 分页记录数不一致，已拒绝不完整结果")
            return output
        params["page"] += 1


def _record_result(body, rtype=None, content=None, record_id=None):
    result = body.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"]:
        raise RuntimeError("Cloudflare 未返回有效记录，不能确认操作成功")
    if record_id is not None and result["id"] != record_id:
        raise RuntimeError("Cloudflare 返回的记录 id 不匹配")
    if rtype and result.get("type") != rtype:
        raise RuntimeError("Cloudflare 返回的记录类型不匹配")
    if rtype and str(result.get("name", "")).lower().rstrip(".") != config["cloudflare"]["record_name"]:
        raise RuntimeError("Cloudflare 返回的记录名称不匹配")
    if content is not None and str(result.get("content", "")).lower().rstrip(".") != content.lower().rstrip("."):
        raise RuntimeError("Cloudflare 未确认期望的记录内容")
    return result


def _dns_content(rtype, content):
    if rtype not in ("A", "CNAME"):
        raise RuntimeError("仅支持写入 A / CNAME 记录")
    if rtype == "A":
        try:
            return str(ipaddress.IPv4Address(content))
        except (ipaddress.AddressValueError, TypeError):
            raise RuntimeError("A 记录 content 必须是有效 IPv4 地址") from None
    return hostname(content, "CNAME 目标", allow_ip=False)


def cf_create_record(rtype: str, content: str) -> str:
    if config.get("dry_run"):
        raise RuntimeError("DRY-RUN 禁止执行真实 DNS 写操作")
    content = _dns_content(rtype, content)
    payload = {"type": rtype, "name": config["cloudflare"]["record_name"], "content": content,
               "ttl": config["probe"]["record_ttl"], "proxied": False}
    return _record_result(cf_request("POST", json=payload), rtype, content)["id"]


def cf_update_record(record_id: str, content: str) -> None:
    if config.get("dry_run"):
        raise RuntimeError("DRY-RUN 禁止执行真实 DNS 写操作")
    content = _dns_content(config["cloudflare"]["record_type"], content)
    _record_result(cf_request("PATCH", f"/{record_id}", json={"content": content, "ttl": config["probe"]["record_ttl"]}),
                   config["cloudflare"]["record_type"], content, record_id)


def cf_delete_record(record_id: str) -> None:
    if config.get("dry_run"):
        raise RuntimeError("DRY-RUN 禁止执行真实 DNS 写操作")
    _record_result(cf_request("DELETE", f"/{record_id}"), record_id=record_id)


def _single_record(records):
    expected = config["cloudflare"]["record_type"]
    conflicting = {"CNAME"} if expected == "A" else {"A", "AAAA"}
    if any(record["type"] in conflicting for record in records):
        raise RuntimeError("主域名存在冲突的 DNS 类型，已拒绝修改")
    selected = [record for record in records if record["type"] == expected]
    if len(selected) != 1:
        raise RuntimeError(f"单记录模式要求恰好一条 {expected} 记录，实际 {len(selected)} 条；已停止修改")
    return selected[0]


def cf_patch_content(content: str) -> bool:
    """基于本次远端读取决定是否写入，返回是否实际修改内容。"""
    records = cf_list_records()
    record = _single_record(records)
    content = _dns_content(config["cloudflare"]["record_type"], content)
    changed = record["content"].lower().rstrip(".") != content
    if changed:
        cf_update_record(record["id"], content)
    return changed


def _observe_records(records, ips=None):
    mode = config["mode"]
    nodes_cfg = config["nodes"]
    ips = ips or {}
    mapped = {}
    known_ips = {}
    a_records = [record for record in records if record["type"] == "A"]
    with lock:
        cached_nodes = copy.deepcopy(state["nodes"])
        owned_ids = set(state["owned_records"])
    for node in nodes_cfg:
        target = node["target"]
        cached = cached_nodes.get(target, {})
        known_ips[target] = {ips.get(target), cached.get("ip"), cached.get("last_ip")} - {None, ""}
        match = next((r for r in a_records if r["content"] in known_ips[target]), None)
        if match:
            mapped[target] = {"id": match["id"], "ip": match["content"], "owned": match["id"] in owned_ids}
    with lock:
        state["dns_records"] = [{key: r.get(key) for key in ("id", "type", "name", "content", "ttl", "proxied")} for r in records]
        state["records"] = mapped
        state["cf_loaded"] = True
        state["cf_observed_at"] = now_str()
        state["cf_stale"] = False
        state["dns_scope"] = dns_scope()
        state["unmanaged_records"] = [r["content"] for r in a_records if r["id"] not in state["owned_records"]]
        if mode == "round_robin":
            state["current"] = ""
            state["record_id"] = ""
            state["current_target"] = ""
        else:
            state["current"] = ""
            state["record_id"] = ""
            state["current_target"] = ""
    if mode != "round_robin":
        record = _single_record(records)
        current = record["content"].lower().rstrip(".")
        current_target = current if config["cloudflare"]["record_type"] == "CNAME" else ""
        if not current_target:
            current_target = next((n["target"] for n in nodes_cfg if current in known_ips[n["target"]]), "")
        with lock:
            state.update(current=current, current_target=current_target, record_id=record["id"])


def refresh_from_cf() -> bool:
    with operation_lock:
        try:
            records = cf_list_records()
            _observe_records(records)
            if config["mode"] == "round_robin" and any(r["type"] == "CNAME" for r in records):
                raise RuntimeError("主域名存在 CNAME，与多 A 记录模式冲突；已停止同步")
            with lock:
                state["cf_error"] = ""
            return True
        except Exception as error:
            with lock:
                state["cf_error"] = safe_error(error)
                state["cf_stale"] = True
            log(f"读取 Cloudflare 记录失败：{safe_error(error)}", "error")
            return False


def init_round_robin() -> bool:
    return refresh_from_cf()

# ---------------------------------------------------------------- 健康探测


def resolve_ip(host: str):
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_INET, proto=socket.IPPROTO_TCP)
        return str(ipaddress.IPv4Address(infos[0][4][0])) if infos else None
    except (OSError, ValueError):
        return None


def probe_api(target: str, probe_cfg=None) -> dict:
    probe_cfg = probe_cfg or config["probe"]
    url = probe_cfg["api_url"].format(target=quote(target, safe=""))
    started = time.monotonic()
    try:
        response = requests.get(url, timeout=probe_cfg["timeout_seconds"], headers={"User-Agent": f"cf-gtm/{VERSION}"})
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("探测接口 JSON 不是对象")
        elapsed = max(1, round((time.monotonic() - started) * 1000))
        code = body.get("code")
        if code == 200:
            data = body.get("data")
            if data is None:
                data = {}
            if not isinstance(data, dict):
                raise ValueError("探测接口 data 不是对象")
            try:
                latency = max(0, round(float(str(data.get("time")).replace("ms", "").strip())))
            except (ValueError, TypeError, OverflowError):
                latency = elapsed
            return {"status": "ok", "latency": latency, "server": str(data.get("server", ""))[:100]}
        if code == -2:
            return {"status": "fail", "latency": elapsed, "error": "ping 请求失败"}
        return {"status": "unknown", "latency": elapsed, "error": "探测接口未返回有效结论"}
    except Exception as error:
        return {"status": "unknown", "latency": None, "error": f"探测接口请求异常（{type(error).__name__}）"}


def probe_tcp(target: str, port: int = 443, probe_cfg=None) -> dict:
    probe_cfg = probe_cfg or config["probe"]
    started = time.monotonic()
    deadline = started + probe_cfg["timeout_seconds"]
    try:
        infos = socket.getaddrinfo(target, port, proto=socket.IPPROTO_TCP)
    except OSError:
        return {"status": "unknown", "latency": None, "error": "DNS 解析失败"}
    last_error = "未找到可用地址"
    seen = set()
    for family, socktype, proto, _canon, sockaddr in infos:
        if (family, sockaddr) in seen:
            continue
        seen.add((family, sockaddr))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            last_error = "TCP 探测超时"
            break
        try:
            with socket.socket(family, socktype, proto) as connection:
                connection.settimeout(remaining)
                connection.connect(sockaddr)
            return {"status": "ok", "latency": max(1, round((time.monotonic() - started) * 1000))}
        except OSError as error:
            last_error = f"TCP 连接失败（{type(error).__name__}）"
    return {"status": "fail", "latency": max(1, round((time.monotonic() - started) * 1000)), "error": last_error}


def judge(node: dict, decision_mode=None) -> bool:
    mode = decision_mode or config["probe"].get("decision_mode", "smart")
    api_ok = node.get("api", {}).get("status") == "ok"
    tcp_status = node.get("tcp", {}).get("status")
    if mode == "api_only":
        return api_ok
    if mode == "tcp_only":
        return tcp_status == "ok"
    if tcp_status == "ok":
        return True
    if tcp_status == "fail":
        return False
    return api_ok


def node_verdict(api_status, tcp_status, icmp_ever_ok=False) -> str:
    # 保留原展示字段；实际调度统一使用 judge() 的配置化结论。
    if api_status == "ok":
        return "正常"
    if api_status == "fail":
        return "国内探测失败"
    if tcp_status == "ok":
        return "正常"
    if tcp_status == "fail":
        return "异常"
    return "未知"

# ---------------------------------------------------------------- 切换 / 调度


def in_auto_window() -> bool:
    window = config.get("auto_window") or {}
    if not window.get("enabled"):
        return True
    start, end = window["start"], window["end"]
    current_time = datetime.now().strftime("%H:%M")
    return start <= current_time <= end if start <= end else current_time >= start or current_time <= end


def cooldown_ok() -> bool:
    minutes = config.get("cooldown_minutes", 0)
    return not minutes or time.time() - state.get("last_switch_ts", 0) >= minutes * 60


def auto_allowed() -> bool:
    return config.get("auto_switch", True) and in_auto_window() and cooldown_ok()


def node_weight(target: str) -> int:
    return next((n.get("weight", 1) for n in config.get("nodes", []) if n["target"] == target), 1)


def node_enabled(target: str) -> bool:
    return next((n.get("enabled", True) for n in config.get("nodes", []) if n["target"] == target), False)


def switch_to(target: str, reason: str, quiet: bool = False, manual: bool = False) -> bool:
    with operation_lock:
        exists = any(node["target"] == target for node in config["nodes"])
        if not exists or (not manual and not node_enabled(target)):
            with lock:
                state["cf_error"] = "节点不存在或不参与自动切换"
            return False
        try:
            content = target
            if config["cloudflare"]["record_type"] == "A":
                content = resolve_ip(target)
                if not content:
                    raise RuntimeError("目标节点无法解析为 IPv4，未修改 DNS")
            content = _dns_content(config["cloudflare"]["record_type"], content)
            if config["dry_run"]:
                with lock:
                    state["simulation"] = {"mode": config["mode"], "current": target, "content": content, "at": now_str(), "reason": reason}
                if not quiet:
                    log(f"[DRY-RUN] 计划切换至 {target}（{reason}）", "warn")
                save_state()
                return True
            changed = cf_patch_content(content)
            if not refresh_from_cf():
                raise RuntimeError("写入后读取 Cloudflare 失败，无法确认当前解析")
            with lock:
                if state["current"] != content:
                    raise RuntimeError("Cloudflare 当前解析与期望不一致")
                state["current_target"] = target
                state["cf_error"] = ""
                if changed:
                    state["last_switch_ts"] = time.time()
            if not quiet:
                log(f"已确认解析为 {content}（{reason}）", "warn")
            save_state()
            return True
        except Exception as error:
            with lock:
                state["cf_error"] = safe_error(error)
                state["cf_stale"] = True
            log(f"切换失败：{safe_error(error)}", "error")
            return False


def weighted_next(healthy: list) -> str:
    if not healthy:
        raise ValueError("没有可调度的健康节点")
    with lock:
        weighted = state.setdefault("weighted", {"acc": {}, "last_slice": 0})
        acc = weighted["acc"] = {target: weighted.get("acc", {}).get(target, 0) for target, _ in healthy}
        total = sum(weight for _, weight in healthy)
        for target, weight in healthy:
            acc[target] += weight
        best = max(healthy, key=lambda item: acc[item[0]])[0]
        acc[best] -= total
        return best

# ---------------------------------------------------------------- round_robin


def sync_round_robin(results: dict) -> bool:
    with operation_lock:
        # 所有入口均执行门控，不能靠调用者自律保证自动关闭时不写 DNS。
        if config["mode"] != "round_robin" or not auto_allowed():
            return True
        enabled = {node["target"] for node in config["nodes"] if node["enabled"]}
        desired = {result["ip"] for target, result in results.items()
                   if target in enabled and result.get("healthy") and result.get("ip")}
        try:
            desired = {_dns_content("A", ip) for ip in desired}
            if config["dry_run"]:
                with lock:
                    records = copy.deepcopy(state["dns_records"])
            else:
                records = cf_list_records()
                _observe_records(records, {target: result.get("ip") for target, result in results.items()})
            if any(record["type"] == "CNAME" for record in records):
                raise RuntimeError("主域名存在 CNAME，与多 A 记录冲突，未执行修改")
            a_records = [record for record in records if record["type"] == "A"]
            actual_ips = {record["content"] for record in a_records}
            # 宽限只保留远端已存在的地址，不能凭历史健康新建当前探测失败的地址。
            for target, result in results.items():
                cached = state["nodes"].get(target, {})
                failures = cached.get("fail", config["probe"]["fail_threshold"])
                if target in enabled and failures < config["probe"]["fail_threshold"]:
                    desired.update(actual_ips.intersection({result.get("ip"), cached.get("last_ip")}))
            if config["dry_run"]:
                with lock:
                    state["simulation"] = {"mode": "round_robin", "ips": sorted(desired), "at": now_str(), "keep_existing_if_empty": not bool(desired)}
                log(f"[DRY-RUN] 计划同步 {len(desired)} 个 IPv4；真实记录保持不变", "warn")
                return True
            if not desired:
                log("没有健康且启用的 IPv4 节点，保留现有 A 记录", "warn")
                with lock:
                    state["cf_error"] = ""
                return True
            changed = False
            created_ids = set()
            # 先补齐所有健康地址；任一创建失败即停止，不继续删除旧地址。
            for ip in sorted(desired - actual_ips):
                rid = cf_create_record("A", ip)
                created_ids.add(rid)
                with lock:
                    state["owned_records"][rid] = {"ip": ip}
                    state["last_switch_ts"] = time.time()
                if not save_state():
                    raise RuntimeError("新记录已创建，但所有权状态未能持久化；已停止后续删除")
                actual_ips.add(ip)
                changed = True
                log(f"创建 A 记录 -> {ip}", "warn")
            # 只删除本程序显式创建且内容仍匹配的记录；不接管同 IP 的未知记录。
            owned = copy.deepcopy(state["owned_records"])
            by_id = {record["id"]: record for record in a_records}
            for rid, item in owned.items():
                record = by_id.get(rid)
                if record is None:
                    # 本轮刚创建的记录不在起始快照中，必须保留其所有权。
                    if rid not in created_ids:
                        with lock:
                            state["owned_records"].pop(rid, None)
                    continue
                if record["content"] != item.get("ip"):
                    with lock:
                        state["owned_records"].pop(rid, None)
                    continue
                if record["content"] not in desired:
                    cf_delete_record(rid)
                    with lock:
                        state["owned_records"].pop(rid, None)
                        state["last_switch_ts"] = time.time()
                    changed = True
                    log(f"删除已管理的 A 记录 -> {record['content']}", "warn")
            if not refresh_from_cf():
                raise RuntimeError("同步后重新读取 Cloudflare 失败，无法确认记录集合")
            with lock:
                if changed:
                    state["last_switch_ts"] = time.time()
                state["cf_error"] = ""
            return True
        except Exception as error:
            with lock:
                state["cf_error"] = safe_error(error)
                state["cf_stale"] = True
            log(f"A 记录同步失败：{safe_error(error)}", "error")
            return False
        finally:
            save_state()


def node_sync_status(target: str, healthy, ip) -> str:
    if config["mode"] != "round_robin":
        return "-"
    with lock:
        rec = state["records"].get(target)
    if not node_enabled(target):
        return "已禁用（仍有记录）" if rec else "已禁用"
    if config["dry_run"]:
        return "模拟（未写 DNS）"
    if not state["cf_loaded"]:
        return "尚未读取"
    if healthy is None:
        return "已有记录，待探测" if rec else "-"
    if healthy and ip:
        return "已同步" if rec and rec.get("ip") == ip else "待同步"
    if rec:
        return "待摘除" if rec.get("owned") else "保留外部记录"
    return "已摘除"

# ---------------------------------------------------------------- 主循环


def _probe_one(node, probe_cfg):
    target = node["target"]
    ip = resolve_ip(target)
    api = probe_api(target, probe_cfg)
    tcp = probe_tcp(target, node["port"], probe_cfg)
    result = {"api": api, "tcp": tcp, "ip": ip}
    result["healthy"] = judge(result, probe_cfg["decision_mode"])
    result["verdict"] = node_verdict(api.get("status"), tcp.get("status"))
    return result


def probe_round(_acquired=False) -> bool:
    if not _acquired and not probe_lock.acquire(blocking=False):
        return False
    with lock:
        state["probing"] = True
        state["last_probe_error"] = ""
        snapshot = copy.deepcopy(config)
        revision = _config_revision
    try:
        results = {}
        nodes_cfg = snapshot.get("nodes", [])
        if nodes_cfg:
            with ThreadPoolExecutor(max_workers=min(8, len(nodes_cfg)), thread_name_prefix="gtm-probe") as pool:
                futures = {pool.submit(_probe_one, node, snapshot["probe"]): node["target"] for node in nodes_cfg}
                for future in as_completed(futures):
                    results[futures[future]] = future.result()
        # 配置提交与探测结果应用串行；网络探测期间允许编辑，旧结果整轮丢弃。
        with operation_lock:
            with lock:
                if revision != _config_revision:
                    state["last_probe_error"] = "探测期间配置发生变化，已丢弃旧结果，请重新探测"
                    return False
                threshold = config["probe"]["fail_threshold"]
                for node in nodes_cfg:
                    target = node["target"]
                    result = results[target]
                    previous = state["nodes"].get(target, {})
                    failures = 0 if result["healthy"] else previous.get("fail", 0) + 1
                    effective = result["healthy"] or (previous.get("effective_healthy", previous.get("healthy", False)) and failures < threshold)
                    result["effective_healthy"] = effective
                    state["nodes"][target] = {
                        **result, "fail": failures, "succ": previous.get("succ", 0) + 1 if result["healthy"] else 0,
                        "last_ip": (result.get("ip") if result["healthy"] and result.get("ip")
                                    else previous.get("last_ip") or previous.get("ip") or result.get("ip")),
                        "last_check": now_str(), "icmp_ever_ok": previous.get("icmp_ever_ok", False) or result["api"].get("status") == "ok",
                    }
                state["last_round"] = now_str()
            # current 只来自 Cloudflare；读取失败时禁止基于猜测调度。
            if not config["dry_run"] and not refresh_from_cf():
                return False
            if config["dry_run"] and not state["cf_loaded"]:
                refresh_from_cf()  # 允许读取真实状态，但模拟计划不覆盖它。
            if not auto_allowed():
                return True
            if config["mode"] == "round_robin":
                return sync_round_robin(results)
            current = state.get("current_target") or state["current"]
            if config["cloudflare"]["record_type"] == "A":
                current = next((node["target"] for node in nodes_cfg if results[node["target"]].get("ip") == state["current"]), current)
            healthy = [(node["target"], node["weight"]) for node in nodes_cfg if node["enabled"] and results[node["target"]]["healthy"]]
            if not healthy:
                log("所有启用节点均不健康，保持当前解析不变", "warn")
                return True
            current_result = results.get(current, {})
            unavailable = not any(node["target"] == current for node in nodes_cfg)
            failed = not current_result.get("healthy") and state["nodes"].get(current, {}).get("fail", 0) >= threshold
            # 自动参与开关只影响候选池；健康的手动当前节点保留原 failover 语义。
            # 已删除或外部目标不受失败计数约束；配置本身绝不伪造 DNS 切换。
            if unavailable or failed:
                chosen = healthy[0][0] if config["mode"] == "failover" else max(healthy, key=lambda item: item[1])[0]
                return switch_to(chosen, "当前节点已移除或连续探测失败")
            if config["mode"] == "weighted":
                # 未达失败阈值时不借时间片提前绕过故障保护。
                if not current_result.get("healthy"):
                    return True
                now_ts = time.time()
                with lock:
                    due = now_ts - state["weighted"].get("last_slice", 0) >= config["scheduler"]["slice_seconds"]
                    weighted_before = copy.deepcopy(state["weighted"])
                if due:
                    chosen = weighted_next(healthy)
                    success = chosen == current or switch_to(chosen, "按权重轮换")
                    with lock:
                        if success:
                            state["weighted"]["last_slice"] = now_ts
                        else:
                            state["weighted"] = weighted_before
                    return success
            return True
    except Exception as error:
        with lock:
            state["last_probe_error"] = safe_error(error)
        log(f"探测循环异常：{safe_error(error)}", "error")
        return False
    finally:
        with lock:
            state["probing"] = False
        save_state()
        probe_lock.release()


def request_probe():
    if not probe_lock.acquire(blocking=False):
        return False
    with lock:
        state["probing"] = True
    try:
        threading.Thread(target=probe_round, kwargs={"_acquired": True}, daemon=True, name="gtm-manual-probe").start()
    except Exception:
        with lock:
            state["probing"] = False
        probe_lock.release()
        raise
    return True


def probe_loop() -> None:
    while not stop_event.is_set():
        probe_round()
        with lock:
            interval = config["probe"]["interval_seconds"]
        stop_event.wait(interval)

# ---------------------------------------------------------------- Web / 会话 / CSRF


def _auth_version():
    web = config.get("web", {})
    key = str(app.secret_key or web.get("secret", "")).encode("utf-8")
    return hmac.new(key, (web.get("password", "") + "\0" + web.get("secret", "")).encode("utf-8"), hashlib.sha256).hexdigest()


def _logged_in() -> bool:
    version = session.get("auth_version")
    return (session.get("auth") is True and isinstance(version, str)
            and secrets.compare_digest(version.encode("utf-8"), _auth_version().encode("utf-8")))


def csrf_token():
    token = session.get("csrf_token")
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@app.before_request
def _auth_hook():
    path = request.path
    if path.startswith("/static/"):
        return None
    if request.method in ("GET", "HEAD") and path in ("/tools/install-ping-api.sh", "/tools/ping_api.py"):
        return None
    if path != "/login" and not _logged_in():
        if path.startswith("/api/") or path == "/logout":
            return jsonify({"ok": False, "msg": "未登录或会话已失效", "login": True}), 401
        return redirect("/login")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        expected = session.get("csrf_token")
        supplied = request.headers.get("X-CSRF-Token", "")
        if (not isinstance(expected, str) or not supplied
                or not secrets.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))):
            return jsonify({"ok": False, "msg": "安全令牌已失效，请刷新页面后重试"}), 403
    return None


@app.after_request
def response_headers(response):
    if not request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Vary"] = "Cookie"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@app.errorhandler(ValidationError)
def invalid_input(error):
    return jsonify({"ok": False, "msg": safe_error(error)}), 400


@app.errorhandler(OSError)
def persistence_error(error):
    log("文件保存失败，操作未提交", "error")
    return jsonify({"ok": False, "msg": "文件保存失败，操作未提交"}), 500


def json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValidationError("请求必须为有效 JSON 对象")
    return data


def serialized(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with operation_lock:
            # 排队期间可能发生密码变更；持锁后重新核实会话与 CSRF。
            rejected = _auth_hook()
            if rejected is not None:
                return rejected
            return function(*args, **kwargs)
    return wrapped


@app.get("/login")
def login_page():
    if _logged_in():
        return redirect("/")
    return render_template("login.html", csrf_token=csrf_token(), asset_version=VERSION)


@app.post("/login")
@serialized
def login_post():
    data = json_body()
    password = text_value(data.get("password", ""), "password", 1024, allow_empty=True)
    current = config.get("web", {}).get("password", "")
    if password and secrets.compare_digest(password.encode("utf-8"), current.encode("utf-8")):
        session.clear()
        session["auth"] = True
        session["auth_version"] = _auth_version()
        return jsonify({"ok": True, "csrf_token": csrf_token()})
    return jsonify({"ok": False, "msg": "密码错误"}), 401


@app.post("/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/tools/install-ping-api.sh")
@app.get("/tools/ping_api.py")
def download_ping_api():
    # 只公开两份固定源码，配置文件和任意路径均不在下载范围内。
    filename = {"/tools/install-ping-api.sh": "install-ping-api.sh", "/tools/ping_api.py": "ping_api.py"}[request.path]
    return send_file(os.path.join(app.root_path, "tools", filename), mimetype="text/plain", as_attachment=True, download_name=filename)


@app.get("/")
def index():
    return render_template("index.html", csrf_token=csrf_token(), asset_version=VERSION)


def public_config() -> dict:
    with lock:
        cf, probe = config["cloudflare"], config["probe"]
        return {
            "record_name": cf["record_name"], "record_type": cf["record_type"], "zone_id": cf["zone_id"],
            "api_token_masked": masked_token(), "has_token": bool(cf["api_token"]),
            **{key: probe[key] for key in ("interval_seconds", "timeout_seconds", "fail_threshold", "decision_mode", "api_url", "record_ttl")},
            "slice_seconds": config["scheduler"]["slice_seconds"], "mode": config["mode"],
            "dry_run": config["dry_run"], "auto_switch": config["auto_switch"],
            "auto_window": copy.deepcopy(config["auto_window"]), "cooldown_minutes": config["cooldown_minutes"],
            "has_password": bool(config["web"].get("password")),
        }


@app.get("/api/status")
def api_status():
    with lock:
        nodes = []
        for node in config["nodes"]:
            target = node["target"]
            current_state = state["nodes"].get(target, {})
            if config["mode"] == "round_robin":
                is_current = target in state["records"]
            elif config["cloudflare"]["record_type"] == "A":
                is_current = bool(state["current"] and state["current"] in (current_state.get("ip"), current_state.get("last_ip")))
            else:
                is_current = target == state["current"]
            nodes.append({
                "name": node["name"], "target": target, "port": node["port"], "weight": node["weight"], "enabled": node["enabled"],
                "api": current_state.get("api", {}), "tcp": current_state.get("tcp", {}), "ip": current_state.get("ip"),
                "healthy": current_state.get("healthy"), "fail": current_state.get("fail", 0),
                "last_check": current_state.get("last_check", ""), "current": is_current,
                "sync": node_sync_status(target, current_state.get("healthy"), current_state.get("ip")),
                "verdict": node_verdict(current_state.get("api", {}).get("status"), current_state.get("tcp", {}).get("status")),
            })
        current = state["current"]
        if config["mode"] == "round_robin":
            count = sum(record["type"] == "A" for record in state["dns_records"])
            current = f"轮询 {count} 条 A 记录" if state["cf_loaded"] else ""
        return jsonify({
            "mode": config["mode"], "current": current, "dry_run": config["dry_run"], "auto_switch": config["auto_switch"],
            "decision_mode": config["probe"]["decision_mode"], "interval": config["probe"]["interval_seconds"],
            "threshold": config["probe"]["fail_threshold"], "record_name": config["cloudflare"]["record_name"],
            "has_token": bool(config["cloudflare"]["api_token"]), "record_type": config["cloudflare"]["record_type"],
            "cf_error": state["cf_error"], "last_round": state["last_round"], "nodes": nodes,
            "logs": copy.deepcopy(state["logs"][:50]), "config": public_config(), "version": VERSION,
            "probing": state["probing"], "last_probe_error": state["last_probe_error"], "cf_loaded": state["cf_loaded"],
            "cf_observed_at": state["cf_observed_at"], "cf_stale": state["cf_stale"],
            "dns_records": copy.deepcopy(state["dns_records"]), "unmanaged_records": list(state["unmanaged_records"]),
            "simulation": copy.deepcopy(state["simulation"]),
        })


@app.post("/api/probe")
@serialized
def api_probe():
    json_body()
    if not request_probe():
        return jsonify({"ok": False, "msg": "已有探测正在执行", "probing": True}), 409
    return jsonify({"ok": True, "probing": True})


@app.post("/api/switch")
@serialized
def api_switch():
    data = json_body()
    if config["mode"] == "round_robin":
        raise ValidationError("round_robin 模式无需手动切换")
    target = hostname(data.get("target"))
    if not any(node["target"] == target for node in config["nodes"]):
        raise ValidationError("节点不存在")
    ok = switch_to(target, "手动切换", manual=True)
    return jsonify({"ok": ok, "current": state["current"], "simulated": config["dry_run"],
                    "msg": "" if ok else state["cf_error"]}), 200 if ok else 502


@app.post("/api/auto")
@serialized
def api_auto():
    data = json_body()
    enabled = bool_value(data.get("enabled"), "enabled")
    candidate = copy.deepcopy(config)
    candidate["auto_switch"] = enabled
    commit_config(candidate)
    log(f"自动切换已{'开启' if enabled else '关闭'}")
    return jsonify({"ok": True, "auto_switch": enabled})


@app.get("/api/config")
def api_get_config():
    return jsonify(public_config())


@app.post("/api/config")
@serialized
def api_set_config():
    data = json_body()
    candidate = copy.deepcopy(config)
    cf, probe = candidate["cloudflare"], candidate["probe"]
    if "api_token" in data:
        token = text_value(data["api_token"], "api_token", allow_empty=True)
        if token and token not in ("***", "已配置", masked_token()):
            cf["api_token"] = token
    for key in ("zone_id", "record_name", "record_type"):
        if key in data:
            cf[key] = data[key]
    for key in ("interval_seconds", "timeout_seconds", "fail_threshold", "record_ttl", "decision_mode", "api_url"):
        if key in data:
            probe[key] = data[key]
    for key in ("mode", "auto_switch", "dry_run", "cooldown_minutes"):
        if key in data:
            candidate[key] = data[key]
    if "slice_seconds" in data:
        candidate["scheduler"]["slice_seconds"] = data["slice_seconds"]
    if "auto_window" in data:
        if not isinstance(data["auto_window"], dict):
            raise ValidationError("auto_window 必须是对象")
        candidate["auto_window"].update(data["auto_window"])
    commit_config(candidate)
    log("配置已更新")
    return jsonify({"ok": True, "config": public_config()})


@app.post("/api/password")
@serialized
def api_password():
    data = json_body()
    old = text_value(data.get("old", ""), "old", 1024, allow_empty=True)
    new = text_value(data.get("new", ""), "new", 1024, allow_empty=True)
    if not secrets.compare_digest(old.encode("utf-8"), config["web"]["password"].encode("utf-8")):
        raise ValidationError("当前密码不正确")
    if len(new) < 6:
        raise ValidationError("新密码至少 6 位")
    candidate = copy.deepcopy(config)
    candidate["web"]["password"] = new
    commit_config(candidate)
    # 当前客户端续签，其他已经签名的旧密码会话全部失效。
    session["auth_version"] = _auth_version()
    log("面板密码已更新，其他会话已失效")
    return jsonify({"ok": True})


@app.post("/api/test_cf")
@serialized
def api_test_cf():
    json_body()
    try:
        records = cf_list_records()
        _observe_records(records)
        if config["mode"] == "round_robin" and any(record["type"] == "CNAME" for record in records):
            raise RuntimeError("主域名存在 CNAME，与多 A 模式冲突")
        with lock:
            state["cf_error"] = ""
        return jsonify({"ok": True, "count": len(records), "records": [
            {"type": r["type"], "content": r["content"], "ttl": r.get("ttl")} for r in records]})
    except Exception as error:
        with lock:
            state["cf_error"] = safe_error(error)
            state["cf_stale"] = True
        return jsonify({"ok": False, "msg": safe_error(error)}), 502


@app.post("/api/nodes")
@serialized
def api_add_node():
    data = json_body()
    target = hostname(data.get("target"))
    candidate = copy.deepcopy(config)
    name = data.get("name", target)
    if name == "":
        name = target
    candidate["nodes"].append({"name": name, "target": target,
                               "port": data.get("port", 443), "weight": data.get("weight", 1), "enabled": data.get("enabled", True)})
    commit_config(candidate)
    log(f"添加节点 {target}")
    return jsonify({"ok": True})


@app.post("/api/nodes/reorder")
@serialized
def api_reorder_nodes():
    data = json_body()
    order = data.get("order")
    if not isinstance(order, list) or not all(isinstance(target, str) for target in order):
        raise ValidationError("order 必须是 target 字符串列表")
    candidate = copy.deepcopy(config)
    by_target = {node["target"]: node for node in candidate["nodes"]}
    normalized = [hostname(target) for target in order]
    if any(target not in by_target for target in normalized):
        raise ValidationError("order 包含未知节点")
    seen = set()
    ordered = []
    for target in normalized + list(by_target):
        if target not in seen:
            seen.add(target)
            ordered.append(by_target[target])
    candidate["nodes"] = ordered
    commit_config(candidate)
    log("节点顺序已更新")
    return jsonify({"ok": True})


@app.delete("/api/nodes")
@serialized
def api_del_node():
    target = hostname(request.args.get("target"))
    if not any(node["target"] == target for node in config["nodes"]):
        return jsonify({"ok": False, "msg": "节点不存在"}), 404
    candidate = copy.deepcopy(config)
    candidate["nodes"] = [node for node in candidate["nodes"] if node["target"] != target]
    commit_config(candidate)
    # current 与 owned_records 必须保留；前者仍是真实解析，后者供后续安全清理。
    log(f"删除节点 {target}；真实 DNS 将按自动调度设置处理")
    return jsonify({"ok": True})


@app.post("/api/nodes/update")
@serialized
def api_update_node():
    data = json_body()
    target = hostname(data.get("target"))
    candidate = copy.deepcopy(config)
    node = next((item for item in candidate["nodes"] if item["target"] == target), None)
    if node is None:
        return jsonify({"ok": False, "msg": "节点不存在"}), 404
    for key in ("name", "port", "weight", "enabled"):
        if key in data:
            node[key] = data[key]
    commit_config(candidate)
    return jsonify({"ok": True})

# ---------------------------------------------------------------- 启动


def init_app(start_background=False, refresh=False) -> None:
    global _initialized, _background_thread
    with operation_lock:
        if not _initialized:
            load_config()
            app.secret_key = config["web"]["secret"]
            app.config["SESSION_COOKIE_SECURE"] = config["web"].get("cookie_secure", False)
            with lock:
                state["started_at"] = now_str()
            _initialized = True
            log(f"CF-GTM 启动 | 模式 {config['mode']} | 节点 {len(config['nodes'])} | 自动切换 {'开' if config['auto_switch'] else '关'}")
        if refresh:
            refresh_from_cf()
        if start_background and (_background_thread is None or not _background_thread.is_alive()):
            stop_event.clear()
            _background_thread = threading.Thread(target=probe_loop, daemon=True, name="gtm-probe-loop")
            _background_thread.start()


def main() -> None:
    init_app(start_background=True, refresh=True)
    web = config["web"]
    app.run(host=web["host"], port=web["port"], threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
