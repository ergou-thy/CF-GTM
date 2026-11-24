#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CF-GTM —— 基于 Cloudflare DNS 的轻量级全局流量管理（GTM 平替）

桃花酱出品 · @taohuayuang

三种模式：
  failover     单条记录，节点故障时切换目标（主备容灾）
  round_robin  主域名多条 A 记录，等概率轮询 + 故障摘除（无权重）
  weighted     单条记录，按权重做时间片轮换（带一点均衡 + 故障切换）

健康检查 = xxapi.cn 国内 Ping 接口（ICMP） + 本地 TCP 端口探测
自带 Web 控制台：主域名绑定、节点增删、频率/权重设置、日志

运行：
  pip install -r requirements.txt
  python app.py
  浏览器打开 http://<服务器IP>:8787
"""

import os
import json
import time
import socket
import secrets
import threading
from datetime import datetime
from urllib.parse import quote

import requests
from flask import Flask, request, jsonify, Response, session, redirect

# ---------------------------------------------------------------- 基础路径

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")

CF_API = "https://api.cloudflare.com/client/v4"

DEFAULT_CONFIG = {
    "cloudflare": {
        "api_token": "",
        "zone_id": "",
        "record_name": "main.example.com",
        "record_type": "CNAME",
    },
    "probe": {
        "interval_seconds": 60,
        "timeout_seconds": 10,
        "fail_threshold": 2,
        "decision_mode": "smart",
        "api_url": "https://v2.xxapi.cn/api/ping?url={target}",
        "record_ttl": 60,
    },
    "scheduler": {
        "slice_seconds": 300,
    },
    "auto_window": {
        "enabled": False,
        "start": "00:00",
        "end": "23:59",
    },
    "cooldown_minutes": 0,
    "nodes": [
        {"name": "日本A", "target": "jp1.example.com", "port": 443, "weight": 1},
        {"name": "新加坡A", "target": "sg1.example.com", "port": 443, "weight": 1},
    ],
    "mode": "failover",
    "auto_switch": True,
    "dry_run": False,
    "web": {"host": "0.0.0.0", "port": 8787, "password": ""},
}

app = Flask(__name__)
lock = threading.RLock()

config = {}
state = {
    "current": "",
    "record_id": "",
    "records": {},
    "nodes": {},
    "logs": [],
    "started_at": "",
    "last_round": "",
    "cf_error": "",
    "weighted": {"acc": {}, "last_slice": 0},
    "last_switch_ts": 0,
}


# ---------------------------------------------------------------- 工具函数

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str, level: str = "info") -> None:
    entry = {"ts": now_str(), "level": level, "msg": str(msg)}
    with lock:
        state["logs"].insert(0, entry)
        del state["logs"][200:]
    print(f"[{entry['ts']}] [{level.upper()}] {msg}", flush=True)
    try:
        with open(os.path.join(BASE_DIR, "gtm.log"), "a", encoding="utf-8") as f:
            f.write(f"[{entry['ts']}] [{level.upper()}] {msg}\n")
    except OSError:
        pass


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> None:
    global config
    if not os.path.exists(CONFIG_PATH):
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        save_config()
        log("未找到 config.json，已生成示例配置", "warn")
    else:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        config = deep_merge(DEFAULT_CONFIG, user_cfg)

    for n in config.get("nodes") or []:
        n.setdefault("weight", 1)
        n.setdefault("enabled", True)
    if config.get("mode") not in ("failover", "round_robin", "weighted"):
        config["mode"] = "failover"

    web = config.setdefault("web", {})
    if not web.get("password"):
        pwd = secrets.token_urlsafe(9)
        web["password"] = pwd
        save_config()
        print(f"[安全] 未设置面板密码，已自动生成：{pwd}（可在面板「安全」里修改）", flush=True)
    if not web.get("secret"):
        web["secret"] = secrets.token_hex(16)
        save_config()

    with lock:
        state["current"] = config["nodes"][0]["target"] if config["nodes"] else ""


def save_config() -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def save_state() -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def masked_token() -> str:
    tok = config["cloudflare"].get("api_token", "")
    if not tok:
        return ""
    return tok[:6] + "..." + tok[-4:] if len(tok) > 12 else "***"


# ---------------------------------------------------------------- Cloudflare

def cf_headers() -> dict:
    return {
        "Authorization": f"Bearer {config['cloudflare'].get('api_token', '')}",
        "Content-Type": "application/json",
    }


def cf_list_records(rtype: str = None):
    cf = config["cloudflare"]
    if not cf.get("api_token") or not cf.get("zone_id"):
        raise RuntimeError("未配置 api_token / zone_id")
    params = {"name": cf.get("record_name", "")}
    if rtype:
        params["type"] = rtype
    r = requests.get(
        f"{CF_API}/zones/{cf['zone_id']}/dns_records",
        headers=cf_headers(), params=params, timeout=15,
    )
    j = r.json()
    if not j.get("success"):
        raise RuntimeError(f"Cloudflare 查询失败: {j.get('errors')}")
    return j.get("result") or []


def cf_create_record(rtype: str, content: str) -> str:
    cf = config["cloudflare"]
    payload = {
        "type": rtype,
        "name": cf["record_name"],
        "content": content,
        "ttl": int(config["probe"].get("record_ttl", 60)),
        "proxied": False,
    }
    r = requests.post(
        f"{CF_API}/zones/{cf['zone_id']}/dns_records",
        headers=cf_headers(), json=payload, timeout=15,
    )
    j = r.json()
    if not j.get("success"):
        raise RuntimeError(f"Cloudflare 创建失败: {j.get('errors')}")
    return j["result"]["id"]


def cf_update_record(record_id: str, content: str) -> None:
    cf = config["cloudflare"]
    r = requests.patch(
        f"{CF_API}/zones/{cf['zone_id']}/dns_records/{record_id}",
        headers=cf_headers(), json={"content": content}, timeout=15,
    )
    j = r.json()
    if not j.get("success"):
        raise RuntimeError(f"Cloudflare 更新失败: {j.get('errors')}")


def cf_delete_record(record_id: str) -> None:
    cf = config["cloudflare"]
    r = requests.delete(
        f"{CF_API}/zones/{cf['zone_id']}/dns_records/{record_id}",
        headers=cf_headers(), timeout=15,
    )
    j = r.json()
    if not j.get("success"):
        raise RuntimeError(f"Cloudflare 删除失败: {j.get('errors')}")


def cf_patch_content(content: str) -> None:
    cf = config["cloudflare"]
    with lock:
        record_id = state.get("record_id")
    if not record_id:
        rtype = cf.get("record_type", "CNAME")
        recs = cf_list_records(rtype) or cf_list_records()
        if not recs:
            raise RuntimeError(f"Cloudflare 中找不到记录 {cf.get('record_name')}")
        rec = recs[0]
        with lock:
            state["record_id"] = rec["id"]
        record_id = rec["id"]

    r = requests.patch(
        f"{CF_API}/zones/{cf['zone_id']}/dns_records/{record_id}",
        headers=cf_headers(), json={"content": content}, timeout=15,
    )
    j = r.json()
    if not j.get("success"):
        raise RuntimeError(f"Cloudflare 修改失败: {j.get('errors')}")


def refresh_from_cf() -> None:
    if config.get("mode") == "round_robin":
        init_round_robin()
        return

    cf = config["cloudflare"]
    if not cf.get("api_token") or not cf.get("zone_id"):
        log("未配置 Cloudflare Token/Zone，跳过读取当前解析", "warn")
        return
    try:
        rtype = cf.get("record_type", "CNAME")
        recs = cf_list_records(rtype) or cf_list_records()
        if not recs:
            raise RuntimeError(f"Cloudflare 中找不到记录 {cf.get('record_name')}")
        rec = recs[0]
        with lock:
            state["record_id"] = rec["id"]
            state["current"] = (rec.get("content") or "").rstrip(".")
            state["cf_error"] = ""
        log(f"已读取 Cloudflare 当前解析：{state['current']}")
    except Exception as e:  # noqa: BLE001
        with lock:
            state["cf_error"] = str(e)
        log(f"读取 Cloudflare 记录失败：{e}", "error")


def init_round_robin() -> None:
    if config.get("dry_run"):
        log("DRY-RUN 模式：跳过读取主域名 A 记录，首轮探测时模拟同步", "warn")
        return
    try:
        all_recs = cf_list_records()
        cnames = [r for r in all_recs if r["type"] == "CNAME"]
        if cnames:
            log(
                f"注意：主域名存在 CNAME 记录（{cnames[0].get('content')}），"
                f"与多 A 记录模式冲突，请先删除 CNAME 或改用 failover/weighted 模式",
                "error",
            )
        a_recs = [r for r in all_recs if r["type"] == "A"]
        mapped = {}
        for node in config.get("nodes") or []:
            ip = resolve_ip(node["target"])
            if not ip:
                continue
            for r in a_recs:
                if r.get("content") == ip:
                    mapped[node["target"]] = {"id": r["id"], "ip": ip}
                    break
        with lock:
            state["records"] = mapped
            state["cf_error"] = ""
        log(f"已加载主域名 A 记录 {len(a_recs)} 条，匹配到节点 {len(mapped)} 个")
    except Exception as e:  # noqa: BLE001
        with lock:
            state["cf_error"] = str(e)
        log(f"初始化多 A 记录失败：{e}", "error")


# ---------------------------------------------------------------- 健康探测

def resolve_ip(host: str):
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_INET, proto=socket.IPPROTO_TCP)
        return infos[0][4][0] if infos else None
    except socket.gaierror:
        return None


def probe_api(target: str) -> dict:
    url = config["probe"]["api_url"].format(target=quote(target))
    timeout = config["probe"].get("timeout_seconds", 10)
    t0 = time.time()
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "cf-gtm/1.2"})
        elapsed = max(1, round((time.time() - t0) * 1000))
        j = r.json()
        code = j.get("code")
        if code == 200:
            data = j.get("data") or {}
            latency = data.get("time")
            try:
                latency = round(float(str(latency).replace("ms", "").strip()))
            except (TypeError, ValueError):
                latency = elapsed
            return {"status": "ok", "latency": latency, "server": data.get("server", "")}
        if code == -2:
            return {"status": "fail", "latency": elapsed, "error": "ping 请求失败"}
        return {"status": "unknown", "latency": elapsed, "error": f"code={code}"}
    except Exception as e:  # noqa: BLE001
        return {"status": "unknown", "latency": None, "error": str(e)}


def probe_tcp(target: str, port: int = 443) -> dict:
    timeout = config["probe"].get("timeout_seconds", 10)
    t0 = time.time()
    try:
        infos = socket.getaddrinfo(target, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        return {"status": "unknown", "latency": None, "error": f"DNS 解析失败: {e}"}

    last_err = ""
    for family, socktype, proto, _canon, sockaddr in infos:
        s = socket.socket(family, socktype, proto)
        s.settimeout(timeout)
        try:
            s.connect(sockaddr)
            s.close()
            return {"status": "ok", "latency": max(1, round((time.time() - t0) * 1000))}
        except OSError as e:  # noqa: PERF203
            last_err = str(e)
            try:
                s.close()
            except OSError:
                pass
    return {
        "status": "fail",
        "latency": max(1, round((time.time() - t0) * 1000)),
        "error": last_err,
    }


def judge(node: dict) -> bool:
    mode = config["probe"].get("decision_mode", "smart")
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


# ---------------------------------------------------------------- 切换 / 调度

def in_auto_window() -> bool:
    """自动切换生效时间段（支持跨天，例如 23:00-07:00）。"""
    w = config.get("auto_window") or {}
    if not w.get("enabled"):
        return True
    start = str(w.get("start") or "00:00")[:5]
    end = str(w.get("end") or "23:59")[:5]
    now = datetime.now().strftime("%H:%M")
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end


def cooldown_ok() -> bool:
    """切换冷却：距离上次切换不足 N 分钟则不再自动切换。"""
    try:
        minutes = int(config.get("cooldown_minutes", 0) or 0)
    except (TypeError, ValueError):
        minutes = 0
    if minutes <= 0:
        return True
    last = state.get("last_switch_ts", 0) or 0
    return (time.time() - last) >= minutes * 60


def auto_allowed() -> bool:
    return bool(config.get("auto_switch", True)) and in_auto_window() and cooldown_ok()


def switch_to(target: str, reason: str, quiet: bool = False) -> bool:
    with lock:
        current = state["current"]
    if target == current:
        return True

    if config.get("dry_run"):
        if not quiet:
            log(f"[DRY-RUN] 将 {current} → {target}（{reason}）", "warn")
    else:
        try:
            cf_patch_content(target)
            if not quiet:
                log(f"已切换 {current} → {target}（{reason}）", "warn")
        except Exception as e:  # noqa: BLE001
            with lock:
                state["cf_error"] = str(e)
            log(f"切换失败：{e}", "error")
            return False

    with lock:
        state["current"] = target
        state["cf_error"] = ""
        state["last_switch_ts"] = time.time()
    save_state()
    return True


def node_weight(target: str) -> int:
    for n in config.get("nodes") or []:
        if n["target"] == target:
            try:
                return max(1, int(n.get("weight", 1)))
            except (TypeError, ValueError):
                return 1
    return 1


def node_enabled(target: str) -> bool:
    """节点是否参与自动切换/轮换。"""
    for n in config.get("nodes") or []:
        if n["target"] == target:
            return bool(n.get("enabled", True))
    return True


def weighted_next(healthy: list) -> str:
    """平滑加权轮询：healthy = [(target, weight)]，返回下一个目标。"""
    with lock:
        acc = state.setdefault("weighted", {}).setdefault("acc", {})
        total = sum(w for _, w in healthy)
        for t, w in healthy:
            acc[t] = acc.get(t, 0) + w
        best = max(((t, acc.get(t, 0)) for t, _ in healthy), key=lambda x: x[1])
        acc[best[0]] -= total
        return best[0]


# ---------------------------------------------------------------- round_robin

def sync_round_robin(results: dict) -> None:
    with lock:
        records = state.setdefault("records", {})
        snapshot = {k: dict(v) for k, v in records.items()}

    healthy_count = sum(1 for r in results.values() if r.get("healthy") and r.get("ip"))
    ops = []
    for target, res in results.items():
        ip = res.get("ip")
        healthy = bool(res.get("healthy"))
        rec = snapshot.get(target)
        if healthy and ip:
            if not rec:
                ops.append(("create", target, ip))
            elif rec.get("ip") != ip:
                ops.append(("update", target, ip, rec.get("id")))
        elif rec and healthy_count > 0:
            ops.append(("delete", target, rec.get("id")))

    for op in ops:
        kind, target = op[0], op[1]
        try:
            if kind == "create":
                ip = op[2]
                if config.get("dry_run"):
                    rid = f"dry-{abs(hash(target)) % 100000}"
                    log(f"[DRY-RUN] 创建 A 记录 {config['cloudflare']['record_name']} -> {ip}（{target}）", "warn")
                else:
                    rid = cf_create_record("A", ip)
                    log(f"创建 A 记录 -> {ip}（{target}）", "warn")
                with lock:
                    records[target] = {"id": rid, "ip": ip, "simulated": bool(config.get("dry_run"))}
            elif kind == "update":
                ip, rid = op[2], op[3]
                if config.get("dry_run"):
                    log(f"[DRY-RUN] 更新 A 记录 {target} -> {ip}", "warn")
                else:
                    cf_update_record(rid, ip)
                    log(f"更新 A 记录 {target} -> {ip}", "warn")
                with lock:
                    records[target] = {"id": rid, "ip": ip, "simulated": bool(config.get("dry_run"))}
            elif kind == "delete":
                rid = op[2]
                if config.get("dry_run"):
                    log(f"[DRY-RUN] 删除 A 记录（{target}）", "warn")
                else:
                    cf_delete_record(rid)
                    log(f"删除 A 记录（{target}）", "warn")
                with lock:
                    records.pop(target, None)
        except Exception as e:  # noqa: BLE001
            with lock:
                state["cf_error"] = str(e)
            log(f"A 记录同步失败（{target}）：{e}", "error")


def node_sync_status(target: str, healthy, ip) -> str:
    with lock:
        rec = (state.get("records") or {}).get(target)
    if healthy is None:
        return "-"
    if healthy and ip:
        if not rec:
            return "待创建"
        if rec.get("ip") != ip:
            return "待更新"
        return "已同步"
    if rec:
        return "待删除"
    return "已摘除"


def node_verdict(api_status, tcp_status, icmp_ever_ok=False) -> str:
    """
    按国内 ping（xxapi）的结果给结论：
      - 成功 -> 正常
      - 失败 -> 被墙
      - 接口异常时，才回退看 TCP
    """
    if api_status == "ok":
        return "正常"
    if api_status == "fail":
        return "被墙"
    if tcp_status == "ok":
        return "正常"
    if tcp_status == "fail":
        return "异常"
    return "未知"


# ---------------------------------------------------------------- 主循环

def probe_round() -> None:
    nodes_cfg = list(config.get("nodes") or [])
    if not nodes_cfg:
        return

    results = {}
    for n in nodes_cfg:
        target = n["target"]
        ip = resolve_ip(target)
        api = probe_api(target)
        tcp = probe_tcp(target, int(n.get("port", 443)))
        nst = {"api": api, "tcp": tcp, "ip": ip}
        nst["verdict"] = node_verdict(api.get("status"), tcp.get("status"))
        nst["healthy"] = nst["verdict"] == "正常"
        results[target] = nst

    switch_target = None
    switch_reason = ""

    with lock:
        for target, r in results.items():
            nst = state["nodes"].setdefault(target, {"fail": 0, "succ": 0, "healthy": True})
            nst.update({
                "api": r["api"], "tcp": r["tcp"], "ip": r["ip"],
                "healthy": r["healthy"], "last_check": now_str(),
            })
            if r["healthy"]:
                nst["fail"] = 0
                nst["succ"] = nst.get("succ", 0) + 1
            else:
                nst["fail"] = nst.get("fail", 0) + 1
                nst["succ"] = 0
            if r["api"].get("status") == "ok":
                nst["icmp_ever_ok"] = True
        state["last_round"] = now_str()

        mode = config.get("mode", "failover")
        threshold = int(config["probe"].get("fail_threshold", 2))
        current = state["current"]

        if not config.get("auto_switch", True) and current in state["nodes"]:
            cst = state["nodes"][current]
            if (results.get(current, {}).get("verdict") != "正常") and cst.get("fail", 0) == threshold:
                log(f"节点 {current} 异常，但自动切换已关闭，请手动处理", "warn")

        if mode == "failover" and auto_allowed():
            if current in state["nodes"]:
                cst = state["nodes"][current]
                if (results.get(current, {}).get("verdict") != "正常") and cst["fail"] >= threshold:
                    candidates = [t for t in results if t != current and results[t]["verdict"] == "正常" and node_enabled(t)]
                    if candidates:
                        order = [n["target"] for n in (config.get("nodes") or [])]
                        candidates.sort(key=lambda t: order.index(t) if t in order else 999)
                        switch_target = candidates[0]
                        switch_reason = f"{current} 连续 {cst['fail']} 次异常"
                    else:
                        log("所有节点均不健康，保持当前解析不变", "error")

        elif mode == "weighted" and auto_allowed():
            healthy = [(t, node_weight(t)) for t in results if results[t]["verdict"] == "正常" and node_enabled(t)]
            if not healthy:
                log("所有节点均不健康，保持当前解析不变", "error")
            else:
                cur_ok = results.get(current, {}).get("verdict") == "正常"
                cur_fail = state["nodes"].get(current, {}).get("fail", 0)
                if (not cur_ok) and cur_fail >= threshold:
                    # 当前节点挂了：立即切到权重最高的健康节点
                    switch_target = max(healthy, key=lambda x: x[1])[0]
                    switch_reason = f"{current} 连续 {cur_fail} 次异常"
                else:
                    # 正常：到时间片就按权重轮换
                    slice_sec = max(30, int(config.get("scheduler", {}).get("slice_seconds", 300)))
                    now_ts = time.time()
                    wst = state.setdefault("weighted", {"acc": {}, "last_slice": 0})
                    if now_ts - wst.get("last_slice", 0) >= slice_sec and len(healthy) > 1:
                        nxt = weighted_next(healthy)
                        wst["last_slice"] = now_ts
                        if nxt != current and results.get(nxt, {}).get("verdict") == "正常":
                            switch_target = nxt
                            switch_reason = "按权重轮换"

        state["_pending"] = switch_target
        state["_pending_reason"] = switch_reason

    pending = None
    with lock:
        pending = state.pop("_pending", None)
        reason = state.pop("_pending_reason", "")

    if config.get("mode") == "round_robin":
        sync_round_robin(results)
        save_state()
    elif pending:
        switch_to(pending, reason)
    else:
        save_state()


def probe_loop() -> None:
    while True:
        try:
            probe_round()
        except Exception as e:  # noqa: BLE001
            log(f"探测循环异常：{e}", "error")
        interval = max(10, int(config["probe"].get("interval_seconds", 60)))
        time.sleep(interval)


# ---------------------------------------------------------------- Web

def _logged_in() -> bool:
    return bool(session.get("auth"))


@app.before_request
def _auth_hook():
    path = request.path
    if path in ("/login", "/logout") or path.startswith("/static/"):
        return None
    if not _logged_in():
        if path.startswith("/api/"):
            return jsonify({"ok": False, "msg": "未登录", "login": True}), 401
        return redirect("/login")
    return None


@app.get("/login")
def login_page():
    if _logged_in():
        return redirect("/")
    return Response(LOGIN_PAGE, mimetype="text/html; charset=utf-8")


@app.post("/login")
def login_post():
    data = request.get_json(silent=True) or {}
    pwd = (data.get("password") or "").strip()
    cur = (config.get("web") or {}).get("password", "")
    if pwd and pwd == cur:
        session["auth"] = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "msg": "密码错误"}), 401


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.get("/")
def index():
    return Response(HTML_PAGE, mimetype="text/html; charset=utf-8")


def public_config() -> dict:
    cf = config["cloudflare"]
    return {
        "record_name": cf.get("record_name", ""),
        "record_type": cf.get("record_type", "CNAME"),
        "zone_id": cf.get("zone_id", ""),
        "api_token_masked": masked_token(),
        "has_token": bool(cf.get("api_token")),
        "interval_seconds": config["probe"].get("interval_seconds", 60),
        "timeout_seconds": config["probe"].get("timeout_seconds", 10),
        "fail_threshold": config["probe"].get("fail_threshold", 2),
        "decision_mode": config["probe"].get("decision_mode", "smart"),
        "api_url": config["probe"].get("api_url", ""),
        "record_ttl": config["probe"].get("record_ttl", 60),
        "slice_seconds": config.get("scheduler", {}).get("slice_seconds", 300),
        "mode": config.get("mode", "failover"),
        "dry_run": bool(config.get("dry_run")),
        "auto_switch": bool(config.get("auto_switch")),
        "auto_window": config.get("auto_window") or {"enabled": False, "start": "00:00", "end": "23:59"},
        "cooldown_minutes": int(config.get("cooldown_minutes", 0) or 0),
        "has_password": bool((config.get("web") or {}).get("password")),
    }


@app.get("/api/status")
def api_status():
    mode = config.get("mode", "failover")
    with lock:
        nodes = []
        for n in config.get("nodes") or []:
            nst = state["nodes"].get(n["target"], {})
            nodes.append({
                "name": n.get("name", n["target"]),
                "target": n["target"],
                "port": n.get("port", 443),
                "weight": n.get("weight", 1),
                "enabled": bool(n.get("enabled", True)),
                "api": nst.get("api", {}),
                "tcp": nst.get("tcp", {}),
                "ip": nst.get("ip"),
                "healthy": nst.get("healthy"),
                "fail": nst.get("fail", 0),
                "last_check": nst.get("last_check", ""),
                "current": n["target"] == state["current"],
                "sync": node_sync_status(n["target"], nst.get("healthy"), nst.get("ip")),
                "verdict": node_verdict(
                    nst.get("api", {}).get("status"),
                    nst.get("tcp", {}).get("status"),
                    bool(nst.get("icmp_ever_ok")),
                ),
            })
        healthy_n = sum(1 for n in nodes if n["healthy"] is True)
        current = state["current"]
        if mode == "round_robin":
            current = f"轮询 {healthy_n} 个节点"
        return jsonify({
            "mode": mode,
            "current": current,
            "dry_run": bool(config.get("dry_run")),
            "auto_switch": bool(config.get("auto_switch")),
            "decision_mode": config["probe"].get("decision_mode", "smart"),
            "interval": config["probe"].get("interval_seconds", 60),
            "threshold": config["probe"].get("fail_threshold", 2),
            "record_name": config["cloudflare"].get("record_name", ""),
            "has_token": bool(config["cloudflare"].get("api_token")),
            "record_type": config["cloudflare"].get("record_type", "CNAME"),
            "cf_error": state.get("cf_error", ""),
            "last_round": state.get("last_round", ""),
            "nodes": nodes,
            "logs": state["logs"][:50],
            "config": public_config(),
        })


@app.post("/api/probe")
def api_probe():
    threading.Thread(target=probe_round, daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/switch")
def api_switch():
    if config.get("mode") == "round_robin":
        return jsonify({"ok": False, "msg": "round_robin 模式无需手动切换"}), 400
    data = request.get_json(silent=True) or {}
    target = (data.get("target") or "").strip()
    if not any(n["target"] == target for n in (config.get("nodes") or [])):
        return jsonify({"ok": False, "msg": "未知节点"}), 400
    ok = switch_to(target, "手动切换")
    return jsonify({"ok": ok, "current": state["current"]})


@app.post("/api/auto")
def api_auto():
    data = request.get_json(silent=True) or {}
    with lock:
        config["auto_switch"] = bool(data.get("enabled"))
    save_config()
    log(f"自动切换已{'开启' if config['auto_switch'] else '关闭'}")
    return jsonify({"ok": True, "auto_switch": config["auto_switch"]})


@app.get("/api/config")
def api_get_config():
    return jsonify(public_config())


@app.post("/api/config")
def api_set_config():
    data = request.get_json(silent=True) or {}
    cf = config["cloudflare"]
    probe = config["probe"]

    if data.get("api_token") and data["api_token"] not in ("***", "已配置"):
        cf["api_token"] = data["api_token"].strip()
    if data.get("zone_id") is not None:
        cf["zone_id"] = data["zone_id"].strip()
    if data.get("record_name"):
        cf["record_name"] = data["record_name"].strip()
    if data.get("record_type") in ("CNAME", "A"):
        cf["record_type"] = data["record_type"]

    for key in ("interval_seconds", "fail_threshold", "record_ttl", "timeout_seconds"):
        if key in data:
            try:
                val = int(data[key])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "msg": f"{key} 非法"}), 400
            if key == "interval_seconds":
                probe[key] = max(10, val)
            elif key == "fail_threshold":
                probe[key] = max(1, min(10, val))
            elif key == "record_ttl":
                probe[key] = max(60, val)
            else:
                probe[key] = max(3, val)

    if data.get("decision_mode") in ("smart", "api_only", "tcp_only"):
        probe["decision_mode"] = data["decision_mode"]
    if "api_url" in data:
        url = (data.get("api_url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return jsonify({"ok": False, "msg": "探测接口必须是 http/https 地址"}), 400
        if "{target}" not in url:
            return jsonify({"ok": False, "msg": "探测接口必须包含 {target} 占位符"}), 400
        probe["api_url"] = url
    if data.get("mode") in ("failover", "round_robin", "weighted"):
        if data["mode"] == "round_robin" and cf.get("record_type") == "CNAME":
            return jsonify({
                "ok": False,
                "msg": "round_robin 需要主域名使用 A 记录；CNAME 主域名请选 failover 或 weighted",
            }), 400
        config["mode"] = data["mode"]
    if "slice_seconds" in data:
        try:
            config.setdefault("scheduler", {})["slice_seconds"] = max(30, int(data["slice_seconds"]))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "msg": "slice_seconds 非法"}), 400
    if "auto_switch" in data:
        config["auto_switch"] = bool(data["auto_switch"])
    if "dry_run" in data:
        config["dry_run"] = bool(data["dry_run"])
    if "auto_window" in data and isinstance(data["auto_window"], dict):
        aw = config.setdefault("auto_window", {"enabled": False, "start": "00:00", "end": "23:59"})
        if "enabled" in data["auto_window"]:
            aw["enabled"] = bool(data["auto_window"]["enabled"])
        for k in ("start", "end"):
            if data["auto_window"].get(k):
                aw[k] = str(data["auto_window"][k])[:5]
    if "cooldown_minutes" in data:
        try:
            config["cooldown_minutes"] = max(0, min(1440, int(data["cooldown_minutes"])))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "msg": "cooldown_minutes 非法"}), 400

    save_config()
    log("配置已更新")
    return jsonify({"ok": True, "config": public_config()})


@app.post("/api/password")
def api_password():
    data = request.get_json(silent=True) or {}
    old = (data.get("old") or "").strip()
    new = (data.get("new") or "").strip()
    cur = (config.get("web") or {}).get("password", "")
    if cur and old != cur:
        return jsonify({"ok": False, "msg": "当前密码不正确"}), 400
    if len(new) < 6:
        return jsonify({"ok": False, "msg": "新密码至少 6 位"}), 400
    config.setdefault("web", {})["password"] = new
    save_config()
    log("面板密码已更新")
    return jsonify({"ok": True})


@app.post("/api/test_cf")
def api_test_cf():
    try:
        rtype = config["cloudflare"].get("record_type", "CNAME")
        recs = cf_list_records(rtype) or cf_list_records()
        return jsonify({
            "ok": True,
            "count": len(recs),
            "records": [
                {"type": r.get("type"), "content": r.get("content"), "ttl": r.get("ttl")}
                for r in recs
            ],
        })
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "msg": str(e)})


@app.post("/api/nodes")
def api_add_node():
    data = request.get_json(silent=True) or {}
    target = (data.get("target") or "").strip()
    if not target:
        return jsonify({"ok": False, "msg": "缺少域名"}), 400
    if any(n["target"] == target for n in config["nodes"]):
        return jsonify({"ok": False, "msg": "节点已存在"}), 400
    try:
        port = int(data.get("port") or 443)
        weight = max(1, int(data.get("weight") or 1))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "端口/权重非法"}), 400
    config["nodes"].append({
        "name": (data.get("name") or target).strip(),
        "target": target,
        "port": port,
        "weight": weight,
        "enabled": bool(data.get("enabled", True)),
    })
    save_config()
    log(f"添加节点 {target}（端口 {port}，权重 {weight}）")
    return jsonify({"ok": True})


@app.post("/api/nodes/reorder")
def api_reorder_nodes():
    data = request.get_json(silent=True) or {}
    order = [t for t in (data.get("order") or []) if isinstance(t, str)]
    by_target = {n["target"]: n for n in (config.get("nodes") or [])}
    new_nodes = [by_target[t] for t in order if t in by_target]
    for n in (config.get("nodes") or []):
        if n not in new_nodes:
            new_nodes.append(n)
    config["nodes"] = new_nodes
    save_config()
    log("节点顺序已更新：" + " → ".join(n["target"] for n in new_nodes))
    return jsonify({"ok": True})


@app.delete("/api/nodes")
def api_del_node():
    target = (request.args.get("target") or "").strip()
    config["nodes"] = [n for n in config["nodes"] if n["target"] != target]
    with lock:
        state["nodes"].pop(target, None)
        state.get("records", {}).pop(target, None)
        if state["current"] == target:
            state["current"] = config["nodes"][0]["target"] if config["nodes"] else ""
    save_config()
    log(f"删除节点 {target}")
    return jsonify({"ok": True})


@app.post("/api/nodes/update")
def api_update_node():
    data = request.get_json(silent=True) or {}
    target = (data.get("target") or "").strip()
    for n in config["nodes"]:
        if n["target"] == target:
            if data.get("name"):
                n["name"] = data["name"].strip()
            if "port" in data:
                try:
                    n["port"] = int(data["port"])
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "msg": "端口非法"}), 400
            if "weight" in data:
                try:
                    n["weight"] = max(1, int(data["weight"]))
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "msg": "权重非法"}), 400
            if "enabled" in data:
                n["enabled"] = bool(data["enabled"])
            save_config()
            return jsonify({"ok": True})
    return jsonify({"ok": False, "msg": "节点不存在"}), 404


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CF-GTM · 流量调度控制台</title>
<style>
:root{
  --bg:#0b1220;--panel:#121c2e;--panel2:#0f1828;--line:#22314b;
  --txt:#e6edf7;--dim:#8ba0bf;--ok:#22c55e;--bad:#ef4444;--warn:#f59e0b;--acc:#3b82f6;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);
  font:14px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1240px;margin:0 auto;padding:24px 18px 60px}
header{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;margin-bottom:22px}
h1{font-size:20px;margin:0;letter-spacing:.5px}
h1 span{color:var(--acc)}
.sub{color:var(--dim);font-size:12px;margin-top:2px}
.badge{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;
  font-size:12px;border:1px solid var(--line);background:var(--panel2);color:var(--dim)}
.badge.ok{color:var(--ok);border-color:rgba(34,197,94,.35);background:rgba(34,197,94,.08)}
.badge.bad{color:var(--bad);border-color:rgba(239,68,68,.35);background:rgba(239,68,68,.08)}
.badge.warn{color:var(--warn);border-color:rgba(245,158,11,.35);background:rgba(245,158,11,.08)}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
@media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.card .k{color:var(--dim);font-size:12px;margin-bottom:6px}
.card .v{font-size:18px;font-weight:600;word-break:break-all}
.card .v.small{font-size:14px;font-weight:500}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;margin-bottom:16px;overflow:hidden}
.panel .ph{display:flex;align-items:center;justify-content:space-between;gap:10px;
  padding:13px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.panel .ph b{font-size:14px}
.panel .pb{padding:14px 16px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{color:var(--dim);font-weight:500;text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:10px;border-bottom:1px solid rgba(34,49,75,.55);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr.cur td{background:rgba(59,130,246,.07)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}
.dot.ok{background:var(--ok);box-shadow:0 0 8px rgba(34,197,94,.6)}
.dot.bad{background:var(--bad);box-shadow:0 0 8px rgba(239,68,68,.6)}
.dot.unk{background:var(--warn)}
.mono{font-family:ui-monospace,Consolas,"SFMono-Regular",monospace;font-size:12px;color:var(--dim)}
button{cursor:pointer;border:1px solid var(--line);background:var(--panel2);color:var(--txt);
  border-radius:8px;padding:6px 12px;font-size:12px;transition:.15s}
button:hover{border-color:var(--acc);color:#fff}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff}
button.danger{border-color:rgba(239,68,68,.4);color:var(--bad)}
button.danger:hover{background:rgba(239,68,68,.12)}
button:disabled{opacity:.45;cursor:not-allowed}
.switch{position:relative;display:inline-block;width:40px;height:22px;vertical-align:middle}
.switch.mini{width:34px;height:18px}
.switch.mini .slider:before{width:12px;height:12px;left:3px;top:3px}
.switch.mini input:checked+.slider:before{transform:translateX(16px)}
.switch input{opacity:0;width:0;height:0}
.slider{position:absolute;inset:0;background:#2a3a55;border-radius:22px;transition:.2s;cursor:pointer}
.slider:before{content:"";position:absolute;width:16px;height:16px;left:3px;top:3px;
  background:#fff;border-radius:50%;transition:.2s}
input:checked+.slider{background:var(--ok)}
input:checked+.slider:before{transform:translateX(18px)}
.logs{max-height:300px;overflow:auto;font-family:ui-monospace,Consolas,monospace;font-size:12px}
.log{display:flex;gap:10px;padding:5px 0;border-bottom:1px dashed rgba(34,49,75,.6)}
.log .t{color:var(--dim);white-space:nowrap}
.log .m.info{color:#c8d6ea}
.log .m.warn{color:var(--warn)}
.log .m.error{color:var(--bad)}
footer{color:var(--dim);font-size:12px;text-align:center;margin-top:30px}
footer a,.sub a{color:var(--acc);text-decoration:none}
footer a:hover,.sub a:hover{text-decoration:underline}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;z-index:100;padding:16px}
.modal-card{background:var(--panel);border:1px solid var(--line);border-radius:14px;
  width:min(680px,94vw);max-height:90vh;overflow:auto;overflow-x:hidden;box-shadow:0 20px 60px rgba(0,0,0,.5)}
.modal-head{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid var(--line)}
.modal-body{padding:16px 18px;overflow-x:hidden}
.modal .field{min-width:0}
.modal .field input,.modal .field select{width:100%}
.modal .field label{white-space:normal;line-height:1.45}
@media(max-width:720px){
  .modal-card{width:96vw;border-radius:12px}
  .modal-head{padding:12px 14px}
  .modal-body{padding:14px}
  .modal .grid2,.modal .grid3{grid-template-columns:1fr}
  .wrap{padding:16px 12px 40px}
  header h1{font-size:18px}
}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
td.toolbar{display:table-cell;white-space:nowrap;vertical-align:middle}
td.toolbar button,td.toolbar .badge{height:26px;display:inline-flex;align-items:center;justify-content:center;
  padding:0 9px;line-height:1;vertical-align:middle;margin-right:4px}
td.toolbar button:last-child{margin-right:0}
input,select{background:var(--panel2);border:1px solid var(--line);color:var(--txt);
  border-radius:8px;padding:7px 10px;font-size:13px;outline:none}
input:focus,select:focus{border-color:var(--acc)}
input.small{width:80px}
input.mid{width:180px}
.grid2{display:grid;grid-template-columns:repeat(2,1fr);gap:14px 20px}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px 20px}
@media(max-width:900px){.grid2,.grid3{grid-template-columns:1fr}}
.field{display:flex;flex-direction:column;gap:6px}
.field label{color:var(--dim);font-size:12px}
.hint{color:var(--dim);font-size:12px;margin-top:6px}
.msg{font-size:12px;margin-left:8px}
.msg.ok{color:var(--ok)}
.msg.bad{color:var(--bad)}
.divider{height:1px;background:var(--line);margin:16px 0}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>CF<span>-GTM</span> · 流量调度控制台</h1>
      <div class="sub">Cloudflare DNS 自动故障切换 · 探测源：国内 ICMP + 本地 TCP · <a href="https://t.me/taohuayuang" target="_blank" rel="noopener">桃花酱出品 @taohuayuang</a></div>
    </div>
    <div class="toolbar">
      <span id="modeBadge" class="badge">-</span>
      <span id="dryBadge" class="badge warn" style="display:none">DRY-RUN 演练</span>
      <span id="cfBadge" class="badge">CF 未连接</span>
      <span class="badge">自动切换</span>
      <label class="switch">
        <input type="checkbox" id="autoSwitch" onchange="toggleAuto(this)">
        <span class="slider"></span>
      </label>
      <button onclick="openDomainModal()">主域名</button>
      <button onclick="openProbeModal()">探测设置</button>
      <button onclick="openNodesModal()">节点管理</button>
      <button onclick="openAutoModal()">自动切换设置</button>
      <button onclick="openPwdModal()">安全</button>
      <button class="primary" onclick="probeNow()">立即探测</button>
    </div>
  </header>

  <div class="cards">
    <div class="card"><div class="k">当前解析</div><div class="v" id="curTarget">-</div></div>
    <div class="card"><div class="k">服务状态</div><div class="v" id="svcState">-</div></div>
    <div class="card"><div class="k">探测频率 / 失败阈值</div><div class="v small" id="probeCfg">-</div></div>
    <div class="card"><div class="k">最近一轮探测</div><div class="v small" id="lastRound">-</div></div>
  </div>

  <!-- 探测设置弹窗 -->
  <div id="probeModal" class="modal" style="display:none">
    <div class="modal-card">
      <div class="modal-head"><b>探测与调度设置</b><button onclick="closeProbeModal()">关闭</button></div>
      <div class="modal-body">
        <div class="grid2">
          <div class="field"><label>探测频率（秒）</label><input id="cfgInterval" type="number" min="10"></div>
          <div class="field"><label>失败阈值（连续 N 次切换）</label><input id="cfgThreshold" type="number" min="1"></div>
        </div>
        <div class="grid2" style="margin-top:14px">
          <div class="field"><label>调度模式</label>
            <select id="cfgMode">
              <option value="failover">failover · 主备切换</option>
              <option value="weighted">weighted · 按权重轮换</option>
              <option value="round_robin" id="rrOption">round_robin · 多 A 记录轮询（需 A 记录）</option>
            </select>
          </div>
          <div class="field"><label>判定模式</label>
            <select id="cfgDecision">
              <option value="smart">smart（TCP 优先）</option>
              <option value="api_only">api_only（只看国内 ping）</option>
              <option value="tcp_only">tcp_only（只看 TCP）</option>
            </select>
          </div>
        </div>
        <div class="field" style="margin-top:14px">
          <label>探测接口（国内）</label>
          <input id="cfgApiUrl" placeholder="https://v2.xxapi.cn/api/ping?url={target}">
        </div>
        <div class="hint" style="margin-top:6px">默认用 xxapi 第三方接口。换成自己的国内服务器时，填自己的地址，用 <b>{target}</b> 占位，例如 <span class="mono">http://你的IP:8080/ping?host={target}</span>；接口返回 JSON 里 <span class="mono">code=200</span> 表示正常、<span class="mono">code=-2</span> 表示失败。</div>
        <div class="toolbar" style="margin-top:16px">
          <button class="primary" onclick="saveConfig()">保存设置</button>
          <span id="cfgMsg" class="msg"></span>
        </div>
        <div class="hint">CNAME 主域名只能用 failover / weighted；round_robin 需要主域名改成 A 记录。weighted 模式下权重相同 = 严格轮流，权重不同 = 按比例轮换，不是随机。</div>
      </div>
    </div>
  </div>

  <!-- 节点管理弹窗 -->
  <div id="nodesModal" class="modal" style="display:none">
    <div class="modal-card">
      <div class="modal-head"><b>节点管理</b><button onclick="closeNodesModal()">关闭</button></div>
      <div class="modal-body">
        <div class="grid3">
          <div class="field"><label>名称</label><input id="nodeName" placeholder="日本A"></div>
          <div class="field"><label>DDNS 域名 / IP</label><input id="nodeTarget" placeholder="jp1.ddns.net"></div>
          <div class="field"><label>探测端口</label><input id="nodePort" class="small" type="number" value="443"></div>
        </div>
        <div class="toolbar" style="margin-top:12px">
          <div class="field"><label>权重</label>
            <select id="nodeWeight">
              <option value="4">最高</option>
              <option value="3">高</option>
              <option value="2">中</option>
              <option value="1" selected>低</option>
            </select>
          </div>
          <div class="field"><label>自动切换</label>
            <label style="display:flex;align-items:center;gap:6px;font-size:13px;padding-top:6px">
              <input type="checkbox" id="nodeEnabled" checked style="width:auto"> 参与
            </label>
          </div>
          <button class="primary" onclick="addNode()">添加节点</button>
          <span id="nodeMsg" class="msg"></span>
        </div>
        <div class="hint">权重只在 weighted 模式下生效（最高/高/中/低 = 4/3/2/1），failover 模式忽略权重。权重仅保存在本机，不会写入 Cloudflare。</div>
      </div>
    </div>
  </div>

  <!-- 自动切换弹窗 -->
  <div id="autoModal" class="modal" style="display:none">
    <div class="modal-card">
      <div class="modal-head"><b>自动切换设置</b><button onclick="closeAutoModal()">关闭</button></div>
      <div class="modal-body">
        <div class="field"><label>自动切换时间段</label>
          <label style="display:flex;align-items:center;gap:8px;color:var(--txt);font-size:13px">
            <input type="checkbox" id="autoWindowEnabled" style="width:auto"> 启用生效时间段限制
          </label>
        </div>
        <div class="grid2" style="margin-top:14px">
          <div class="field"><label>生效时间段（开始）</label><input id="autoStart" type="time" value="00:00"></div>
          <div class="field"><label>生效时间段（结束）</label><input id="autoEnd" type="time" value="23:59"></div>
        </div>
        <div class="field" style="margin-top:14px">
          <label>切换冷却（分钟，0 表示不限制）</label>
          <input id="autoCooldown" type="number" min="0" value="0">
        </div>
        <div class="field" style="margin-top:14px">
          <label>自动切换时间（秒）</label>
          <input id="cfgSlice" type="number" min="30">
        </div>
        <div class="hint">weighted 模式下每隔这个时间按权重轮换一次解析（默认 300 秒 = 5 分钟；想 60 分钟换一次就填 3600）；failover 模式只在故障时切换，不受此限制。</div>
        <div class="toolbar" style="margin-top:16px">
          <button class="primary" onclick="saveAuto()">保存</button>
          <span id="autoMsg" class="msg"></span>
        </div>
        <div class="hint">生效时间段支持跨天（如 23:00-07:00）；冷却期内不会再次自动切换，防止抖动来回切。</div>
      </div>
    </div>
  </div>

  <!-- 主域名设置弹窗 -->
  <div id="domainModal" class="modal" style="display:none">
    <div class="modal-card">
      <div class="modal-head">
        <b>主域名设置</b>
        <button onclick="closeDomainModal()">关闭</button>
      </div>
      <div class="modal-body">
        <div class="grid2">
          <div class="field"><label>主域名（固定记录）</label><input id="cfgRecord" placeholder="main.example.com"></div>
          <div class="field"><label>记录类型</label>
            <select id="cfgType" onchange="syncModeOptions()"><option value="CNAME">CNAME</option><option value="A">A</option></select>
          </div>
        </div>
        <div class="field" style="margin-top:14px"><label>Zone ID</label><input id="cfgZone" placeholder="32 位 Zone ID"></div>
        <div class="field" style="margin-top:14px">
          <label>A 记录 TTL（秒，仅 round_robin 模式使用）</label>
          <input id="cfgTtl" type="number" min="60">
        </div>
        <div class="field" style="margin-top:14px">
          <label>API Token（留空表示不修改）</label>
          <input id="cfgToken" type="password" placeholder="已配置则留空">
        </div>
        <div class="toolbar" style="margin-top:16px">
          <button class="primary" onclick="saveDomain()">保存主域名</button>
          <button onclick="testCF()">测试 CF 连接</button>
          <span id="domainMsg" class="msg"></span>
        </div>
        <div class="hint">这里不常改。Token 只保存在本机 config.json，不会外发。</div>
      </div>
    </div>
  </div>

  <!-- 安全设置弹窗 -->
  <div id="pwdModal" class="modal" style="display:none">
    <div class="modal-card">
      <div class="modal-head"><b>面板安全</b><button onclick="closePwdModal()">关闭</button></div>
      <div class="modal-body">
        <div class="hint" style="margin-bottom:12px">面板已启用 HTTP Basic Auth，用户名固定 admin。改完密码后浏览器会重新要求登录。</div>
        <div class="field"><label>当前密码</label><input id="pwdOld" type="password" placeholder="首次设置可留空"></div>
        <div class="field" style="margin-top:12px"><label>新密码（至少 6 位）</label><input id="pwdNew" type="password"></div>
        <div class="field" style="margin-top:12px"><label>确认新密码</label><input id="pwdConfirm" type="password"></div>
        <div class="toolbar" style="margin-top:16px">
          <button class="primary" onclick="savePassword()">保存密码</button>
          <a href="/logout" style="color:var(--bad);font-size:12px;text-decoration:none;margin-left:6px">退出登录</a>
          <span id="pwdMsg" class="msg"></span>
        </div>
        <div class="hint">Basic Auth 是明文传输，公网部署建议再加一层 HTTPS（Nginx / Caddy），或者只允许内网访问。</div>
      </div>
    </div>
  </div>

  <!-- 状态 -->
  <div class="panel">
    <div class="ph"><b>节点健康状态</b><span class="mono" id="modeHint"></span></div>
    <div class="pb" style="padding:0;overflow-x:auto">
      <table>
        <thead><tr>
          <th>节点</th><th>DDNS 域名</th><th>解析 IP</th><th>权重</th><th>判断</th><th>自动切换</th><th>同步/状态</th><th></th>
        </tr></thead>
        <tbody id="nodeBody"><tr><td colspan="8" style="text-align:center;color:var(--dim)">加载中…</td></tr></tbody>
      </table>
    </div>
  </div>

  <div class="panel">
    <div class="ph"><b>操作日志</b><span class="mono" id="logCount"></span></div>
    <div class="pb logs" id="logBody"></div>
  </div>

  <footer>CF-GTM · 桃花酱出品 <a href="https://t.me/taohuayuang" target="_blank" rel="noopener">@taohuayuang</a> · 单文件 Flask 应用 · 状态仅保存在本机</footer>
</div>

<script>
function esc(s){ return String(s==null?"":s).replace(/[&<>"']/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c])); }
function dot(st){ const cls = st==="ok" ? "ok" : st==="fail" ? "bad" : "unk"; return `<span class="dot ${cls}"></span>`; }
function badgeVerdict(v){
  const ok = v==="正常";
  const cls = ok ? "ok" : (v==="被墙" || v==="异常") ? "bad" : "";
  return `<span class="badge ${cls}">${esc(v || "未知")}</span>`;
}
function fmtLat(x){ return (x===null||x===undefined) ? "-" : x+"ms"; }
async function jget(u){
  const r = await fetch(u);
  if(r.status === 401){ location.href = "/login"; throw new Error("unauthorized"); }
  if(!r.ok) throw new Error("HTTP "+r.status);
  return r.json();
}
async function jpost(u,b){
  const r = await fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b||{})});
  if(r.status === 401){ location.href = "/login"; throw new Error("unauthorized"); }
  return r.json();
}
async function jdel(u){ const r=await fetch(u,{method:"DELETE"}); return r.json(); }

const MODE_LABEL = {failover:"主备切换", round_robin:"多 A 记录轮询", weighted:"按权重轮换"};
const SYNC_CLS = {"已同步":"ok","待创建":"warn","待更新":"warn","待删除":"bad","已摘除":"bad"};

function fillConfig(c){
  document.getElementById("cfgRecord").value = c.record_name || "";
  document.getElementById("cfgType").value = c.record_type || "CNAME";
  document.getElementById("cfgZone").value = c.zone_id || "";
  document.getElementById("cfgInterval").value = c.interval_seconds;
  document.getElementById("cfgThreshold").value = c.fail_threshold;
  document.getElementById("cfgMode").value = c.mode;
  document.getElementById("cfgDecision").value = c.decision_mode;
  document.getElementById("cfgSlice").value = c.slice_seconds;
  document.getElementById("cfgTtl").value = c.record_ttl;
  document.getElementById("cfgToken").placeholder = c.has_token ? "已配置（留空不修改）" : "未配置";
  document.getElementById("cfgApiUrl").value = c.api_url || "";
  document.getElementById("autoWindowEnabled").checked = !!(c.auto_window && c.auto_window.enabled);
  document.getElementById("autoStart").value = (c.auto_window && c.auto_window.start) || "00:00";
  document.getElementById("autoEnd").value = (c.auto_window && c.auto_window.end) || "23:59";
  document.getElementById("autoCooldown").value = c.cooldown_minutes || 0;
  syncModeOptions();
}

function syncModeOptions(){
  const type = document.getElementById("cfgType").value;
  const rr = document.getElementById("rrOption");
  const modeSel = document.getElementById("cfgMode");
  rr.disabled = (type === "CNAME");
  if(type === "CNAME" && modeSel.value === "round_robin") modeSel.value = "weighted";
}

let lastConfig = null;
async function loadConfig(){
  try{ lastConfig = await jget("/api/config"); fillConfig(lastConfig); }catch(e){}
}

const WEIGHT_OPTS = [[4,"最高"],[3,"高"],[2,"中"],[1,"低"]];
function weightSelect(w, target){
  const opts = WEIGHT_OPTS.map(([v,label])=>`<option value="${v}" ${Number(w)===v?"selected":""}>${label}</option>`).join("");
  return `<select onchange="saveWeight('${esc(target)}', this)">${opts}</select>`;
}

function openProbeModal(){ if(lastConfig) fillConfig(lastConfig); document.getElementById("probeModal").style.display="flex"; }
function closeProbeModal(){ document.getElementById("probeModal").style.display="none"; }
function openNodesModal(){ document.getElementById("nodesModal").style.display="flex"; }
function closeNodesModal(){ document.getElementById("nodesModal").style.display="none"; }
function openAutoModal(){ if(lastConfig) fillConfig(lastConfig); document.getElementById("autoModal").style.display="flex"; }
function closeAutoModal(){ document.getElementById("autoModal").style.display="none"; }
function openPwdModal(){ document.getElementById("pwdModal").style.display="flex"; }
function closePwdModal(){ document.getElementById("pwdModal").style.display="none"; }

async function savePassword(){
  const msg = document.getElementById("pwdMsg");
  const oldp = document.getElementById("pwdOld").value;
  const newp = document.getElementById("pwdNew").value;
  const conf = document.getElementById("pwdConfirm").value;
  if(newp !== conf){ msg.className="msg bad"; msg.textContent="两次输入不一致"; return; }
  const r = await jpost("/api/password", {old: oldp, new: newp});
  msg.className = "msg " + (r.ok ? "ok" : "bad");
  msg.textContent = r.ok ? "已更新，请刷新页面重新登录" : (r.msg || "失败");
}

async function saveAuto(){
  const msg = document.getElementById("autoMsg");
  const payload = {
    auto_window: {
      enabled: document.getElementById("autoWindowEnabled").checked,
      start: document.getElementById("autoStart").value || "00:00",
      end: document.getElementById("autoEnd").value || "23:59",
    },
    cooldown_minutes: parseInt(document.getElementById("autoCooldown").value||"0"),
    slice_seconds: parseInt(document.getElementById("cfgSlice").value||"300"),
  };
  const r = await jpost("/api/config", payload);
  msg.className = "msg " + (r.ok ? "ok" : "bad");
  msg.textContent = r.ok ? "已保存" : (r.msg || "保存失败");
  if(r.ok){ lastConfig = r.config; fillConfig(r.config); refresh(); }
  setTimeout(()=>{ msg.textContent=""; }, 4000);
}

async function saveConfig(){
  const msg = document.getElementById("cfgMsg");
  const payload = {
    interval_seconds: parseInt(document.getElementById("cfgInterval").value||"60"),
    fail_threshold: parseInt(document.getElementById("cfgThreshold").value||"2"),
    mode: document.getElementById("cfgMode").value,
    decision_mode: document.getElementById("cfgDecision").value,
    api_url: document.getElementById("cfgApiUrl").value.trim(),
  };
  const r = await jpost("/api/config", payload);
  msg.className = "msg " + (r.ok ? "ok" : "bad");
  msg.textContent = r.ok ? "已保存" : (r.msg || "保存失败");
  if(r.ok){ lastConfig = r.config; fillConfig(r.config); refresh(); }
  setTimeout(()=>{ msg.textContent=""; }, 4000);
}

function openDomainModal(){ document.getElementById("domainModal").style.display = "flex"; }
function closeDomainModal(){ document.getElementById("domainModal").style.display = "none"; }

async function saveDomain(){
  const msg = document.getElementById("domainMsg");
  const payload = {
    record_name: document.getElementById("cfgRecord").value.trim(),
    record_type: document.getElementById("cfgType").value,
    zone_id: document.getElementById("cfgZone").value.trim(),
    api_token: document.getElementById("cfgToken").value.trim(),
    record_ttl: parseInt(document.getElementById("cfgTtl").value||"60"),
  };
  const r = await jpost("/api/config", payload);
  msg.className = "msg " + (r.ok ? "ok" : "bad");
  msg.textContent = r.ok ? "已保存" : (r.msg || "保存失败");
  if(r.ok){
    document.getElementById("cfgToken").value = "";
    lastConfig = r.config;
    fillConfig(r.config);
    refresh();
  }
  setTimeout(()=>{ msg.textContent=""; }, 4000);
}

async function testCF(){
  const msg = document.getElementById("domainMsg");
  msg.className = "msg"; msg.textContent = "测试中…";
  const r = await jpost("/api/test_cf", {});
  if(r.ok){
    msg.className = "msg ok";
    msg.textContent = `连接成功，查到 ${r.count} 条记录：` + (r.records||[]).map(x=>x.type+" "+x.content).join(" / ");
  } else {
    msg.className = "msg bad"; msg.textContent = "失败：" + (r.msg||"");
  }
  setTimeout(()=>{ msg.textContent=""; }, 8000);
}

async function addNode(){
  const msg = document.getElementById("nodeMsg");
  const payload = {
    name: document.getElementById("nodeName").value.trim(),
    target: document.getElementById("nodeTarget").value.trim(),
    port: parseInt(document.getElementById("nodePort").value||"443"),
    weight: parseInt(document.getElementById("nodeWeight").value||"1"),
    enabled: document.getElementById("nodeEnabled").checked,
  };
  if(!payload.target){ msg.className="msg bad"; msg.textContent="请填写域名"; return; }
  const r = await jpost("/api/nodes", payload);
  msg.className = "msg " + (r.ok ? "ok" : "bad");
  msg.textContent = r.ok ? "已添加" : (r.msg || "添加失败");
  if(r.ok){ document.getElementById("nodeName").value=""; document.getElementById("nodeTarget").value=""; refresh(); }
  setTimeout(()=>{ msg.textContent=""; }, 4000);
}

async function delNode(target){
  if(!confirm("确认删除节点 "+target+" ？")) return;
  await jdel("/api/nodes?target="+encodeURIComponent(target));
  refresh();
}

async function saveWeight(target, el){
  const w = parseInt(el.value||"1");
  const r = await jpost("/api/nodes/update", {target: target, weight: w});
  if(!r.ok) alert("保存失败："+(r.msg||""));
  refresh();
}

async function refresh(){
  try{
    const s = await jget("/api/status");
    const rr = s.mode === "round_robin", wd = s.mode === "weighted";
    document.getElementById("modeBadge").textContent = MODE_LABEL[s.mode] || s.mode;
    document.getElementById("modeBadge").className = "badge " + (rr || wd ? "ok" : "");
    document.getElementById("modeHint").textContent =
      wd ? "weighted：按权重分配解析时间片，故障立即切换" :
      rr ? "round_robin：多 A 记录等概率，故障摘除记录" :
      "failover：当前节点故障后切换到健康节点";
    document.getElementById("curTarget").textContent = s.current || "-";
    document.getElementById("probeCfg").textContent = s.interval+"s / "+s.threshold+" 次";
    document.getElementById("lastRound").textContent = s.last_round || "尚未探测";
    document.getElementById("autoSwitch").checked = !!s.auto_switch;
    document.getElementById("dryBadge").style.display = s.dry_run ? "" : "none";

    const cfBadge = document.getElementById("cfBadge");
    if(s.cf_error){ cfBadge.className="badge bad"; cfBadge.textContent="CF 异常"; }
    else if(!s.has_token){ cfBadge.className="badge"; cfBadge.textContent="CF 未配置"; }
    else { cfBadge.className="badge ok"; cfBadge.textContent = s.dry_run ? "CF 已连接 · 演练" : "CF 已连接"; }

    const svc = document.getElementById("svcState");
    if(rr){
      const n = s.nodes.filter(x=>x.healthy===true).length;
      svc.textContent = n > 0 ? (n+" 个节点在线") : "全部异常";
      svc.style.color = n > 0 ? "var(--ok)" : "var(--bad)";
    } else {
      const curNode = s.nodes.find(n=>n.current);
      if(!curNode){
        svc.textContent = s.current ? ("指向 " + s.current) : "未知";
        svc.style.color = "var(--warn)";
      }
      else if(curNode.healthy){ svc.textContent="正常"; svc.style.color="var(--ok)"; }
      else if(curNode.fail >= s.threshold){ svc.textContent="异常（已触发切换）"; svc.style.color="var(--bad)"; }
      else { svc.textContent="异常（等待确认）"; svc.style.color="var(--warn)"; }
    }

    nodesOrder = s.nodes.map(n=>n.target);

    const tb = document.getElementById("nodeBody");
    if(!s.nodes.length){ tb.innerHTML='<tr><td colspan="8" style="text-align:center;color:var(--dim)">还没有节点，先在上方添加</td></tr>'; }
    else {
      tb.innerHTML = s.nodes.map(n=>{
        const api = n.api||{}, tcp = n.tcp||{};
        const lat = tcp.latency || api.latency;
        const syncCls = SYNC_CLS[n.sync] || "";
        return `<tr class="${n.current&&!rr?'cur':''}">
          <td><b>${esc(n.name)}</b></td>
          <td class="mono">${esc(n.target)}:${n.port}</td>
          <td class="mono">${esc(n.ip||"-")}</td>
          <td>${s.mode==="weighted" ? weightSelect(n.weight, n.target) : '<span class="mono">-</span>'}</td>
          <td>${badgeVerdict(n.verdict)}</td>
          <td><label class="switch mini"><input type="checkbox" ${n.enabled ? "checked" : ""} onchange="toggleNodeEnabled('${esc(n.target)}', this)"><span class="slider"></span></label></td>
          <td>${s.mode==="round_robin" && n.sync ? `<span class="badge ${syncCls}">${esc(n.sync)}</span>` : '<span class="mono">-</span>'}</td>
          <td class="toolbar">
            ${n.current&&!rr ? '<span class="badge ok">当前</span>' : (!rr ? `<button onclick="switchTo('${esc(n.target)}')">切到此</button>` : '')}
            <button onclick="moveNode('${esc(n.target)}', -1)" title="上移">↑</button>
            <button onclick="moveNode('${esc(n.target)}', 1)" title="下移">↓</button>
            <button class="danger" onclick="delNode('${esc(n.target)}')">删除</button>
          </td>
        </tr>`;
      }).join("");
    }

    const lb = document.getElementById("logBody");
    lb.innerHTML = (s.logs||[]).map(l=>`<div class="log"><span class="t">${esc(l.ts)}</span><span class="m ${esc(l.level)}">${esc(l.msg)}</span></div>`).join("") || '<div style="color:var(--dim)">暂无日志</div>';
    document.getElementById("logCount").textContent = (s.logs||[]).length + " 条";
  }catch(e){ console.error(e); }
}

async function probeNow(){ await jpost("/api/probe"); setTimeout(refresh, 800); }
async function switchTo(t){
  if(!confirm("确认把主域名切换到 "+t+" ？")) return;
  const r = await jpost("/api/switch", {target:t});
  if(!r.ok) alert("切换失败："+(r.msg||""));
  refresh();
}
async function toggleAuto(el){ await jpost("/api/auto", {enabled: el.checked}); refresh(); }

let nodesOrder = [];
async function moveNode(target, delta){
  const arr = [...nodesOrder];
  const i = arr.indexOf(target), j = i + delta;
  if(i < 0 || j < 0 || j >= arr.length) return;
  [arr[i], arr[j]] = [arr[j], arr[i]];
  const r = await jpost("/api/nodes/reorder", {order: arr});
  if(r.ok) refresh();
}

async function toggleNodeEnabled(target, el){
  const r = await jpost("/api/nodes/update", {target, enabled: el.checked});
  if(!r.ok){ alert("保存失败："+(r.msg||"")); el.checked = !el.checked; }
  refresh();
}

loadConfig();
refresh();
["domainModal","probeModal","nodesModal","autoModal","pwdModal"].forEach(function(id){
  const el = document.getElementById(id);
  el.addEventListener("click", function(e){ if(e.target === this) this.style.display="none"; });
});
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


LOGIN_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CF-GTM · 登录</title>
<style>
:root{--bg:#0b1220;--panel:#121c2e;--panel2:#0f1828;--line:#22314b;
  --txt:#e6edf7;--dim:#8ba0bf;--acc:#3b82f6;--bad:#ef4444}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:radial-gradient(1200px 600px at 50% -10%,#15213a 0%,var(--bg) 60%);
  color:var(--txt);font:14px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.card{width:min(380px,92vw);background:var(--panel);border:1px solid var(--line);border-radius:16px;
  padding:30px 28px;box-shadow:0 24px 70px rgba(0,0,0,.5)}
h1{font-size:21px;margin:0 0 4px;letter-spacing:.5px}
h1 span{color:var(--acc)}
.sub{color:var(--dim);font-size:12px;margin-bottom:22px}
.sub a{color:var(--acc);text-decoration:none}
label{display:block;color:var(--dim);font-size:12px;margin-bottom:8px}
input{width:100%;background:var(--panel2);border:1px solid var(--line);color:var(--txt);
  border-radius:10px;padding:11px 13px;font-size:14px;outline:none}
input:focus{border-color:var(--acc);box-shadow:0 0 0 3px rgba(59,130,246,.12)}
button{width:100%;margin-top:16px;background:var(--acc);border:none;color:#fff;border-radius:10px;
  padding:11px;font-size:14px;cursor:pointer;transition:.15s}
button:hover{filter:brightness(1.1)}
button:active{transform:translateY(1px)}
.err{color:var(--bad);font-size:12px;margin-top:10px;min-height:18px}
footer{color:var(--dim);font-size:11px;text-align:center;margin-top:22px}
</style>
</head>
<body>
<div class="card">
  <h1>CF<span>-GTM</span> · 登录</h1>
  <div class="sub">桃花酱出品 · <a href="https://t.me/taohuayuang" target="_blank" rel="noopener">@taohuayuang</a></div>
  <label>面板密码</label>
  <input id="pwd" type="password" autofocus placeholder="请输入密码" onkeydown="if(event.key==='Enter')doLogin()">
  <button onclick="doLogin()">登 录</button>
  <div class="err" id="err"></div>
  <footer>Cloudflare DNS 流量调度控制台</footer>
</div>
<script>
async function doLogin(){
  const pwd = document.getElementById("pwd").value;
  const err = document.getElementById("err");
  err.textContent = "";
  if(!pwd){ err.textContent = "请输入密码"; return; }
  try{
    const r = await fetch("/login", {method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({password:pwd})});
    const j = await r.json();
    if(j.ok){ location.href = "/"; }
    else { err.textContent = j.msg || "密码错误"; document.getElementById("pwd").select(); }
  }catch(e){ err.textContent = "请求失败，请重试"; }
}
</script>
</body>
</html>
"""

