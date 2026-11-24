#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CF-GTM 自建国内探测服务

部署在国内服务器上，接收 /ping?host=xxx&port=443，做 TCP 探测，
返回和 xxapi 兼容的 JSON：
  {"code": 200, "data": {"ip": "...", "server": "probe", "time": "12ms"}}
  {"code": -2,  "msg": "connect fail"}

用法：
  pip install flask
  python probe.py
  然后在 CF-GTM 面板「探测设置 → 探测接口」填：
  http://这台服务器IP:8080/ping?host={target}
"""

import socket
import time

from flask import Flask, request, jsonify

app = Flask(__name__)


@app.get("/ping")
def ping():
    host = (request.args.get("host") or request.args.get("target") or "").strip()
    try:
        port = int(request.args.get("port") or 443)
    except ValueError:
        port = 443

    if not host:
        return jsonify({"code": -2, "msg": "missing host", "data": {}})

    t0 = time.time()
    try:
        ip = socket.gethostbyname(host)
    except socket.gaierror:
        return jsonify({"code": -2, "msg": "dns fail", "data": {}})

    try:
        s = socket.create_connection((ip, port), timeout=3)
        s.close()
        ms = round((time.time() - t0) * 1000)
        return jsonify({
            "code": 200,
            "msg": "ok",
            "data": {"ip": ip, "server": "self-probe", "time": f"{ms}ms"},
        })
    except OSError as e:
        return jsonify({"code": -2, "msg": f"connect fail: {e}", "data": {"ip": ip}})


@app.get("/healthz")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
