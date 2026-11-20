import sys, threading, time, unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.helpers import FakeEngine, FakePlayer, FakeSource, FakeTranscriber, MockOllama, entorno_temporal, esperar

HOME = entorno_temporal()
import numpy as np
from companera.app import Api
from companera.brain import Companion
from companera.config import Config
from companera.llm import Ollama
from companera.memory import Memory
from companera.runtime import Runtime
from companera.speech import Endpointer, Speaker
from companera.tools import PCTools
from companera.voice_loop import VoiceLoop


def ruido(amp, n=1, rng=np.random.default_rng(3)):
    return [(rng.standard_normal(480) * amp).astype(np.float32) for _ in range(n)]


class TestEndpointer(unittest.TestCase):
    def correr(self, ep, tramos):
        ev = []
        for amp, n in tramos:
            for f in ruido(amp, n):
                ev += ep.feed(f)
        return [e for e, _ in ev]

    def test_frase_completa(self):
        self.assertEqual(self.correr(Endpointer(), [(0.002, 30), (0.1, 40), (0.002, 50)]), ["start", "end"])

    def test_ruido_corto_se_descarta(self):
        self.assertEqual(self.correr(Endpointer(), [(0.002, 30), (0.1, 5), (0.002, 50)]), ["start", "discard"])

    def test_ruido_de_fondo_alto_no_dispara(self):
        # ventilador constante: el umbral se adapta, no hay falsos positivos; la voz encima sí se detecta
        ev = self.correr(Endpointer(), [(0.02, 80)])
        self.assertEqual(ev, [])
        self.assertEqual(self.correr(Endpointer(), [(0.02, 40), (0.3, 40), (0.02, 50)]), ["start", "end"])

    def test_modo_estricto_pide_mas_voz(self):
        ep = Endpointer(); ep.set_strict(True)
        self.assertEqual(self.correr(ep, [(0.002, 30), (0.06, 10), (0.002, 50)]), [])          # eco débil: ignorado
        ep2 = Endpointer(); ep2.set_strict(True)
        self.assertEqual(self.correr(ep2, [(0.002, 30), (0.2, 20)]), ["start"])                 # voz fuerte: interrumpe


class TestSpeaker(unittest.TestCase):
    def test_habla_en_orden_y_se_calla(self):
        eng, pl, eventos = FakeEngine(0.15), FakePlayer(), []
        sp = Speaker(eng, pl, emit=eventos.append)
        for t in ["uno", "dos", "tres"]:
            sp.enqueue(t, "feliz")
        self.assertTrue(sp.is_speaking()); self.assertTrue(sp.wait_idle(5))
        self.assertEqual(eng.textos, ["uno", "dos", "tres"]); self.assertEqual(pl.reproducidas, 3)
        self.assertIn("hablando", eventos); self.assertEqual(eventos[-1], "callada")

    def test_stop_corta_al_instante(self):
        eng, pl = FakeEngine(0.5), FakePlayer()
        sp = Speaker(eng, pl)
        for t in "abcd":
            sp.enqueue(t)
        time.sleep(0.15)
        t0 = time.time(); sp.stop()
        self.assertTrue(sp.wait_idle(2)); self.assertLess(time.time() - t0, 0.3)
        self.assertGreaterEqual(pl.cortadas, 1); self.assertEqual(pl.reproducidas, 0)
        sp.enqueue("nueva"); self.assertTrue(sp.wait_idle(3)); self.assertEqual(pl.reproducidas, 1)  # sigue funcionando


def montar(delay=0.0, respuesta=None, seg=0.25):
    srv = MockOllama(delay=delay)
    if respuesta:
        srv.respuesta = respuesta
    cfg = Config(HOME / f"c{time.time_ns()}.json")
    cfg.data["ollama_url"] = srv.url; cfg.data["voz"]["silencio_ms"] = 300
    mem = Memory(HOME / f"v{time.time_ns()}.db")
    tools = PCTools(cfg, mem, launcher=lambda t: None)
    brain = Companion(cfg, mem, Ollama(srv.url), tools, idle_delay=0.05)
    ev = []
    lock = threading.Lock()
    emit = lambda t, v=None: (lock.acquire(), ev.append((t, v)), lock.release())
    eng, pl = FakeEngine(seg), FakePlayer()
    sp = Speaker(eng, pl, emit=lambda e: emit("estado", e))
    return srv, cfg, brain, sp, eng, pl, ev, emit