# ---------------------------------------------------------------- 启动

_initialized = False


def init_app() -> None:
    """幂等初始化：python app.py 或 gunicorn 导入 app 时都会执行。"""
    global _initialized
    if _initialized:
        return
    _initialized = True

    load_config()
    app.secret_key = config["web"]["secret"]
    state["started_at"] = now_str()

    log("=" * 56)
    log("CF-GTM 启动")
    log("桃花酱出品 · @taohuayuang")
    log(f"主域名：{config['cloudflare'].get('record_name')} | 模式：{config.get('mode')}")
    log(f"节点数：{len(config.get('nodes') or [])} | 判定：{config['probe'].get('decision_mode')} | DRY-RUN：{config.get('dry_run')}")
    log("=" * 56)

    refresh_from_cf()
    threading.Thread(target=probe_loop, daemon=True).start()

    if (config.get("web") or {}).get("password"):
        log("Web 控制台已启用登录密码")
    else:
        log("Web 控制台未设置密码，建议仅在内网使用", "warn")


# 模块级初始化：gunicorn 导入时也会启动探测线程（gunicorn 请用 workers=1）
init_app()


def main() -> None:
    web = config.get("web") or {}
    host = web.get("host", "0.0.0.0")
    port = int(web.get("port", 8787))

    app.run(host=host, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
