import sys, threading, time, unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.helpers import *  # noqa
from tests.helpers import entorno_temporal, esperar, MockOllama

HOME = entorno_temporal()
from companera import persona
from companera.brain import Companion, EmotionStripper, SentenceSplitter, parse_si_no, quick_facts
from companera.config import Config
from companera.initiative import Initiative, decidir, en_silencio
from companera.llm import Ollama, OllamaError
from companera.memory import Memory
from companera.tools import PCTools, inline_tool_call


def nuevo(modelos=None, delay=0.0, ahora=None):
    srv = MockOllama(**({"modelos": modelos} if modelos else {}), delay=delay)
    cfg = Config(HOME / f"cfg-{time.time_ns()}.json")
    cfg.data["ollama_url"] = srv.url
    cfg.data["pc"]["carpetas"] = [str(HOME)]
    mem = Memory(HOME / f"m-{time.time_ns()}.db", now=ahora or datetime.now)
    llm = Ollama(srv.url)
    lanz = []
    tools = PCTools(cfg, mem, launcher=lambda t: lanz.append(t), notas_dir=HOME / "notas")
    brain = Companion(cfg, mem, llm, tools, now=ahora or datetime.now, idle_delay=0.05)
    return srv, cfg, mem, llm, tools, brain, lanz


class TestMemoria(unittest.TestCase):
    def setUp(self):
        self.t = datetime(2026, 11, 19, 10, 0)
        self.m = Memory(HOME / f"mm-{time.time_ns()}.db", now=lambda: self.t)

    def test_guardar_buscar_y_dedup(self):
        a = self.m.add_memory("Le gusta jugar Hollow Knight los fines de semana", "preferencia", 4)
        b = self.m.add_memory("Le gusta jugar Hollow Knight los fines de semana.", "preferencia", 3)
        self.assertEqual(a[0], b[0]); self.assertEqual(b[1], "actualizado")
        r = self.m.search("¿a qué me gusta jugar?")
        self.assertTrue(r and "Hollow Knight" in r[0]["texto"])
        self.assertEqual(r[0]["importancia"], 4)  # conserva la mayor importancia

    def test_actualiza_dato_cambiado(self):
        self.m.add_memory("El usuario vive en Medellín", "hecho", 4)
        self.m.add_memory("El usuario vive en Bogotá", "hecho", 4)
        textos = [r["texto"] for r in self.m.all_memories()]
        self.assertEqual(len(textos), 1)

    def test_core_eventos_y_caducidad(self):
        self.m.add_memory("El usuario se llama Camila", "persona", 5)
        self.m.add_memory("Hoy desayunó pan", "hecho", 1)
        self.m.add_memory("Examen de cálculo", "evento", 4, "2026-11-20")
        self.assertEqual([r["texto"] for r in self.m.core()], ["El usuario se llama Camila"])
        self.assertEqual(len(self.m.upcoming(2)), 1)
        self.t += timedelta(days=90)
        self.assertEqual(self.m.maintenance(), 1)   # solo la trivial caduca
        self.assertEqual(len(self.m.all_memories()), 2)

    def test_olvidar_tareas_recordatorios(self):
        i, _ = self.m.add_memory("Odia el cilantro", "preferencia", 3)
        self.assertTrue(self.m.forget(i)); self.assertEqual(self.m.search("cilantro"), [])
        self.m.add_task("Entregar informe de redes", 3, "2026-11-25")
        self.assertEqual(self.m.complete_task("informe redes")["texto"], "Entregar informe de redes")
        self.m.add_reminder("tomar agua", self.t + timedelta(minutes=5))
        self.assertEqual(self.m.due_reminders(), [])
        self.assertEqual(len(self.m.due_reminders(self.t + timedelta(minutes=6))), 1)

    def test_turnos_buscables_y_persistencia(self):
        self.m.add_turn("s1", "user", "Mi perro Toby se enfermó la semana pasada")
        self.assertIn("Toby", self.m.search_turns("cómo está Toby")[0]["texto"])
        p = self.m.path; self.m.close()
        m2 = Memory(p, now=lambda: self.t)
        self.assertEqual(m2.stats()["turnos"], 1)


