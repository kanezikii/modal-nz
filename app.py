import os
import json
import time
import subprocess
import urllib.request
import zipfile
import asyncio
import threading
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
import modal

# ========== 1. 预构建镜像 ==========
image = (
    modal.Image.debian_slim()
    .apt_install("curl", "unzip", "ca-certificates", "procps")
    .run_commands(
        "curl -sL https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip -o /tmp/xray.zip && "
        "unzip -q /tmp/xray.zip -d /tmp/xray_files && "
        "mv /tmp/xray_files/xray /usr/local/bin/xray && "
        "chmod +x /usr/local/bin/xray && "
        "rm -rf /tmp/xray*",
        "curl -sL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared && "
        "chmod +x /usr/local/bin/cloudflared",
    )
    .pip_install("fastapi==0.115.12", "requests", "psutil", "uvicorn", "websockets")
)

app = modal.App("app-node", image=image)
web_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

DEFAULT_CF_TOKEN = "eyJhIjoiYTcwNDZjMmMwNzkwZWYwM2E0YzkxM2I0ZTBkODQ5NjUiLCJ0IjoiMmU2NGY3NDgtZmQ1ZC00N2Y2LWEzMTgtMTI0ZjM1YmM2MTAxIiwicyI6Ik5URmhORE0yTkRRdFkyWTROUzAwTTJKbUxUZzJOMll0WVdFM1lqUTNOMlE0Wm1JMyJ9"
DEFAULT_UUID = "b249d7ad-3331-4fc3-b1b4-d412fe0d4414"

_started = False
_lock = threading.Lock()

def launch_background_daemons():
    global _started
    with _lock:
        if _started:
            return
        _started = True

    uuid = os.environ.get('UUID') or DEFAULT_UUID
    cf_token = os.environ.get('CF_TOKEN') or DEFAULT_CF_TOKEN
    nz_server = os.environ.get('NEZHA_SERVER', '')
    nz_port = os.environ.get('NEZHA_PORT', '')
    nz_key = os.environ.get('NEZHA_KEY', '')

    # 1. 启动 Xray 核心 (同时提供 10000 转发与 8080 WS 隧道)
    xray_config = {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "port": 10000,
                "listen": "127.0.0.1",
                "protocol": "vless",
                "settings": {"clients": [{"id": uuid, "level": 0}], "decryption": "none"},
                "streamSettings": {"network": "tcp"}
            },
            {
                "port": 8080,
                "listen": "0.0.0.0",
                "protocol": "vless",
                "settings": {"clients": [{"id": uuid, "level": 0}], "decryption": "none"},
                "streamSettings": {"network": "ws", "wsSettings": {"path": "/"}}
            }
        ],
        "outbounds": [{"protocol": "freedom"}]
    }
    with open("/tmp/xray.json", "w") as f:
        json.dump(xray_config, f)

    subprocess.Popen("/usr/local/bin/xray run -c /tmp/xray.json > /tmp/xray.log 2>&1", shell=True)

    # 2. 启动 Cloudflare 隧道 (强制使用 HTTP2 协议解决握手卡死)
    if cf_token:
        cmd = f"/usr/local/bin/cloudflared tunnel --protocol http2 --no-autoupdate run --token {cf_token} > /tmp/cloudflared.log 2>&1"
        subprocess.Popen(cmd, shell=True)

    # 3. 启动哪吒探针
    if nz_server and nz_key:
        agent_bin = "/tmp/nezha-agent"
        if not os.path.exists(agent_bin):
            os.system(f"curl -sL https://amd64.ssss.nyc.mn/{'agent' if nz_port else 'v1'} -o {agent_bin} && chmod +x {agent_bin}")
        
        if nz_port:
            tls_flag = '--tls' if str(nz_port) in ['443', '8443', '2096', '2087', '2083', '2053'] else ''
            subprocess.Popen(f"nohup {agent_bin} -s {nz_server}:{nz_port} -p {nz_key} {tls_flag} > /tmp/nezha.log 2>&1 &", shell=True)
        else:
            tls_val = "true" if ":443" in nz_server else "false"
            cfg = f"client_secret: {nz_key}\ndebug: false\nserver: {nz_server}\ntls: {tls_val}\nuuid: {uuid}\n"
            with open("/tmp/nezha.yaml", "w") as f:
                f.write(cfg)
            subprocess.Popen(f"nohup {agent_bin} -c /tmp/nezha.yaml > /tmp/nezha.log 2>&1 &", shell=True)

# 强制在容器初始化时启动后台服务
launch_background_daemons()

# ========== 2. HTTP 页面与健康检查 ==========
@web_app.on_event("startup")
def on_startup():
    launch_background_daemons()

@web_app.get("/")
def index():
    launch_background_daemons()
    return HTMLResponse("<html><body><h2>Cloud Proxy Node Operational</h2></body></html>")

@web_app.get("/health")
def health():
    launch_background_daemons()
    return {"status": "healthy", "time": time.time()}

@web_app.get("/status")
def status():
    launch_background_daemons()
    import psutil
    procs = [p.name() for p in psutil.process_iter(['name'])]
    
    def read_log(p):
        if os.path.exists(p):
            with open(p, "r", errors="ignore") as f:
                return f.read()[-1000:]
        return "Not found"

    return {
        "xray_alive": any("xray" in p for p in procs),
        "cloudflared_alive": any("cloudflared" in p for p in procs),
        "processes": procs,
        "xray_log": read_log("/tmp/xray.log"),
        "cloudflared_log": read_log("/tmp/cloudflared.log"),
    }

# ========== 3. WebSocket 直连中继桥 ==========
@web_app.websocket("/")
@web_app.websocket("/ws")
async def ws_proxy(websocket: WebSocket):
    await websocket.accept()
    launch_background_daemons()

    reader, writer = None, None
    for _ in range(20):
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", 10000)
            break
        except Exception:
            await asyncio.sleep(0.1)

    if not reader or not writer:
        await websocket.close()
        return

    async def ws_to_tcp():
        try:
            while True:
                data = await websocket.receive_bytes()
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def tcp_to_ws():
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                await websocket.send_bytes(data)
        except Exception:
            pass
        finally:
            try:
                await websocket.close()
            except Exception:
                pass

    await asyncio.gather(ws_to_tcp(), tcp_to_ws(), return_exceptions=True)

# ========== 4. Modal 入口 ==========
@app.function(
    secrets=[modal.Secret.from_name("nezha-secrets")],
    scaledown_window=300,
    region="us-east",
)
@modal.asgi_app()
def fastapi_app():
    launch_background_daemons()
    return web_app
