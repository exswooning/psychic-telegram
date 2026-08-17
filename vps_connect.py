#!/usr/bin/env python3
"""Local UI launcher: connect to the migration VPS with just an IP + password.

The VPS's webui.py (8080) and api_server.py (8090) deliberately bind
127.0.0.1 only -- the SSH tunnel IS the access control. Browser-run UI
served from the VPS cannot open that tunnel (a remote page can't reach back
into your machine), so this is a tiny LOCAL server that takes the IP and the
root password once and opens both tunnels for you. Works without sshpass:
uses `expect` to answer the SSH password prompt, and never stores the
password.

    python3 vps_connect.py              # open http://localhost:8899
    python3 vps_connect.py --port 8899
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import shutil
import subprocess
import threading
import urllib.parse

STATE_DIR = os.path.expanduser("~/.workspace_migrator")
PIDFILE = os.path.join(STATE_DIR, "tunnel.pid")
os.makedirs(STATE_DIR, exist_ok=True)

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Connect to VPS</title>
<style>
  body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;
       color:#e6e8eb;display:flex;justify-content:center;padding-top:8vh;margin:0}
  .card{background:#1a1e27;border:1px solid #2a3040;border-radius:12px;
        padding:28px 32px;width:min(420px,90vw)}
  h1{font-size:19px;margin:0 0 4px}
  p.hint{color:#8a93a6;font-size:13px;margin:0 0 20px}
  label{display:block;font-size:12px;color:#aab2c4;margin:12px 0 4px}
  input{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;
        border:1px solid #333c50;background:#0f1115;color:#e6e8eb;font-size:14px}
  button{width:100%;margin-top:20px;padding:11px;border:none;border-radius:8px;
         background:#3b82f6;color:#fff;font-size:15px;font-weight:600;cursor:pointer}
  button:disabled{opacity:.6;cursor:wait}
  #msg{margin-top:14px;font-family:ui-monospace,monospace;font-size:12px;
       white-space:pre-wrap;color:#8a93a6;display:none}
  a.link{display:block;margin-top:10px;color:#7dd3fc;text-decoration:none;
         font-size:15px}
  .err{color:#f87171}
  .ok{color:#4ade80}
</style></head><body>
<div class="card">
  <h1>Connect to the migration VPS</h1>
  <p class="hint">Opens the SSH tunnel so the Mission Control UI can load.
  Nothing is stored.</p>
  <label>VPS IP / host</label>
  <input id="ip" value="78.47.176.120" placeholder="e.g. 78.47.176.120">
  <label>Username</label>
  <input id="user" value="root" placeholder="root">
  <label>Password</label>
  <input id="pw" type="password" placeholder="SSH password"
         onkeydown="if(event.key==='Enter')connect()">
  <button id="btn" onclick="connect()">Connect</button>
<pre id="msg"></pre>
  <div id="links"></div>
</div>
<script>
async function connect(){
  const ip=document.getElementById('ip').value.trim(),
        user=document.getElementById('user').value.trim(),
        pw=document.getElementById('pw').value;
  if(!ip||!user||!pw){setStatus('fill in all fields','err');return}
  const btn=document.getElementById('btn');
  btn.disabled=true; btn.textContent='Connecting...';
  setStatus('opening tunnel...','');
  try{
    const r=await fetch('/connect',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ip,user,pw})});
    const d=await r.json();
    btn.disabled=false; btn.textContent='Connect';
    if(!d.ok){setStatus(d.error||'failed','err');return}
    links(d);
  }catch(e){
    btn.disabled=false; btn.textContent='Connect';
    setStatus('error: '+e,'err');
  }
}
function links(d){
  const base=d.openUrl||'http://localhost:8080';
  const el=document.getElementById('links');
  el.innerHTML='<div class="ok" style="font-size:14px;margin-top:16px">'+
    '&#10003; Connected</div>'+
    '<a class="link" target="_blank" href="'+base+'">Open '+base+'</a>'+
    (d.cpUrl?('<a class="link" target="_blank" href="'+d.cpUrl+
      '">Control plane: '+d.cpUrl+'</a>'):'');
}
function setStatus(t,cls){
  const m=document.getElementById('msg');
  m.style.display='block'; m.className=cls; m.textContent=t;
}
</script></body></html>
"""


def _tunnel_running() -> bool:
    if not os.path.exists(PIDFILE):
        return False
    try:
        pid = int(open(PIDFILE, encoding="utf-8").read().strip())
        os.kill(pid, 0)
        return True
    except Exception:  # noqa: BLE001
        return False


