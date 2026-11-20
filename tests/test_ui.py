import os, sys, unittest
from pathlib import Path
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright

URL = (Path(__file__).resolve().parents[1] / "companera" / "ui" / "index.html").as_uri()
MOCK = """
window.__calls = [];
window.pywebview = {api: new Proxy({}, {get: (t, m) => async (...a) => { window.__calls.push([m, ...a]);
  if (m === 'iniciar') return {nombre:'Aiko', usuario:'', voz:'kokoro', juego:false, dnd:false, llamada:false, avisos:[]};
  if (m === 'ajustes') return {nombre:'Aiko', usuario:'Cami', hablar_respuestas:true, auriculares:false, usar_ligero:false, iniciativa:true, stt_modelo:'base'};
  if (m === 'recuerdos') return [{id:7, texto:'Le gusta el café', tipo:'preferencia', importancia:4, fecha:null}];
  if (m === 'alternar_llamada') return true;
  if (m === 'alternar_juego') return true;
  return true; }})};
"""

class TestUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = sync_playwright().start(); cls.b = cls.p.chromium.launch()
    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.p.stop()
    def pagina(self, scheme="dark"):
        pg = self.b.new_page(viewport={"width": 400, "height": 700}, color_scheme=scheme)
        self.errs = []; pg.on("pageerror", lambda e: self.errs.append(str(e)))
        pg.add_init_script(MOCK); pg.goto(URL); pg.wait_for_timeout(300); return pg
    def ev(self, pg, t, v=None): pg.evaluate("e=>window.ui.evento(e)", {"t": t, "v": v})

    def test_flujo(self):
        pg = self.pagina()
        pg.fill("#txt", "hola"); pg.press("#txt", "Enter")
        self.assertIn(["enviar", "hola"], pg.evaluate("window.__calls"))
        self.ev(pg, "usuario", "hola"); self.ev(pg, "ia_inicio", True); self.ev(pg, "emocion", "traviesa")
        for d in ["Ey, ", "qué ", "bueno"]: self.ev(pg, "ia_delta", d)
        self.ev(pg, "ia_fin", True)
        self.assertEqual(pg.inner_text(".msg.ia"), "Ey, qué bueno"); self.assertEqual(pg.locator(".msg.ia.vivo").count(), 0)
        pg.click("#b-mic"); self.assertEqual(pg.get_attribute("#b-mic", "aria-pressed"), "true")
        pg.click("#t-juego"); self.assertEqual(pg.get_attribute("#t-juego", "aria-pressed"), "true")
        self.assertEqual(self.errs, [])

    def test_permiso(self):
        pg = self.pagina()
        self.ev(pg, "confirmar", "Abrir /etc/hostname")
        self.assertTrue(pg.is_visible("#cap-conf")); pg.click("#conf-no")
        self.assertFalse(pg.is_visible("#cap-conf")); self.assertIn(["confirmar", False], pg.evaluate("window.__calls"))
        self.ev(pg, "confirmar", "x"); pg.click("#conf-si"); self.assertIn(["confirmar", True], pg.evaluate("window.__calls"))

    def test_ajustes_y_memoria(self):
        pg = self.pagina("light")
        pg.click("#b-aj"); self.assertEqual(pg.input_value("#a-usuario"), "Cami")
        self.assertIn("Le gusta el café", pg.inner_text("#recs"))
        pg.click(".rec button"); self.assertIn(["borrar_recuerdo", 7], pg.evaluate("window.__calls"))
        pg.fill("#a-nombre", "Mika"); pg.click("#aj-ok")
        self.assertTrue(any(c[0] == "guardar_ajustes" and c[1]["nombre"] == "Mika" for c in pg.evaluate("window.__calls")))

    def test_texto_html_no_se_inyecta(self):
        pg = self.pagina()
        self.ev(pg, "usuario", "<img src=x onerror=window.__x=1>")
        pg.wait_for_timeout(100); self.assertIsNone(pg.evaluate("window.__x ?? null")); self.assertEqual(self.errs, [])

    def test_captura_oscura(self):
        pg = self.pagina("dark"); self.ev(pg, "emocion", "feliz"); self.ev(pg, "estado", "escuchando")
        pg.screenshot(path="/tmp/ui_dark.png")

if __name__ == "__main__":
    unittest.main(verbosity=2)
