import os
import json
import time
import urllib.request
import zipfile
import subprocess
import platform
import threading
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse
import modal

DEPLOY_REGION = os.environ.get('DEPLOY_REGION', 'us-east')

image = modal.Image.debian_slim().pip_install(
    "fastapi==0.115.12",
    "requests",
    "psutil",
    "uvicorn",
)

app = modal.App("app", image=image)
web_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

_services_started = False
_services_lock = threading.Lock()

def get_arch():
    arch = platform.machine().lower()
    return 'arm' if 'arm' in arch or 'aarch64' in arch else 'amd'

# ========== 1. 运行 Xray 节点服务 (8080 端口) ==========
def run_xray(uuid):
    arch = get_arch()
    xray_arch = "arm64-v8a" if arch == 'arm' else "64"
    xray_bin = "/tmp/xray"
    zip_path = "/tmp/xray.zip"
    
    if not os.path.exists(xray_bin):
        url = f"https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-{xray_arch}.zip"
        try:
            urllib.request.urlretrieve(url, zip_path)
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall("/tmp/xray_files")
            os.rename("/tmp/xray_files/xray", xray_bin)
            os.chmod(xray_bin, 0o775)
        except Exception as e:
            print(f"Failed to download Xray: {e}")
            return

    config = {
        "log": {"loglevel": "none"},
        "inbounds": [{
            "port": 8080,
            "listen": "0.0.0.0",
            "protocol": "vless",
            "settings": {
                "clients": [{"id": uuid, "level": 0}],
                "decryption": "none"
            },
            "streamSettings": {
                "network": "ws",
                "wsSettings": {"path": "/"}
            }
        }],
        "outbounds": [{"protocol": "freedom"}]
    }
    
    config_path = "/tmp/xray_config.json"
    with open(config_path, "w") as f:
        json.dump(config, f)
        
    subprocess.Popen(f"{xray_bin} run -c {config_path} >/dev/null 2>&1 &", shell=True)
    print("✅ Xray service started on port 8080")

# ========== 2. 运行 Cloudflare Tunnel ==========
def run_cloudflared(token):
    if not token:
        print("⚠️ CF_TOKEN is empty, skipping cloudflared.")
        return
    arch = get_arch()
    cf_arch = "arm64" if arch == 'arm' else "amd64"
    cf_bin = "/tmp/cloudflared"
    
    if not os.path.exists(cf_bin):
        url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{cf_arch}"
        try:
            urllib.request.urlretrieve(url, cf_bin)
            os.chmod(cf_bin, 0o775)
        except Exception as e:
            print(f"Failed to download cloudflared: {e}")
            return
            
    cmd = f"{cf_bin} tunnel --no-autoupdate run --token {token} >/dev/null 2>&1 &"
    subprocess.Popen(cmd, shell=True)
    print("✅ Cloudflared tunnel connected")

# ========== 3. 运行 哪吒探针 ==========
def run_nezha(server, port, key, uuid):
    if not server or not key:
        return
    arch = get_arch()
    url = f"https://{'arm64' if arch == 'arm' else 'amd64'}.ssss.nyc.mn/{'agent' if port else 'v1'}"
    agent_bin = "/tmp/cache_worker"
    
    try:
        urllib.request.urlretrieve(url, agent_bin)
        os.chmod(agent_bin, 0o775)
    except Exception as e:
        print(f"Failed to download Nezha agent: {e}")
        return

    if port:
        tls = '--tls' if port in ['443', '8443', '2096', '2087', '2083', '2053'] else ''
        cmd = f"nohup {agent_bin} -s {server}:{port} -p {key} {tls} >/dev/null 2>&1 &"
    else:
        p = server.split(":")[-1] if ":" in server else ""
        tls_val = "true" if p in ['443', '8443', '2096', '2087', '2083', '2053'] else "false"
        cfg = f"client_secret: {key}\ndebug: false\nserver: {server}\ntls: {tls_val}\nuuid: {uuid}\n"
        with open("/tmp/nezha.yaml", "w") as f:
            f.write(cfg)
        cmd = f"nohup {agent_bin} -c /tmp/nezha.yaml >/dev/null 2>&1 &"
        
    subprocess.Popen(cmd, shell=True)
    print("✅ Nezha agent started")

# ========== 后台服务启动总控 ==========
def start_all_services():
    global _services_started
    with _services_lock:
        if _services_started:
            return
        _services_started = True

    uuid = os.environ.get('UUID', 'b249d7ad-3331-4fc3-b1b4-d412fe0d4414')
    cf_token = os.environ.get('CF_TOKEN', '')
    nz_server = os.environ.get('NEZHA_SERVER', '')
    nz_port = os.environ.get('NEZHA_PORT', '')
    nz_key = os.environ.get('NEZHA_KEY', '')

    threading.Thread(target=run_xray, args=(uuid,), daemon=True).start()
    threading.Thread(target=run_cloudflared, args=(cf_token,), daemon=True).start()
    threading.Thread(target=run_nezha, args=(nz_server, nz_port, nz_key, uuid), daemon=True).start()

@web_app.on_event("startup")
async def startup_event():
    start_all_services()

@web_app.get("/")
async def root():
    return HTMLResponse("<html><body><h2>Cloud Node & Probe Operational</h2></body></html>")

@web_app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": time.time()}

# ========== Modal 函数配置 ==========
selected_region = os.environ.get('DEPLOY_REGION', 'us-east')

@app.function(
    secrets=[modal.Secret.from_name("nezha-secrets")],
    scaledown_window=300,
    region=[selected_region],
)
@modal.concurrent(max_inputs=20)
@modal.asgi_app()
def fastapi_app():
    return web_app