def _port_alive(port: int) -> bool:
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=2)
        s.close()
        return True
    except OSError:
        return False


def _expect_script(target: str, ui_port: int, cp_port: int) -> str:
    """An expect program that answers the SSH password prompt and opens the
    two forward tunnels. The password comes from the $VPS_PW env var (kept
    out of the file/argv); the script file deletes itself when ssh exits so
    no copy lingers on disk.

    The VPS services listen on 8080 (webui.py) and 8090 (api_server.py), so
    the *remote* side of each leg is fixed to those ports; only the local
    side is the passed-in port. Forwarding to the same port on both sides
    was the original bug -- it put 8900→VPS:8900, where nothing listens."""
    return f'''set timeout 30
spawn ssh -N \\
  -L {ui_port}:localhost:8080 \\
  -L {cp_port}:localhost:8090 \\
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o \
ServerAliveCountMax=3 \\
  -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 {target}
expect {{
  -re {{[Pp]assword:}} {{ send "$env(VPS_PW)\\r"; exp_continue }}
  "yes/no" {{ send "yes\\r"; exp_continue }}
  eof {{ }}
  timeout {{ puts "TIMEOUT"; exit 1 }}
}}
file delete [info script]
'''


def open_tunnel(ip: str, user: str, password: str, ui_port: int,
                cp_port: int) -> dict:
    target = f"{user}@{ip}"
    if _tunnel_running() and _port_alive(ui_port):
        return {"ok": True, "openUrl": f"http://localhost:{ui_port}",
                "cpUrl": f"http://localhost:{cp_port}",
                "msg": "already connected"}
    for p in (ui_port, cp_port):
        if _port_alive(p):
            return {"ok": False,
                    "error": f"port {p} is already in use locally by "
                             f"something else; stop it first"}

    which = shutil.which("expect")
    if not which:
        return {"ok": False,
                "error": "no `expect` on this machine (install it, or use "
                         "the SSH-key path via connect_vps.sh)"}

    script = _expect_script(target, ui_port, cp_port)
    import tempfile
    fd, path = tempfile.mkstemp(prefix="vps_tunnel_", suffix=".exp")
    os.fdopen(fd, "w").write(script)

    logpath = os.path.join(STATE_DIR, "tunnel.log")
    env = dict(os.environ, VPS_PW=password)
    with open(logpath, "wb") as log:
        proc = subprocess.Popen(
            [which, "-f", path], stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, env=env)

    import time
    time.sleep(1)
    if proc.poll() is not None:
        return {"ok": False,
                "error": f"tunnel process exited immediately; see {logpath}"}
    with open(PIDFILE, "w", encoding="utf-8") as fh:
        fh.write(str(proc.pid))

    for _ in range(12):
        if proc.poll() is not None:
            break
        if _port_alive(ui_port):
            return {"ok": True,
                    "openUrl": f"http://localhost:{ui_port}",
                    "cpUrl": f"http://localhost:{cp_port}"}
        time.sleep(1)
    if _port_alive(ui_port):
        return {"ok": True, "openUrl": f"http://localhost:{ui_port}",
                "cpUrl": f"http://localhost:{cp_port}"}
    return {"ok": False,
            "error": "tunnel opened but the UI port is not answering yet; "
                     "check the webui on the VPS is running"}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: D401 - quiet
        pass

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):  # noqa: N802
        if self.path != "/connect":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            data = {}
        ip = str(data.get("ip", "")).strip()
        user = str(data.get("user", "root")).strip()
        pw = str(data.get("pw", ""))
        ui = self.server.ui_port
        cp = self.server.cp_port
        if not ip:
            self._json({"ok": False, "error": "no IP given"}, 400)
            return
        result = open_tunnel(ip, user, pw, ui, cp)
        self._json(result, 200 if result.get("ok") else 400)

    def _json(self, obj, code=200):  # noqa: D401 - simple helper
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, port):  # noqa: D107
        self.ui_port = port + 1
        self.cp_port = port + 2
        super().__init__(addr, Handler)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8899,
                    help="local launcher port (UI tunnel = port+1, control "
                         "plane = port+2)")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)
    print(f"Connect UI open:  http://{args.host}:{args.port}")
    print(f"Tunnels it opens: localhost:{args.port+1} (UI) + "
          f"localhost:{args.port+2} (control plane)")
    try:
        Server((args.host, args.port), args.port).serve_forever()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())