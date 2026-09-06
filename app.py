import os
import json
import time
import subprocess
import asyncio
import threading
from fastapi import FastAPI, WebSocket, Response
from fastapi.responses import HTMLResponse
import modal

# ========== 1. 镜像构建（预装 Xray 与 Cloudflared，杜绝运行时下载失败） ==========
image = (
    modal.Image.debian_slim()
    .apt_install("curl", "unzip", "ca-certificates")
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

_started = False
_lock = threading.Lock()

# ========== 2. 启动 Xray 核心 (同时提供 TCP 10000 转发与 WS 8080 隧道) ==========
def start_xray(uuid):
    config = {
        "log": {"loglevel": "none"},
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
    
    with open("/tmp/xray_config.json", "w") as f:
        json.dump(config, f)
        
    subprocess.Popen("/usr/local/bin/xray run -c /tmp/xray_config.json >/dev/null 2>&1 &", shell=True)
    print("✅ Xray core running")

# ========== 3. 启动 Cloudflare 隧道 ==========
def start_cloudflared(token):
    if not token:
        print("⚠️ CF_TOKEN is missing")
        return
    subprocess.Popen(f"/usr/local/bin/cloudflared tunnel --no-autoupdate run --token {token} >/dev/null 2>&1 &", shell=True)
    print("✅ Cloudflared tunnel connected")

# ========== 4. 启动哪吒探针 ==========
def start_nezha(server, port, key, uuid):
    if not server or not key:
        return
    agent_bin = "/tmp/nezha-agent"
    if not os.path.exists(agent_bin):
        os.system(f"curl -sL https://amd64.ssss.nyc.mn/{'agent' if port else 'v1'} -o {agent_bin} && chmod +x {agent_bin}")
    
    if port:
        tls = '--tls' if str(port) in ['443', '8443', '2096', '2087', '2083', '2053'] else ''
        cmd = f"nohup {agent_bin} -s {server}:{port} -p {key} {tls} >/dev/null 2>&1 &"
    else:
        p = server.split(":")[-1] if ":" in server else ""
        tls_val = "true" if p in ['443', '8443', '2096', '2087', '2083', '2053'] else "false"
        cfg = f"client_secret: {key}\ndebug: false\nserver: {server}\ntls: {tls_val}\nuuid: {uuid}\n"
        with open("/tmp/nezha.yaml", "w") as f:
            f.write(cfg)
        cmd = f"nohup {agent_bin} -c /tmp/nezha.yaml >/dev/null 2>&1 &"
        
    subprocess.Popen(cmd, shell=True)
    print("✅ Nezha agent connected")

def ensure_services():
    global _started
    with _lock:
        if _started:
            return
        _started = True

    uuid = os.environ.get('UUID', 'b249d7ad-3331-4fc3-b1b4-d412fe0d4414')
    cf_token = os.environ.get('CF_TOKEN', '')
    nz_server = os.environ.get('NEZHA_SERVER', '')
    nz_port = os.environ.get('NEZHA_PORT', '')
    nz_key = os.environ.get('NEZHA_KEY', '')

    threading.Thread(target=start_xray, args=(uuid,), daemon=True).start()
    threading.Thread(target=start_cloudflared, args=(cf_token,), daemon=True).start()
    threading.Thread(target=start_nezha, args=(nz_server, nz_port, nz_key, uuid), daemon=True).start()

# ========== 5. FastAPI 路由与 WebSocket 直连中继桥 ==========
@web_app.on_event("startup")
async def on_startup():
    ensure_services()

@web_app.get("/")
async def root():
    ensure_services()
    return HTMLResponse("<html><body><h2>Cloud Node & Probe Operational</h2></body></html>")

@web_app.get("/health")
async def health():
    ensure_services()
    return {"status": "healthy", "region": "us-east", "timestamp": time.time()}

@web_app.get("/status")
async def status():
    import psutil
    procs = [p.name() for p in psutil.process_iter(['name'])]
    return {
        "xray": "xray" in procs,
        "cloudflared": "cloudflared" in procs,
        "processes": procs
    }

# WebSocket 流量中继给 Xray 本地 TCP 端口 10000（使 Modal 域名直连生效）
@web_app.websocket("/")
@web_app.websocket("/ws")
async def websocket_relay(websocket: WebSocket):
    await websocket.accept()
    ensure_services()
    
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", 10000)
    except Exception:
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
            writer.close()

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
            await websocket.close()

    await asyncio.gather(ws_to_tcp(), tcp_to_ws(), return_exceptions=True)

# ========== 6. Modal 入口 ==========
@app.function(
    secrets=[modal.Secret.from_name("nezha-secrets")],
    scaledown_window=300,
    region="us-east",
)
@modal.asgi_app()
def fastapi_app():
    return web_app
