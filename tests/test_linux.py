import json, os, re, signal, subprocess, sys, threading, time, unittest, urllib.request, urllib.error, wave
from pathlib import Path
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.helpers import MockOllama, entorno_temporal, esperar

RAIZ = Path(__file__).resolve().parents[1]
HOME = entorno_temporal()
from companera import config as C
from companera.app import Api
from companera.config import Config
from companera.memory import Memory
from companera.runtime import Runtime
from companera.speech import EspeakEngine
from companera.tools import PCTools
from companera.webserver import WebApp


def http(url, metodo="GET", token=None, cuerpo=None, host=None):
    req = urllib.request.Request(url, method=metodo, data=json.dumps(cuerpo).encode() if cuerpo is not None else None)
    if token:
        req.add_header("X-Token", token)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


class TestWeb(unittest.TestCase):
    def setUp(self):
        self.srv = MockOllama()
        cfg = Config(HOME / f"w{time.time_ns()}.json"); cfg.data["ollama_url"] = self.srv.url
        self.web = WebApp(None); self.rt = Runtime(cfg, emit=self.web.emit, con_voz=False)
        self.web.api = Api(self.rt); self.web.servir()
        self.base = f"http://127.0.0.1:{self.web.puerto}"

    def tearDown(self):
        self.web.detener()

    def test_seguridad(self):
        self.assertEqual(http(self.base + "/")[0], 403)                                   # sin token
        self.assertEqual(http(self.base + "/?t=malo")[0], 403)
        self.assertEqual(http(self.base + "/api/iniciar", "POST", None, {"args": []})[0], 403)
        self.assertEqual(http(self.base + "/api/iniciar", "POST", "malo", {"args": []})[0], 403)
        self.assertEqual(http(self.base + "/?t=" + self.web.token, host="evil.com")[0], 403)   # DNS rebinding
        self.assertEqual(http(self.base + "/api/_rt", "POST", self.web.token, {"args": []})[0], 404)   # métodos privados no
        self.assertEqual(http(self.base + "/api/__class__", "POST", self.web.token, {"args": []})[0], 404)

    def test_pagina_y_api(self):
        st, html = http(self.base + "/?t=" + self.web.token)
        self.assertEqual(st, 200); self.assertIn("window.pywebview", html); self.assertIn("EventSource", html)
        st, r = http(self.base + "/api/iniciar", "POST", self.web.token, {"args": []})
        self.assertEqual(st, 200); self.assertEqual(json.loads(r)["r"]["nombre"], "Aiko")
        st, r = http(self.base + "/api/guardar_ajustes", "POST", self.web.token, {"args": [{"nombre": "Mika", "modelo": "x"}]})
        self.assertEqual(json.loads(r)["r"]["nombre"], "Mika"); self.assertEqual(self.rt.cfg["modelo"], "companera")

    def test_eventos_en_tiempo_real(self):
        vistos = []
        def leer():
            req = urllib.request.Request(self.base + "/events?t=" + self.web.token)
            with urllib.request.urlopen(req, timeout=10) as r:
                for linea in r:
                    if linea.startswith(b"data:"):
                        vistos.append(json.loads(linea[5:])["t"])
                        if vistos[-1] == "ia_fin":
                            return
        th = threading.Thread(target=leer, daemon=True); th.start(); time.sleep(0.3)
        http(self.base + "/api/enviar", "POST", self.web.token, {"args": ["hola"]})
        th.join(8); self.assertIn("ia_fin", vistos); self.assertIn("emocion", vistos); self.assertIn("ia_delta", vistos)