class TestLlamada(unittest.TestCase):
    def test_turno_de_voz_completo(self):
        srv, cfg, brain, sp, eng, pl, ev, emit = montar()
        src = FakeSource()
        vl = VoiceLoop(cfg, brain, sp, FakeTranscriber(["hola, ¿cómo estás?"]), src, emit); vl.start()
        src.silencio(30); src.habla(40); src.silencio(30)
        self.assertTrue(esperar(lambda: ("ia_fin", True) in ev, 8), ev)
        self.assertTrue(sp.wait_idle(5)); time.sleep(0.2)
        tipos = [t for t, _ in ev]
        for esperado in ["usuario", "ia_inicio", "emocion", "ia_delta", "ia_fin"]:
            self.assertIn(esperado, tipos)
        self.assertLess(tipos.index("usuario"), tipos.index("ia_inicio"))
        self.assertEqual(eng.textos[0], "Hola, ¡qué gusto verte!"); self.assertEqual(pl.reproducidas, len(eng.textos))
        self.assertNotIn("[emo", " ".join(eng.textos))
        vl.detener(); vl.join(3); self.assertFalse(vl.is_alive()); self.assertEqual(vl.errores, [])

    def test_interrupcion_mientras_habla(self):
        resp = "[emo:feliz] " + " ".join(f"Esta es la frase número {i} de mi respuesta larga." for i in range(1, 7))
        srv, cfg, brain, sp, eng, pl, ev, emit = montar(respuesta=resp, seg=0.6)
        src = FakeSource()
        tr = FakeTranscriber(["cuéntame algo largo", "espera, mejor otra cosa"])
        vl = VoiceLoop(cfg, brain, sp, tr, src, emit); vl.start()
        src.silencio(30); src.habla(40); src.silencio(30)
        self.assertTrue(esperar(lambda: sp.is_speaking() and pl.reproducidas >= 1, 8))
        t0 = time.time()
        src.habla(25)                                   # la persona habla encima de ella
        self.assertTrue(esperar(lambda: ("interrumpida", True) in ev, 3))
        self.assertTrue(esperar(lambda: pl.cortadas >= 1, 2)); self.assertLess(time.time() - t0, 1.5)
        total = len(eng.textos)
        src.silencio(30)                                # termina de hablar → se atiende la nueva frase
        self.assertTrue(esperar(lambda: tr.llamadas == 2, 6)); self.assertTrue(esperar(lambda: ("ia_fin", True) in ev, 8))
        self.assertIn("espera, mejor otra cosa", [v for t, v in ev if t == "usuario"])
        self.assertLess(total, 6)                       # no leyó toda la respuesta anterior
        vl.detener(); vl.join(3)

    def test_ruido_sin_texto_vuelve_a_escuchar(self):
        srv, cfg, brain, sp, eng, pl, ev, emit = montar()
        src = FakeSource()
        vl = VoiceLoop(cfg, brain, sp, FakeTranscriber([""]), src, emit); vl.start()
        src.silencio(30); src.habla(20); src.silencio(30)
        self.assertTrue(esperar(lambda: ev and ev[-1] == ("estado", "escuchando") and len(ev) >= 3, 5), ev)
        self.assertEqual(eng.textos, []); vl.detener(); vl.join(3)

    def test_permiso_por_voz(self):
        srv, cfg, brain, sp, eng, pl, ev, emit = montar()
        cfg.data["pc"]["apps"] = {}
        lanz = []
        brain.tools._launch = lambda t: lanz.append(t)
        brain.on_confirm = lambda d: emit("confirmar", d)
        src = FakeSource()
        vl = VoiceLoop(cfg, brain, sp, FakeTranscriber(["abre el documento secreto", "sí, hazlo"]), src, emit); vl.start()
        src.silencio(30); src.habla(30); src.silencio(30)
        self.assertTrue(esperar(lambda: any(t == "confirmar" for t, _ in ev), 6), ev)
        src.habla(30); src.silencio(30)                 # responde "sí" por voz
        self.assertTrue(esperar(lambda: ("ia_fin", True) in ev, 8), ev)
        self.assertEqual(len(lanz), 1)
        vl.detener(); vl.join(3)


