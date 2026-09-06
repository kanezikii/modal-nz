import os
import json
import time
import subprocess
import asyncio
import threading
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
import modal

# ========== 1. 预构建镜像（Xray + Cloudflared） ==========
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

app = modal.App("app", image=image)
web_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

_init_done = False
_init_lock = threading.Lock()

def start_services():
    global _init_done
    with _init_lock:
        if _init_done:
            return
        _init_done = True

    uuid = os.environ.get('UUID', 'b249d7ad-3331-4fc3-b1b4-d412fe0d4414')
    cf_token = os.environ.get('CF_TOKEN', '')
    nz_server = os.environ.get('NEZHA_SERVER', '')
    nz_port = os.environ.get('NEZHA_PORT', '')
    nz_key = os.environ.get('NEZHA_KEY', '')

    # 1. 启动 Xray
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

    # 2. 启动 Cloudflared 隧道
    if cf_token:
        subprocess.Popen(f"/usr/local/bin/cloudflared tunnel --no-autoupdate run --token {cf_token} > /tmp/cloudflared.log 2>&1", shell=True)

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

@web_app.on_event("startup")
def on_startup():
    start_services()

@web_app.get("/")
def index():
    start_services()
    return HTMLResponse("<html><body><h2>Cloud Node Operational</h2></body></html>")

@web_app.get("/health")
def health():
    start_services()
    return {"status": "ok", "time": time.time()}

# ========== 状态与日志查看端点 ==========
@web_app.get("/status")
def status():
    import psutil
    procs = [p.name() for p in psutil.process_iter(['name'])]
    
    def get_log(path):
        if os.path.exists(path):
            with open(path, "r", errors="ignore") as f:
                return f.read()[-1000:]
        return "Log file empty or not generated yet"

    return {
        "xray_running": any("xray" in p for p in procs),
        "cloudflared_running": any("cloudflared" in p for p in procs),
        "active_processes": procs,
        "xray_log": get_log("/tmp/xray.log"),
        "cloudflared_log": get_log("/tmp/cloudflared.log"),
    }

# ========== WebSocket 直连中继桥（带等待重试） ==========
@web_app.websocket("/")
@web_app.websocket("/ws")
async def websocket_relay(websocket: WebSocket):
    await websocket.accept()
    start_services()

    reader, writer = None, None
    for _ in range(10):
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", 10000)
            break
        except Exception:
            await asyncio.sleep(0.2)

    if not writer or not reader:
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

# ========== Modal 入口 ==========
@app.function(
    secrets=[modal.Secret.from_name("nezha-secrets")],
    scaledown_window=300,
    region="us-east",
)
@modal.asgi_app()
def fastapi_app():
    return web_app
