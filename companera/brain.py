"""Cerebro: orquesta modelo, memoria, herramientas, emociones y frases para la voz."""
from __future__ import annotations

import json
import queue
import re
import threading
import time
from datetime import datetime

from . import persona
from .llm import Ollama, OllamaError
from .memory import Memory
from .tools import PCTools, inline_tool_call

SI = {"si", "sí", "claro", "dale", "ok", "okay", "vale", "hazlo", "adelante", "confirmo", "afirmativo", "porfa", "listo", "perfecto", "buena", "bueno"}
NO = {"no", "cancela", "cancelar", "para", "detente", "nunca", "nada", "negativo", "mejor", "tampoco", "nel"}


def parse_si_no(texto: str):
    """True / False / None (ambiguo). Ante la duda NO se autoriza."""
    pal = re.findall(r"[a-záéíóúñ]+", texto.lower())
    if not pal:
        return None
    if any(p in NO for p in pal):
        return False
    if any(p in SI for p in pal):
        return True
    return None


class EmotionStripper:
    """Quita la etiqueta [emo:x] del inicio del stream y la devuelve aparte."""

    def __init__(self):
        self.buf, self.done, self.emotion = "", False, None

    def feed(self, chunk: str):
        if self.done:
            return None, chunk
        self.buf += chunk
        s = self.buf.lstrip()
        if s.startswith("["):
            if "]" not in s and len(s) < 24:
                return None, ""          # esperando a que cierre la etiqueta
            m = persona._EMO_RE.match(self.buf)
            self.done = True
            if m:
                self.emotion = persona.normalizar_emocion(m.group(1))
                resto = self.buf[m.end():]
                self.buf = ""
                return self.emotion, resto
        if s == "" and len(self.buf) < 24:
            return None, ""
        self.done = True
        out, self.buf = self.buf, ""
        return None, out

    def flush(self) -> str:
        out, self.buf, self.done = self.buf, "", True
        return out


class SentenceSplitter:
    """Parte el texto en streaming en frases para que la voz empiece cuanto antes."""
    FIN = re.compile(r"(?<=[\.\!\?…])\s+|\n+")

    def __init__(self, min_first: int = 8, min_next: int = 14, max_chunk: int = 110):
        self.buf, self.first = "", True
        self.min_first, self.min_next, self.max_chunk = min_first, min_next, max_chunk

    def _min(self):
        return self.min_first if self.first else self.min_next

    def feed(self, text: str) -> list[str]:
        self.buf += text
        out = []
        while True:
            m = self.FIN.search(self.buf)
            if m and len(self.buf[: m.start()].strip()) >= self._min():
                s, self.buf = self.buf[: m.start()].strip(), self.buf[m.end():]
                if s:
                    out.append(s)
                    self.first = False
                continue
            if m and len(self.buf[: m.start()].strip()) < self._min():
                # frase muy corta: buscar siguiente final
                m2 = self.FIN.search(self.buf, m.end())
                if m2:
                    s, self.buf = self.buf[: m2.start()].strip(), self.buf[m2.end():]
                    out.append(s)
                    self.first = False
                    continue
            if len(self.buf) > self.max_chunk:
                cut = self.buf.rfind(",", 30, self.max_chunk)
                if cut == -1:
                    cut = self.buf.rfind(" ", 30, self.max_chunk)
                if cut != -1:
                    out.append(self.buf[: cut + 1].strip())
                    self.buf = self.buf[cut + 1:]
                    self.first = False
                    continue
            break
        return out

    def flush(self) -> list[str]:
        s, self.buf = self.buf.strip(), ""
        return [s] if s else []