class TestLinux(unittest.TestCase):
    def test_apps_por_candidatos(self):
        import companera.tools as T
        lanz = []
        cfg = Config(HOME / "l1.json"); cfg.data["pc"]["apps"] = {"mi editor": ["no-existe-xyz", "sh"]}
        mem = Memory(HOME / "l1.db"); tools = PCTools(cfg, mem, launcher=lanz.append)
        self.assertIn("Listo", tools.run("abrir_app", {"nombre": "mi editor"})); self.assertEqual(lanz, ["sh"])
        cfg.data["pc"]["apps"] = {"fantasma": ["no-existe-xyz"]}
        self.assertIn("no está instalado", tools.run("abrir_app", {"nombre": "fantasma"}))
        self.assertIn("firefox", cfg.apps if os.name != "nt" else {"firefox": 1})

    def test_lanzador_real_en_linux(self):
        cfg = Config(HOME / "l2.json"); tools = PCTools(cfg, Memory(HOME / "l2.db"))
        llamadas = []
        orig = subprocess.Popen
        subprocess.Popen = lambda cmd, **k: llamadas.append(cmd)
        try:
            tools._launch_real("https://example.com"); tools._launch_real("/tmp"); tools._launch_real("gnome-calculator")
        finally:
            subprocess.Popen = orig
        self.assertEqual(llamadas, [["xdg-open", "https://example.com"], ["xdg-open", "/tmp"], ["gnome-calculator"]])

    def test_carpetas_xdg(self):
        h = HOME / "home"; (h / ".config").mkdir(parents=True); (h / "Documentos").mkdir(); (h / "Descargas").mkdir()
        (h / ".config" / "user-dirs.dirs").write_text('XDG_DOCUMENTS_DIR="$HOME/Documentos"\nXDG_DOWNLOAD_DIR="$HOME/Descargas"\n')
        os.environ["XDG_CONFIG_HOME"] = str(h / ".config"); old = os.environ.get("HOME"); os.environ["HOME"] = str(h)
        try:
            carpetas = C._carpetas_usuario()
        finally:
            os.environ["HOME"] = old; del os.environ["XDG_CONFIG_HOME"]
        self.assertEqual({Path(c).name for c in carpetas}, {"Documentos", "Descargas"})

    def test_espeak_con_proceso_simulado(self):
        import companera.speech as S
        def falso(cmd, input=None, **k):
            w = wave.open(cmd[cmd.index("-w") + 1], "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
            w.writeframes(b"\x00\x10" * 2205); w.close(); self.cmd = cmd; self.entrada = input
        old = S.subprocess.run; S.subprocess.run = falso
        try:
            e = EspeakEngine(); e.exe = "/usr/bin/espeak-ng"
            a, sr = e.synth("-hola ñandú", 1.0)
        finally:
            S.subprocess.run = old
        self.assertEqual(sr, 22050); self.assertEqual(len(a), 2205)
        self.assertEqual(self.entrada.decode(), "-hola ñandú"); self.assertIn("es+f3", self.cmd)   # el texto va por stdin, no como opción


class TestProcesoReal(unittest.TestCase):
    """Arranca `python -m companera --web` como proceso aparte, contra el Ollama simulado, y lo usa desde un navegador."""

    def test_extremo_a_extremo(self):
        from playwright.sync_api import sync_playwright
        srv = MockOllama()
        home = HOME / "e2e"; home.mkdir()
        (home / "config.json").write_text(json.dumps({"ollama_url": srv.url, "iniciativa": {"inactividad_min": 999}}))
        env = dict(os.environ, COMPANERA_HOME=str(home), PYTHONUNBUFFERED="1")
        p = subprocess.Popen([sys.executable, "-m", "companera", "--web", "--sin-abrir"], cwd=RAIZ, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            url, lineas = None, []
            fin = time.time() + 15
            while time.time() < fin and url is None:
                l = p.stdout.readline(); lineas.append(l)
                m = re.search(r"http://127\.0\.0\.1:\d+/\?t=\S+", l)
                url = m.group(0) if m else None
            self.assertTrue(url, "".join(lineas))
            with sync_playwright() as pw:
                b = pw.chromium.launch(); pg = b.new_page(viewport={"width": 430, "height": 760}, color_scheme="dark")
                errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
                pg.goto(url)
                pg.wait_for_selector(".msg.ia", timeout=10000)                       # ella saluda sola (iniciativa)
                self.assertIn("qué gusto", pg.inner_text(".msg.ia"))
                pg.fill("#txt", "Hola, me llamo Camila"); pg.press("#txt", "Enter")
                pg.wait_for_function("document.querySelectorAll('.msg.ia').length >= 2", timeout=10000)
                self.assertEqual(pg.locator(".msg.yo").inner_text(), "Hola, me llamo Camila")
                self.assertFalse(pg.is_visible("#b-min"))                            # sin botones de ventana en modo navegador
                pg.click("#b-aj"); pg.wait_for_selector("#recs .rec", timeout=5000)  # el nombre quedó en su memoria
                self.assertIn("Camila", pg.inner_text("#recs"))
                pg.screenshot(path="/tmp/ui_linux_web.png")
                self.assertEqual(errs, [])
                b.close()
            p.send_signal(signal.SIGINT)                                             # Ctrl+C: debe cerrar limpio
            self.assertEqual(p.wait(timeout=40), 0)
            m = Memory(home / "memoria.db"); self.assertTrue(any("Camila" in r["texto"] for r in m.all_memories()))
            self.assertEqual(json.loads((home / "config.json").read_text())["usuario"], "Camila")
        finally:
            if p.poll() is None:
                p.kill()
            p.stdout.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
