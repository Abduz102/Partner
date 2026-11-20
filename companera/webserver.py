"""Modo navegador: la misma interfaz servida en http://127.0.0.1 (útil en Linux/macOS, sin pywebview).

Seguridad: solo escucha en 127.0.0.1, exige un token aleatorio y comprueba la cabecera Host
(evita que otra página web abierta en tu navegador use la API: CSRF / DNS rebinding).
"""
from __future__ import annotations

import collections
import json
import queue
import secrets
import shutil
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UI = Path(__file__).parent / "ui" / "index.html"

SHIM = """<script>(()=>{
const q=new URLSearchParams(location.search).get('t');if(q)sessionStorage.setItem('tok',q);
const tok=sessionStorage.getItem('tok')||'';
window.pywebview={api:new Proxy({},{get:(_,m)=>async(...a)=>{
 const r=await fetch('/api/'+m,{method:'POST',headers:{'X-Token':tok,'Content-Type':'application/json'},body:JSON.stringify({args:a})});
 const j=await r.json();if(j.error)throw new Error(j.error);return j.r;}})};
document.addEventListener('DOMContentLoaded',()=>{document.body.classList.add('web');
 const es=new EventSource('/events?t='+encodeURIComponent(tok));
 es.onmessage=e=>{try{window.ui&&window.ui.evento(JSON.parse(e.data));}catch(_){}};
 es.onerror=()=>{window.ui&&window.ui.evento({t:'aviso',v:'Conexión con la compañera perdida (¿cerraste el programa?)'});};});
})();</script>"""


class WebApp:
    def __init__(self, api, puerto: int = 0):
        self.api = api
        self.token = secrets.token_urlsafe(24)
        self._clientes: list[queue.Queue] = []
        # Lo último de la charla se reenvía al conectar (o recargar) la página: así no se pierde el saludo
        # que ella da mientras el navegador todavía está abriendo.
        self._hist: collections.deque = collections.deque(maxlen=400)
        self._lock = threading.Lock()
        self.cerrado = threading.Event()
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _ok_host(self):
                return self.headers.get("Host", "") in (f"127.0.0.1:{outer.puerto}", f"localhost:{outer.puerto}")

            def _token(self):
                t = self.headers.get("X-Token")
                if t is None:
                    from urllib.parse import parse_qs, urlparse
                    t = (parse_qs(urlparse(self.path).query).get("t") or [""])[0]
                return secrets.compare_digest(t or "", outer.token)

            def _enviar(self, code, body: bytes, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if not (self._ok_host() and self._token()):
                    return self._enviar(403, b'{"error":"forbidden"}')
                ruta = self.path.split("?")[0]
                if ruta == "/":
                    html = UI.read_text(encoding="utf-8").replace("</head>", SHIM + "</head>", 1)
                    return self._enviar(200, html.encode("utf-8"), "text/html; charset=utf-8")
                if ruta == "/events":
                    return self._sse()
                self._enviar(404, b'{"error":"not found"}')

            def _sse(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                q: queue.Queue = queue.Queue()
                with outer._lock:
                    for m in outer._hist:
                        q.put(m)
                    outer._clientes.append(q)
                try:
                    self.wfile.write(b": hola\n\n")
                    self.wfile.flush()
                    while not outer.cerrado.is_set():
                        try:
                            msg = q.get(timeout=10)
                            self.wfile.write(f"data: {msg}\n\n".encode("utf-8"))
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with outer._lock:
                        if q in outer._clientes:
                            outer._clientes.remove(q)
                    self.close_connection = True

            def do_POST(self):
                if not (self._ok_host() and self._token()):
                    return self._enviar(403, b'{"error":"forbidden"}')
                if not self.path.startswith("/api/"):
                    return self._enviar(404, b'{"error":"not found"}')
                nombre = self.path[5:]
                fn = getattr(outer.api, nombre, None) if nombre and not nombre.startswith("_") else None
                if not callable(fn):
                    return self._enviar(404, b'{"error":"metodo desconocido"}')
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    args = json.loads(self.rfile.read(n) or b"{}").get("args", [])
                    res = fn(*args)
                    self._enviar(200, json.dumps({"r": res}, ensure_ascii=False, default=str).encode("utf-8"))
                except Exception as e:  # noqa: BLE001
                    self._enviar(500, json.dumps({"error": str(e)}).encode("utf-8"))

        self.srv = ThreadingHTTPServer(("127.0.0.1", puerto), H)
        self.srv.daemon_threads = True
        self.puerto = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.puerto}/?t={self.token}"

    # ---- eventos Python → navegador
    def emit(self, tipo, valor=None):
        msg = json.dumps({"t": tipo, "v": valor}, ensure_ascii=False)
        with self._lock:
            if tipo in ("usuario", "ia_inicio", "ia_delta", "ia_fin", "emocion", "herramienta"):
                self._hist.append(msg)
            for q in self._clientes:
                q.put(msg)

    def servir(self):
        threading.Thread(target=self.srv.serve_forever, daemon=True, name="web").start()

    def detener(self):
        self.cerrado.set()
        threading.Thread(target=self.srv.shutdown, daemon=True).start()


def abrir_ventana(url: str) -> str:
    """Abre la interfaz como ventana tipo app (Chromium/Chrome --app) o, si no hay, en el navegador."""
    for exe in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "brave-browser", "microsoft-edge"):
        ruta = shutil.which(exe)
        if ruta:
            subprocess.Popen([ruta, f"--app={url}", "--window-size=430,760"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return exe
    webbrowser.open(url)
    return "navegador predeterminado"