class TestPiezas(unittest.TestCase):
    def test_splitter_y_emocion(self):
        sp, out = SentenceSplitter(), []
        for ch in ["Hola, qué bueno verte. ", "Hoy ", "me siento con energía! ¿Y tú? Cuéntame ", "algo."]:
            out += sp.feed(ch)
        out += sp.flush()
        self.assertEqual(out[0], "Hola, qué bueno verte."); self.assertTrue(len(out) >= 2)
        es = EmotionStripper()
        self.assertEqual(es.feed("[emo:fe"), (None, "")); self.assertEqual(es.feed("liz] Hola"), ("feliz", "Hola"))
        self.assertEqual(EmotionStripper().feed("Sin etiqueta"), (None, "Sin etiqueta"))

    def test_si_no_y_datos_rapidos(self):
        self.assertTrue(parse_si_no("sí, dale")); self.assertFalse(parse_si_no("mejor no")); self.assertIsNone(parse_si_no("mmm"))
        n, h = quick_facts("me llamo valentina y recuerda que odio el cilantro")
        self.assertEqual(n, "Valentina"); self.assertEqual(len(h), 2)
        self.assertIsNone(quick_facts("me llamo la atención")[0])

    def test_persona_y_modelfile(self):
        mf = persona.modelfile("qwen2.5:7b", "Aiko")
        self.assertTrue(mf.startswith("FROM qwen2.5:7b")); self.assertEqual(mf.count('"""'), 2)
        self.assertIn("PARAMETER num_ctx 4096", mf)
        self.assertEqual(persona.limpiar_para_voz("[emo:feliz] **Hola** 😊 https://x.co/a"), "Hola un enlace")
        self.assertTrue(en_silencio(datetime(2026, 1, 1, 23, 30), "23:00", "08:00"))
        self.assertTrue(en_silencio(datetime(2026, 1, 1, 3, 0), "23:00", "08:00"))
        self.assertFalse(en_silencio(datetime(2026, 1, 1, 12, 0), "23:00", "08:00"))
        self.assertEqual(inline_tool_call('{"name":"listar_tareas","arguments":{}}')[0]["function"]["name"], "listar_tareas")
        self.assertIsNone(inline_tool_call("hola"))


