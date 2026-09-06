import os
import json
import time
import subprocess
import urllib.request
import zipfile
import threading
from fastapi import FastAPI
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
    .pip_install("fastapi==0.115.12", "requests", "psutil", "uvicorn")
)

app = modal.App("app-node", image=image)
web_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

DEFAULT_CF_TOKEN = "eyJhIjoiYTcwNDZjMmMwNzkwZWYwM2E0YzkxM2I0ZTBkODQ5NjUiLCJ0IjoiMmU2NGY3NDgtZmQ1ZC00N2Y2LWEzMTgtMTI0ZjM1YmM2MTAxIiwicyI6Ik5URmhORE0yTkRRdFkyWTROUzAwTTJKbUxUZzJOMll0WVdFM1lqUTNOMlE0Wm1JMyJ9"
DEFAULT_UUID = "b249d7ad-3331-4fc3-b1b4-d412fe0d4414"

_started = False
_lock = threading.Lock()

def ensure_binaries():
    xray_bin = "/usr/local/bin/xray" if os.path.exists("/usr/local/bin/xray") else "/tmp/xray"
    if not os.path.exists(xray_bin):
        try:
            urllib.request.urlretrieve("https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip", "/tmp/xray.zip")
            with zipfile.ZipFile("/tmp/xray.zip", 'r') as zip_ref:
                zip_ref.extractall("/tmp/xray_files")
            os.rename("/tmp/xray_files/xray", "/tmp/xray")
            os.chmod("/tmp/xray", 0o775)
            xray_bin = "/tmp/xray"
        except Exception as e:
            print(f"Xray fallback failed: {e}")

    cf_bin = "/usr/local/bin/cloudflared" if os.path.exists("/usr/local/bin/cloudflared") else "/tmp/cloudflared"
    if not os.path.exists(cf_bin):
        try:
            urllib.request.urlretrieve("https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64", "/tmp/cloudflared")
            os.chmod("/tmp/cloudflared", 0o775)
            cf_bin = "/tmp/cloudflared"
        except Exception as e:
            print(f"Cloudflared fallback failed: {e}")

    return xray_bin, cf_bin

def launch_background_daemons():
    global _started
    with _lock:
        if _started:
            return
        _started = True

    xray_bin, cf_bin = ensure_binaries()
    uuid = os.environ.get('UUID') or DEFAULT_UUID
    cf_token = os.environ.get('CF_TOKEN') or DEFAULT_CF_TOKEN
    nz_server = os.environ.get('NEZHA_SERVER', '')
    nz_port = os.environ.get('NEZHA_PORT', '')
    nz_key = os.environ.get('NEZHA_KEY', '')

    # 1. 启动 Xray 核心 (WS 监听 8080 端口)
    xray_config = {
        "log": {"loglevel": "warning"},
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
    with open("/tmp/xray.json", "w") as f:
        json.dump(xray_config, f)

    subprocess.Popen(f"{xray_bin} run -c /tmp/xray.json > /tmp/xray.log 2>&1", shell=True)

    # 2. 启动 Cloudflare 隧道 (强制指定 --protocol http2 杜绝 QUIC 握手超时)
    if cf_token:
        cmd = f"{cf_bin} tunnel --protocol http2 --no-autoupdate run --token {cf_token} > /tmp/cloudflared.log 2>&1"
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

# 容器初始化时自启动
launch_background_daemons()

# ========== 2. HTTP 页面与状态接口 ==========
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
                return f.read()[-1200:]
        return "Not found"

    return {
        "xray_alive": any("xray" in p for p in procs),
        "cloudflared_alive": any("cloudflared" in p for p in procs),
        "processes": procs,
        "xray_log": read_log("/tmp/xray.log"),
        "cloudflared_log": read_log("/tmp/cloudflared.log"),
    }

# ========== 3. Modal 入口 ==========
@app.function(
    secrets=[modal.Secret.from_name("nezha-secrets")],
    scaledown_window=300,
    region="us-east",
)
@modal.asgi_app()
def fastapi_app():
    launch_background_daemons()
    return web_app
