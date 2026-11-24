# 自建国内探测服务

CF-GTM 默认用第三方接口 `xxapi.cn` 做国内 ping。如果你有国内服务器，建议自建，优点是：

- 不受第三方限流 / 接口变更影响；
- 用 **TCP 探测**，比 ICMP 更准（能识别“ping 通但 TCP 被墙”）；
- 完全可控。

## 部署

```bash
# 1. 上传 probe.py 到国内服务器
mkdir -p /opt/probe && cd /opt/probe
# 上传 probe.py

# 2. 安装依赖
python3 -m venv venv
./venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple flask

# 3. 启动（测试）
./venv/bin/python probe.py
```

systemd 守护（可选）：

```ini
[Unit]
Description=CF-GTM Probe
After=network.target

[Service]
WorkingDirectory=/opt/probe
ExecStart=/opt/probe/venv/bin/python /opt/probe/probe.py
Restart=always
User=root

[Install]
WantedBy=multi-user.target
```

## 测试

```bash
curl "http://127.0.0.1:8080/ping?host=baidu.com&port=443"
# {"code":200,"msg":"ok","data":{"ip":"...","server":"self-probe","time":"12ms"}}
```

## 填到面板

CF-GTM 面板 → 探测设置 → 探测接口：

```
http://这台服务器IP:8080/ping?host={target}
```

保存即可。记得在服务器的防火墙/安全组放行 8080（或你自定义的端口）。
