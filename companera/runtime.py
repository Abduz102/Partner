"""Ensambla todas las piezas. Lo usan la app de escritorio, el modo texto y las pruebas."""
from __future__ import annotations

import os
import threading
from datetime import datetime

from .brain import Companion
from .config import Config, data_dir
from .initiative import Initiative
from .llm import Ollama
from .memory import Memory
from .tools import PCTools


class Runtime:
    """Un único objeto con todo: config, memoria, LLM, herramientas, cerebro, voz e iniciativa."""

    def __init__(self, cfg: Config | None = None, emit=None, now=datetime.now, con_voz: bool = True):
        self.cfg = cfg or Config()
        self.emit = emit or (lambda *a, **k: None)
        self.mem = Memory(data_dir() / "memoria.db", now=now)
        self.llm = Ollama(self.cfg["ollama_url"])
        self.tools = PCTools(self.cfg, self.mem)
        self.brain = Companion(self.cfg, self.mem, self.llm, self.tools, now=now)
        self.brain.on_confirm = self._pedir_confirmacion
        self.speaker = self.player = self.transcriber = self.loop = self.mic = None
        self.voz_info = "sin voz"
        self.initiative: Initiative | None = None
        self._ocupada = threading.Event()
        self.avisos: list[str] = []
        if con_voz:
            self._init_voz()

    # ------------------------------------------------------------ voz
    def _init_voz(self):
        from .speech import Player, Speaker, make_tts
        try:
            eng = make_tts(self.cfg)
            if eng is None:
                self.avisos.append("No hay motor de voz: descarga Kokoro (`python -m companera --descargar-voz`) o instala " + ("la voz en español de Windows." if os.name == "nt" else "espeak-ng (sudo apt install espeak-ng)."))
                return
            self.player = Player()
            self.speaker = Speaker(eng, self.player, emit=lambda ev: self.emit("estado", {"callada": "escuchando" if self.loop else "inactiva", "hablando": "hablando"}.get(ev, ev)))
            self.voz_info = eng.name
        except Exception as e:  # noqa: BLE001
            self.avisos.append(f"Voz de salida no disponible: {e}")

    def iniciar_llamada(self) -> bool:
        if self.loop is not None:
            return True
        from .speech import MicSource, Transcriber
        from .voice_loop import VoiceLoop
        try:
            self.mic = MicSource()
            self.transcriber = self.transcriber or Transcriber(self.cfg["voz"]["stt_modelo"], self.cfg["voz"]["stt_idioma"], self.cfg["nombre"])
            self.mic.start()
        except Exception as e:  # noqa: BLE001
            self.avisos.append(f"No pude abrir el micrófono: {e}")
            self.emit("error", f"No pude abrir el micrófono: {e}")
            self.mic = None
            return False
        self.loop = VoiceLoop(self.cfg, self.brain, self.speaker, self.transcriber, self.mic, self.emit)
        self.loop.start()
        return True

    def terminar_llamada(self):
        if self.loop is not None:
            self.loop.detener()
            self.loop = None
        if self.mic is not None:
            try:
                self.mic.stop()
            except Exception:  # noqa: BLE001
                pass
            self.mic = None
        self.emit("estado", "inactiva")

    # ------------------------------------------------------------ texto
    def hablar_texto(self, texto: str) -> str:
        """Turno escrito: responde en pantalla y también en voz si está activada."""
        self._ocupada.set()
        try:
            hablar = self.speaker is not None and self.cfg["voz"]["hablar_respuestas"]
            emo = {"v": "neutral"}
            self.emit("estado", "pensando")
            self.emit("ia_inicio", True)
            r = self.brain.converse(
                texto,
                on_emotion=lambda e: (emo.__setitem__("v", e), self.emit("emocion", e)),
                on_delta=lambda d: self.emit("ia_delta", d),
                on_sentence=(lambda s: self.speaker.enqueue(s, emo["v"])) if hablar else None,
                on_tool=lambda n, a, res: self.emit("herramienta", {"nombre": n, "resultado": res[:200]}))
            self.emit("ia_fin", True)
            if not hablar:
                self.emit("estado", "escuchando" if self.loop else "inactiva")
            return r
        finally:
            self._ocupada.clear()

    # ------------------------------------------------------------ iniciativa
    def iniciar_iniciativa(self):
        if self.initiative is None:
            self.initiative = Initiative(self.cfg, self.mem, self.brain, self._lanzar_iniciativa,
                                         lambda: self._ocupada.is_set() or (self.speaker is not None and self.speaker.is_speaking()))
            self.initiative.start()

    def _lanzar_iniciativa(self, tipo: str, dato: str):
        self._ocupada.set()
        try:
            hablar = self.speaker is not None and self.cfg["iniciativa"]["hablar"] and self.cfg["voz"]["hablar_respuestas"]
            emo = {"v": "neutral"}
            self.emit("ia_inicio", True)
            texto = self.brain.proactive(
                tipo, dato,
                on_emotion=lambda e: (emo.__setitem__("v", e), self.emit("emocion", e)),
                on_delta=lambda d: self.emit("ia_delta", d),
                on_sentence=(lambda s: self.speaker.enqueue(s, emo["v"])) if hablar else None)
            self.emit("ia_fin", True)
            self.emit("proactivo", texto)
            cb = getattr(self, "on_proactivo", None)
            if cb and texto:
                cb(texto)
        except Exception as e:  # noqa: BLE001
            self.emit("error", str(e))
        finally:
            self._ocupada.clear()

    def _pedir_confirmacion(self, desc: str):
        self.emit("confirmar", desc)
        if self.speaker is not None:
            self.speaker.enqueue(f"Necesito tu permiso: {desc}. ¿Lo hago?")

    # ------------------------------------------------------------ cierre
    def cerrar(self):
        self.terminar_llamada()
        if self.initiative:
            self.initiative.detener()
        if self.speaker:
            self.speaker.stop()
        try:
            self.brain.cerrar_sesion(25)
        finally:
            self.cfg.save()
            self.mem.close()


def _preparar(self) -> list[str]:
    """Comprueba Ollama y modelos; si falta el modelo personalizado usa el base (la personalidad viaja en el prompt)."""
    avisos: list[str] = []
    if not self.llm.ensure_running():
        return ["Ollama no responde. Abre la aplicación de Ollama (o ejecuta `ollama serve`) y reinicia."]
    cfg = self.cfg
    cand = [cfg["modelo_ligero"], cfg["modelo_ligero_base"]] if cfg["usar_ligero"] else [cfg["modelo"], cfg["modelo_base"]]
    elegido = next((m for m in cand if self.llm.has_model(m)), None)
    if elegido is None:
        return [f"No encuentro el modelo '{cand[-1]}' en Ollama. Ejecuta instalar.bat (o `ollama pull {cand[-1]}`)."]
    if elegido != cand[0]:
        avisos.append(f"Uso '{elegido}' (aún no creaste el modelo personalizado; ejecuta `python -m companera --crear-modelos`).")
        if cfg["usar_ligero"]:
            cfg.data["modelo_ligero"] = elegido
        else:
            cfg.data["modelo"] = elegido
    return avisos


Runtime.preparar = _preparar