# ---------------------------------------------------------------- extracción rápida de datos
_NOMBRES_NO = {"la", "el", "un", "una", "que", "tu", "mi", "se", "lo", "los", "las", "muy", "asi", "así", "poco", "mucho"}
_RE_NOMBRE = re.compile(r"\b(?:me llamo|mi nombre es|puedes llamarme|ll[aá]mame)\s+([A-Za-zÁÉÍÓÚÑáéíóúñ]{2,20})", re.I)
_RE_RECUERDA = re.compile(r"\b(?:recuerda|no olvides|ten en cuenta)\s+que\s+(.{4,200})", re.I)
_RE_VALE = re.compile(
    r"\b(me llamo|mi nombre|mi cumple|soy |tengo |vivo|trabajo|estudio|me gusta|me encanta|odio|detesto|prefiero|"
    r"quiero|voy a|ma[nñ]ana|el (lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)|examen|entrevista|"
    r"mi (mam[aá]|pap[aá]|madre|padre|hermano|hermana|novi[oa]|pareja|perro|gato|amig[oa]|jefe|jefa|esposo|esposa|hij[oa])|"
    r"recuerda|no olvides|importante|me siento|me pas[oó]|cumplea[nñ]os)\b", re.I)


def quick_facts(texto: str):
    """Datos que se pueden guardar sin gastar el modelo. Devuelve (nombre|None, [(texto,tipo,imp)])."""
    nombre, hechos = None, []
    m = _RE_NOMBRE.search(texto)
    if m and m.group(1).lower() not in _NOMBRES_NO:
        nombre = m.group(1).capitalize()
        hechos.append((f"El usuario se llama {nombre}", "persona", 5))
    m = _RE_RECUERDA.search(texto)
    if m:
        hechos.append((f"El usuario pidió recordar: {m.group(1).strip(' .')}", "hecho", 5))
    return nombre, hechos


def vale_la_pena_extraer(texto: str) -> bool:
    return bool(_RE_VALE.search(texto)) or len(texto.split()) >= 16


ESQUEMA_RECUERDOS = {
    "type": "object",
    "properties": {"recuerdos": {"type": "array", "items": {
        "type": "object",
        "properties": {"texto": {"type": "string"},
                       "tipo": {"type": "string", "enum": ["hecho", "preferencia", "evento", "experiencia", "persona", "objetivo"]},
                       "importancia": {"type": "integer"},
                       "fecha": {"type": ["string", "null"]}},
        "required": ["texto", "tipo", "importancia"]}}},
    "required": ["recuerdos"]}


def _json_en(texto: str):
    a, b = texto.find("{"), texto.rfind("}")
    if a == -1 or b <= a:
        return None
    try:
        return json.loads(texto[a: b + 1])
    except ValueError:
        return None