class TestApiYRuntime(unittest.TestCase):
    def rt(self, modelos=None):
        srv = MockOllama(**({"modelos": modelos} if modelos else {}))
        cfg = Config(HOME / f"a{time.time_ns()}.json")
        cfg.data["ollama_url"] = srv.url
        ev = []
        rt = Runtime(cfg, emit=lambda t, v=None: ev.append((t, v)), con_voz=False)
        return srv, cfg, rt, ev

    def test_enviar_texto_emite_eventos_y_voz(self):
        srv, cfg, rt, ev = self.rt()
        pl = FakePlayer(); rt.speaker = Speaker(FakeEngine(0.1), pl)
        api = Api(rt)
        self.assertTrue(api.enviar("hola Aiko"))
        self.assertTrue(esperar(lambda: ("ia_fin", True) in ev, 6))
        tipos = [t for t, _ in ev]
        self.assertEqual(tipos[:3], ["usuario", "estado", "ia_inicio"]); self.assertIn(("emocion", "feliz"), ev)
        self.assertTrue(rt.speaker.wait_idle(5)); self.assertGreaterEqual(pl.reproducidas, 1)
        self.assertFalse(api.enviar("   "))

    def test_ajustes_con_lista_blanca(self):
        srv, cfg, rt, ev = self.rt()
        api = Api(rt)
        a = api.guardar_ajustes({"nombre": "Mika", "auriculares": True, "inactividad_min": 40.0, "pc": {"apps": {"x": "y"}},
                                 "modelo": "otro", "ollama_url": "http://evil"})
        self.assertEqual((a["nombre"], a["auriculares"], a["inactividad_min"]), ("Mika", True, 40))
        self.assertEqual(cfg["ollama_url"], srv.url); self.assertEqual(cfg["modelo"], "companera"); self.assertNotIn("x", cfg["pc"]["apps"])
        self.assertTrue(Config(cfg.path)["voz"]["auriculares"])                      # persistió en disco
        self.assertTrue(api.alternar_juego()); self.assertTrue(api.alternar_dnd())

    def test_memoria_desde_la_interfaz(self):
        srv, cfg, rt, ev = self.rt()
        api = Api(rt)
        i, _ = rt.mem.add_memory("Le encanta el café con canela", "preferencia", 4)
        self.assertTrue(any("café con canela" in r["texto"] for r in api.recuerdos()))
        self.assertTrue(api.borrar_recuerdo(i)); self.assertNotIn(i, [r["id"] for r in api.recuerdos()])

    def test_preparar_modelos(self):
        srv, cfg, rt, ev = self.rt(modelos=["qwen2.5:7b"])
        av = rt.preparar()
        self.assertEqual(cfg["modelo"], "qwen2.5:7b"); self.assertTrue(av and "crear-modelos" in av[0])
        srv2, cfg2, rt2, _ = self.rt(modelos=["otro:1b"])
        self.assertIn("No encuentro", rt2.preparar()[0])
        srv3, cfg3, rt3, _ = self.rt()
        self.assertEqual(rt3.preparar(), []); self.assertEqual(cfg3["modelo"], "companera")

    def test_sin_microfono_no_se_cae(self):
        srv, cfg, rt, ev = self.rt()
        self.assertFalse(Api(rt).alternar_llamada())          # aquí no hay PortAudio: debe avisar, no romper
        self.assertTrue(any(t == "error" for t, _ in ev))

    def test_iniciativa_habla_y_avisa(self):
        srv, cfg, rt, ev = self.rt()
        rt.speaker = Speaker(FakeEngine(0.1), FakePlayer()); avisos = []
        rt.on_proactivo = avisos.append
        rt._lanzar_iniciativa("saludo", "")
        self.assertIn("proactivo", [t for t, _ in ev]); self.assertTrue(avisos); self.assertTrue(rt.speaker.wait_idle(5))


if __name__ == "__main__":
    unittest.main(verbosity=2)