class TestLLMyCerebro(unittest.TestCase):
    def test_cliente(self):
        srv = MockOllama()
        ll = Ollama(srv.url)
        self.assertTrue(ll.ping()); self.assertTrue(ll.has_model("companera")); self.assertFalse(ll.has_model("nada"))
        with self.assertRaises(OllamaError):
            list(ll.chat_stream("nada", [{"role": "user", "content": "x"}]))
        self.assertFalse(Ollama("http://127.0.0.1:9").ping())

    def test_conversacion_completa(self):
        srv, cfg, mem, llm, tools, brain, _ = nuevo()
        emos, deltas, frases = [], [], []
        r = brain.converse("Hola, me llamo Camila", on_emotion=emos.append, on_delta=deltas.append, on_sentence=frases.append)
        self.assertEqual(emos, ["feliz"]); self.assertNotIn("[emo", "".join(deltas) + r)
        self.assertEqual(frases[0], "Hola, ¡qué gusto verte!"); self.assertEqual(cfg["usuario"], "Camila")
        self.assertTrue(any("se llama Camila" in m["texto"] for m in mem.core()))
        p = srv.peticiones[-1]
        self.assertIn("Eres Aiko", p["messages"][0]["content"]); self.assertEqual(p["options"]["num_ctx"], 4096)
        # segundo turno: recuerda y trae el nombre y el historial
        brain.converse("¿cómo me llamo?")
        p2 = srv.peticiones[-1]["messages"]
        self.assertIn("La persona se llama Camila", p2[0]["content"]); self.assertIn("se llama Camila", p2[0]["content"])
        self.assertEqual([m["role"] for m in p2[1:]], ["user", "assistant", "user"])
        self.assertTrue(p2[2]["content"].startswith("[emo:feliz]"))

    def test_herramienta_abrir_app(self):
        srv, cfg, mem, llm, tools, brain, lanz = nuevo()
        cfg.data["pc"]["apps"] = {"spotify": "spotify"}
        usados = []
        r = brain.converse("abre spotify por favor", on_tool=lambda n, a, res: usados.append((n, res)))
        self.assertEqual(lanz, ["spotify"]); self.assertEqual(usados[0][0], "abrir_app"); self.assertIn("Listo", r)
        # la herramienta solo se ofrece cuando hace falta
        brain.converse("cuéntame un chiste")
        self.assertFalse(srv.peticiones[-1].get("tools"))

    def test_json_en_texto_se_ejecuta_y_no_se_habla(self):
        srv, cfg, mem, llm, tools, brain, _ = nuevo()
        mem.add_task("Estudiar redes", 2)
        frases = []
        r = brain.converse("agenda: json en texto, lista mis tareas", on_sentence=frases.append)
        self.assertNotIn('{"name"', " ".join(frases)); self.assertIn("Listo", r)

    def test_permiso_denegado_fuera_de_carpetas(self):
        srv, cfg, mem, llm, tools, brain, lanz = nuevo()
        pedidos = []
        brain.on_confirm = lambda d: (pedidos.append(d), threading.Timer(0.1, lambda: brain.answer_confirmation("no, mejor no")).start())
        brain.converse("abre el documento secreto")
        self.assertTrue(pedidos and "/etc/hostname" in pedidos[0]); self.assertEqual(lanz, [])
        res = srv.peticiones[-1]["messages"][-1]["content"]
        self.assertIn("no autorizó", res)

    def test_permiso_concedido(self):
        srv, cfg, mem, llm, tools, brain, lanz = nuevo()
        brain.on_confirm = lambda d: threading.Timer(0.1, lambda: brain.answer_confirmation(True)).start()
        brain.converse("abre el documento secreto")
        self.assertEqual(len(lanz), 1)

    def test_cancelar_a_mitad(self):
        srv, cfg, mem, llm, tools, brain, _ = nuevo(delay=0.03)
        srv.respuesta = "[emo:feliz] " + "Esto es una respuesta muy larga que sigue y sigue. " * 6
        cancel, frases = threading.Event(), []
        threading.Timer(1.1, cancel.set).start()
        t0 = time.time()
        brain.converse("háblame mucho", on_sentence=frases.append, cancel=cancel)
        self.assertLess(time.time() - t0, 3.0); self.assertGreaterEqual(len(frases), 1)
        self.assertTrue(esperar(lambda: srv.abortadas >= 1, 3))     # Ollama dejó de generar
        brain.converse("ya, gracias")                              # y el siguiente turno funciona
        self.assertEqual(brain._turn_lock.locked(), False)

    def test_extraccion_de_memoria_y_eventos(self):
        t = datetime(2026, 11, 19, 10, 0)
        srv, cfg, mem, llm, tools, brain, _ = nuevo(ahora=lambda: t)
        brain.converse("El sábado es el cumpleaños de mi hermana Laura")
        self.assertTrue(esperar(lambda: len(mem.all_memories()) >= 1, 5), brain.errors)
        textos = [r["texto"] for r in mem.all_memories()]
        self.assertTrue(any("Laura" in x for x in textos)); self.assertFalse(any("ejercicio" in x for x in textos))  # lo trivial no entra
        self.assertEqual(mem.upcoming(3)[0]["fecha_evento"], "2026-11-21")
        brain.converse("hola")  # charla trivial: no dispara extracción
        self.assertEqual(sum(1 for p in srv.peticiones if p.get("format")), 1)

    def test_resumen_de_sesion_y_reinicio(self):
        srv, cfg, mem, llm, tools, brain, _ = nuevo()
        for t in ["hola", "tengo un examen mañana", "estoy nervioso"]:
            brain.converse(t)
        brain.cerrar_sesion(10)
        self.assertIn("examen", mem.recent_summaries(1)[0]["texto"])
        b2 = Companion(cfg, mem, llm, tools, idle_delay=0.05)
        self.assertGreaterEqual(len(b2.hist), 4)                    # retoma la charla reciente
        b2.converse("¿seguimos?")
        self.assertIn("Última charla", srv.peticiones[-1]["messages"][0]["content"])

    def test_modo_juego_sin_herramientas_y_respuesta_corta(self):
        srv, cfg, mem, llm, tools, brain, _ = nuevo()
        cfg.data["modo_juego"] = True
        brain.converse("abre spotify")
        p = srv.peticiones[-1]
        self.assertFalse(p.get("tools")); self.assertIn("MODO JUEGO", p["messages"][0]["content"]); self.assertEqual(p["options"]["num_predict"], 220)

    def test_proactivo_no_guarda_turno_de_usuario(self):
        srv, cfg, mem, llm, tools, brain, _ = nuevo()
        brain.proactive("saludo")
        self.assertEqual([m["role"] for m in brain.hist], ["assistant"])
        self.assertIn("INSTRUCCIÓN INTERNA", srv.peticiones[-1]["messages"][-1]["content"])