class Companion:
    def __init__(self, cfg, memory: Memory, llm: Ollama, tools: PCTools, now=datetime.now, idle_delay: float = 8.0):
        self.cfg, self.mem, self.llm, self.tools, self.now = cfg, memory, llm, tools, now
        self.session = memory.new_session()
        self.hist: list[dict] = []
        self.emotion = "neutral"
        self.last_activity = time.time()
        self.idle_delay = idle_delay
        self._turn_lock = threading.Lock()
        self._cancel = threading.Event()
        self.waiting_confirmation = False
        self._confirm_ev = threading.Event()
        self._confirm_ans = False
        self.on_confirm = lambda desc: None      # la app lo conecta a la interfaz/voz
        self.tools.confirm = self.ask_confirmation
        self._xq: queue.Queue = queue.Queue()
        self._xthread = None
        self.errors: list[str] = []
        self.user_turns = 0
        self._restaurar_historial()

    # ---------------------------------------------------------- estado
    def _restaurar_historial(self) -> None:
        for t in self.mem.last_turns(6, max_age_h=3):
            self.hist.append({"role": "user" if t["rol"] == "user" else "assistant", "content": t["texto"]})

    @property
    def modo_juego(self) -> bool:
        return bool(self.cfg["modo_juego"])

    def idle_seconds(self) -> float:
        return time.time() - self.last_activity

    def cancel(self) -> None:
        self._cancel.set()

    # ---------------------------------------------------------- confirmaciones
    def ask_confirmation(self, desc: str) -> bool:
        self._confirm_ev.clear()
        self._confirm_ans = False
        self.waiting_confirmation = True
        try:
            self.on_confirm(desc)
            self._confirm_ev.wait(45)
            return self._confirm_ans
        finally:
            self.waiting_confirmation = False

    def answer_confirmation(self, respuesta) -> bool:
        """Acepta bool o texto hablado. Devuelve True si la respuesta quedó resuelta."""
        if isinstance(respuesta, str):
            respuesta = parse_si_no(respuesta)
            if respuesta is None:
                return False
        self._confirm_ans = bool(respuesta)
        self._confirm_ev.set()
        return True

    # ---------------------------------------------------------- contexto
    def _contexto(self, user_text: str) -> str:
        lineas, usados = [], []
        core = self.mem.core(6)
        vistos = {r["id"] for r in core}
        rel = [r for r in self.mem.search(user_text, 5) if r["id"] not in vistos]
        for r in core + rel:
            usados.append(r["id"])
            f = f" (fecha: {r['fecha_evento']})" if r.get("fecha_evento") else ""
            lineas.append(f"- {r['texto']}{f}")
        for r in self.mem.upcoming(2):
            if r["id"] not in usados:
                lineas.append(f"- PRÓXIMO: {r['texto']} ({r['fecha_evento']})")
        for s in self.mem.recent_summaries(1):
            lineas.append(f"- Última charla ({s['ts'][:10]}): {s['texto']}")
        tareas = self.mem.list_tasks()[:3]
        if tareas:
            lineas.append("- Pendientes de la persona: " + "; ".join(t["texto"] for t in tareas))
        self.mem.touch(usados)
        return "\n".join(lineas)[:1500]

    def _mensajes(self, user_text: str, interno: str | None) -> list[dict]:
        ctx = self._contexto(user_text if not interno else "")
        sistema = persona.prompt_sistema(self.cfg, ctx, self.now(), self.modo_juego)
        hist = list(self.hist[-int(self.cfg["max_historial"]):])
        presupuesto = int(self.cfg["num_ctx"]) * 3 - len(sistema) - 400
        while hist and sum(len(m["content"]) for m in hist) > presupuesto:
            hist.pop(0)
        return [{"role": "system", "content": sistema}] + hist + [{"role": "user", "content": interno or user_text}]

    def _opciones(self) -> dict:
        return {"num_ctx": int(self.cfg["num_ctx"]), "temperature": float(self.cfg["temperature"]),
                "num_predict": 220 if self.modo_juego else 380}

    # ---------------------------------------------------------- conversación
    def converse(self, user_text: str, on_emotion=None, on_delta=None, on_sentence=None, on_tool=None,
                 cancel: threading.Event | None = None, interno: str | None = None) -> str:
        with self._turn_lock:
            self._cancel = cancel or threading.Event()
            cancel = self._cancel
            if interno is None:
                self.last_activity = time.time()
                self.user_turns += 1
                nombre, hechos = quick_facts(user_text)
                if nombre and nombre != self.cfg["usuario"]:
                    self.cfg["usuario"] = nombre
                    self.cfg.save()
                for t, tipo, imp in hechos:
                    self.mem.add_memory(t, tipo, imp)
            msgs = self._mensajes(user_text, interno)
            tools = self.tools.select(user_text, self.modo_juego) if interno is None else []
            emo_hecho = [False]

            def emitir_emocion(e):
                if not emo_hecho[0]:
                    emo_hecho[0] = True
                    self.emotion = e
                    if on_emotion:
                        on_emotion(e)

            visible_total = ""
            for ronda in range(4):
                if cancel.is_set():
                    break
                texto_ronda, calls, cancelado = self._ronda(msgs, tools if ronda < 3 else None, cancel, emitir_emocion,
                                                            on_delta, on_sentence)
                visible_total += texto_ronda
                if cancelado or not calls:
                    break
                msgs.append({"role": "assistant", "content": texto_ronda, "tool_calls": calls})
                for c in calls:
                    f = c.get("function", {})
                    res = self.tools.run(f.get("name", ""), f.get("arguments"))
                    if on_tool:
                        on_tool(f.get("name"), f.get("arguments"), res)
                    msgs.append({"role": "tool", "tool_name": f.get("name"), "content": res[:1500]})
                tools = None if ronda >= 1 else tools
            final = persona.limpiar_para_pantalla(visible_total)
            if not emo_hecho[0]:
                emitir_emocion(persona.inferir_emocion(final))
            if interno is None:
                self.mem.add_turn(self.session, "user", user_text)
                self.hist.append({"role": "user", "content": user_text})
            if final:
                self.mem.add_turn(self.session, "assistant", final)
                self.hist.append({"role": "assistant", "content": f"[emo:{self.emotion}] {final}"})
            self.hist = self.hist[-40:]
            if interno is None and vale_la_pena_extraer(user_text):
                self._encolar_extraccion(user_text, final)
            self.last_activity = time.time()
            return final

    def _ronda(self, msgs, tools, cancel, emitir_emocion, on_delta, on_sentence):
        strip, split = EmotionStripper(), SentenceSplitter()
        raw, calls, retenido = "", [], None
        visible = ""

        def salida(texto):
            nonlocal visible
            if not texto:
                return
            visible += texto
            if on_delta:
                on_delta(texto)
            for s in split.feed(texto):
                s = persona.limpiar_para_voz(s)
                if s and on_sentence:
                    on_sentence(s)

        def procesar(chunk):
            e, resto = strip.feed(chunk)
            if e:
                emitir_emocion(e)
            salida(resto)

        for kind, data in self.llm.chat_stream(self.cfg.modelo_activo, msgs, tools, self._opciones(),
                                               self.cfg["keep_alive"], None, cancel):
            if kind == "text":
                raw += data
                if retenido is None and raw.strip():
                    retenido = bool(tools) and raw.lstrip()[0] in "{<"
                if retenido:
                    continue
                procesar(data)
            elif kind == "tools":
                calls.extend(data)
            if cancel.is_set():
                break
        cancelado = cancel.is_set()
        if retenido and not cancelado:
            inline = inline_tool_call(raw)
            if inline:
                calls = inline
            else:
                procesar(raw)
        if not cancelado:
            salida(strip.flush())
            for s in split.flush():
                s = persona.limpiar_para_voz(s)
                if s and on_sentence:
                    on_sentence(s)
        return visible, calls, cancelado

    # ---------------------------------------------------------- iniciativa
    def proactive(self, tipo: str, dato: str = "", **callbacks) -> str:
        franja = persona.franja(self.now())
        if tipo == "saludo":
            ins = (f"[INSTRUCCIÓN INTERNA, no la menciones] La persona acaba de abrir la aplicación ({franja}). Salúdala en una o dos frases, "
                   "con calidez; si recuerdas algo reciente o pendiente suyo, menciónalo con naturalidad. No preguntes '¿en qué te ayudo?'.")
        elif tipo == "charla":
            ins = ("[INSTRUCCIÓN INTERNA, no la menciones] Hace un rato que no hablan. Inicia una conversación espontánea de UNA o dos frases: "
                   "algo que recuerdes de la persona, una curiosidad, una broma ligera o una pregunta abierta. Sin presionar.")
        elif tipo == "recordatorio":
            ins = f"[INSTRUCCIÓN INTERNA, no la menciones] Avísale con naturalidad de este recordatorio que ella pidió: «{dato}»."
        elif tipo == "evento":
            ins = f"[INSTRUCCIÓN INTERNA, no la menciones] Menciónale con cariño este evento próximo suyo: «{dato}». Una o dos frases."
        else:
            ins = f"[INSTRUCCIÓN INTERNA, no la menciones] {dato}"
        return self.converse("", interno=ins, **callbacks)

    # ---------------------------------------------------------- memoria en segundo plano
    def _encolar_extraccion(self, user_text: str, respuesta: str) -> None:
        self._xq.put((user_text, respuesta, self.now()))
        if self._xthread is None or not self._xthread.is_alive():
            self._xthread = threading.Thread(target=self._bucle_extraccion, daemon=True, name="memoria")
            self._xthread.start()

    def _bucle_extraccion(self) -> None:
        while True:
            try:
                item = self._xq.get(timeout=60)
            except queue.Empty:
                return
            while self.idle_seconds() < self.idle_delay:   # no competir con la conversación por la GPU
                time.sleep(0.5)
            try:
                self.extraer(*item)
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"extracción: {e}")

    def extraer(self, user_text: str, respuesta: str, when: datetime | None = None) -> int:
        when = when or self.now()
        sistema = (
            "Extraes recuerdos duraderos sobre el USUARIO a partir de un intercambio. "
            f"Hoy es {when:%Y-%m-%d} ({persona.DIAS[when.weekday()]}). Responde solo JSON {{\"recuerdos\":[...]}}.\n"
            "Guarda: nombre, familia, amistades, mascotas, gustos, trabajo o estudios, metas, eventos con fecha, experiencias importantes, "
            "estados de ánimo relevantes y persistentes. NO guardes saludos ni charla trivial, ni lo que dice la asistente.\n"
            "Cada recuerdo: una frase en tercera persona ('El usuario...', 'Su hermana Laura...'). importancia 1-5 "
            "(5 esencial: nombre, familia, cumpleaños; 3 útil; 1-2 trivial). fecha: YYYY-MM-DD si es un evento fechado "
            "(resuelve 'mañana' o 'el viernes' con la fecha de hoy), si no null. Si no hay nada: {\"recuerdos\":[]}.")
        msgs = [{"role": "system", "content": sistema},
                {"role": "user", "content": f"Usuario: {user_text}\nAsistente: {respuesta}"}]
        try:
            texto = self.llm.chat(self.cfg.modelo_activo, msgs, {"temperature": 0, "num_predict": 300, "num_ctx": 2048},
                                  self.cfg["keep_alive"], ESQUEMA_RECUERDOS)
        except OllamaError as e:
            self.errors.append(str(e))
            return 0
        datos = _json_en(texto) or {}
        n = 0
        for r in datos.get("recuerdos", []):
            try:
                imp = int(r.get("importancia", 0))
            except (TypeError, ValueError):
                continue
            t = str(r.get("texto", "")).strip()
            if imp >= 3 and 6 <= len(t) <= 220:
                self.mem.add_memory(t, r.get("tipo", "hecho"), imp, r.get("fecha"))
                n += 1
        return n

    def vaciar_extracciones(self, timeout: float = 30) -> None:
        """Procesa lo pendiente (al cerrar la app)."""
        fin = time.time() + timeout
        while not self._xq.empty() and time.time() < fin:
            try:
                item = self._xq.get_nowait()
            except queue.Empty:
                break
            try:
                self.extraer(*item)
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"extracción: {e}")

    def cerrar_sesion(self, timeout: float = 30) -> None:
        self.vaciar_extracciones(timeout)
        self.mem.maintenance()
        if self.user_turns < 3:
            return
        turnos = [t for t in self.hist[-20:]]
        charla = "\n".join(f"{'Usuario' if t['role'] == 'user' else 'Ella'}: {persona.limpiar_para_pantalla(t['content'])}" for t in turnos)
        try:
            r = self.llm.chat(self.cfg.modelo_activo, [
                {"role": "system", "content": "Resume en 2 o 3 frases lo importante de esta conversación: temas, cómo se sentía el usuario y cosas pendientes. Sin saludos."},
                {"role": "user", "content": charla[:3500]}], {"temperature": 0.2, "num_predict": 160, "num_ctx": 2048}, "5m")
            if r.strip():
                self.mem.add_summary(self.session, r.strip())
        except OllamaError as e:
            self.errors.append(str(e))