class TestHerramientas(unittest.TestCase):
    def test_archivos_notas_recordatorios(self):
        srv, cfg, mem, llm, tools, brain, lanz = nuevo()
        (HOME / "docs").mkdir(exist_ok=True); (HOME / "docs" / "Informe Redes final.txt").write_text("hola mundo")
        self.assertIn("Informe Redes final.txt", tools.run("buscar_archivos", {"consulta": "informe redes"}))
        self.assertIn("Hola mundo".lower(), tools.run("leer_archivo", {"ruta": str(HOME / "docs" / "Informe Redes final.txt")}).lower())
        self.assertIn("Nota guardada", tools.run("crear_nota", {"titulo": "Idea", "texto": "x"}))
        self.assertIn("Recordatorio creado", tools.run("crear_recordatorio", {"texto": "agua", "en_minutos": 10}))
        self.assertIn("Esa fecha ya pasó", tools.run("crear_recordatorio", {"texto": "x", "fecha_hora": "2001-01-01 10:00"}))
        self.assertIn("Tarea #", tools.run("agregar_tarea", {"texto": "Estudiar", "prioridad": 3}))
        self.assertIn("desconocida", tools.run("formatear_disco", {}))
        self.assertIn("No tengo", tools.run("abrir_app", {"nombre": "malware"}))
        self.assertEqual(lanz, [])

    def test_comando_guardado_exige_permiso(self):
        srv, cfg, mem, llm, tools, brain, _ = nuevo()
        cfg.data["pc"]["comandos"] = {"saludo": "echo hola"}
        tools.confirm = lambda d: False
        self.assertIn("no autorizó", tools.run("ejecutar_comando_guardado", {"nombre": "saludo"}))   # confirm por defecto = no
        tools.confirm = lambda d: True
        self.assertIn("hola", tools.run("ejecutar_comando_guardado", {"nombre": "saludo"}))
        self.assertIn("No existe", tools.run("ejecutar_comando_guardado", {"nombre": "rm -rf"}))


class TestIniciativa(unittest.TestCase):
    def setUp(self):
        self.srv, self.cfg, self.mem, self.llm, self.tools, self.brain, _ = nuevo()
        self.noon = datetime(2026, 11, 19, 15, 0)

    def d(self, now=None, idle=3600, ocupada=False, ult=0):
        return decidir(self.cfg, self.mem, now or self.noon, idle, ocupada, ult, 100000)

    def test_reglas(self):
        self.assertEqual(self.d()[0], "charla")
        self.assertIsNone(self.d(idle=60))                                   # hablaron hace poco
        self.assertIsNone(self.d(ocupada=True))
        self.assertIsNone(self.d(now=datetime(2026, 11, 19, 2, 0)))          # horas de silencio
        self.cfg.data["iniciativa"]["no_molestar"] = True
        self.assertIsNone(self.d()); self.cfg.data["iniciativa"]["no_molestar"] = False
        self.mem.meta_set("charlas_fecha", self.noon.date().isoformat()); self.mem.meta_set("charlas_n", 6)
        self.assertIsNone(self.d())                                          # tope diario

    def test_recordatorio_y_evento(self):
        self.mem.add_reminder("llamar a mamá", self.noon - timedelta(minutes=1))
        r = self.d(now=datetime(2026, 11, 20, 2, 0), idle=0)                  # los recordatorios suenan aunque sea de noche
        self.assertEqual(r[:2], ("recordatorio", "llamar a mamá"))
        self.mem.mark_reminder_done(r[2])
        self.mem.add_memory("Examen de redes", "evento", 4, "2026-11-20")
        self.mem.now = lambda: self.noon
        e = self.d(idle=0)
        self.assertEqual(e[0], "evento"); self.mem.meta_set(e[2], "1")
        self.assertIsNone(self.d(idle=0))                                    # no repite el aviso

    def test_hilo_dispara_y_habla(self):
        vistos = []
        ini = Initiative(self.cfg, self.mem, self.brain, lambda t, d: vistos.append((t, d)), lambda: False, intervalo=0.05,
                         now=lambda: self.noon)
        self.mem.add_reminder("beber agua", self.noon - timedelta(minutes=1))
        ini.start()
        self.assertTrue(esperar(lambda: vistos, 3)); ini.detener(); ini.join(2)
        self.assertEqual(vistos[0], ("recordatorio", "beber agua")); self.assertFalse(ini.is_alive())


if __name__ == "__main__":
    unittest.main(verbosity=2)
