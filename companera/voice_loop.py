"""Modo llamada: escucha continua, transcripción, respuesta hablada e interrupción (barge-in)."""
from __future__ import annotations

import threading

from .brain import Companion
from .speech import Endpointer, Speaker, Transcriber


class VoiceLoop(threading.Thread):
    def __init__(self, cfg, brain: Companion, speaker: Speaker | None, transcriber: Transcriber, source, emit):
        super().__init__(daemon=True, name="llamada")
        self.cfg, self.brain, self.speaker, self.tr, self.src, self.emit = cfg, brain, speaker, transcriber, source, emit
        self._parar = threading.Event()
        self._turno: threading.Thread | None = None
        self.errores: list[str] = []
        v = cfg["voz"]
        self.ep = Endpointer(silence_ms=int(v["silencio_ms"]), strict_mult=1.0 if v["auriculares"] else 3.0,
                             strict_ms=240 if v["auriculares"] else 450)

    def detener(self):
        self._parar.set()
        self.barge_in(force=True)

    # --------------------------------------------------------------------
    def barge_in(self, force: bool = False):
        if self.speaker:
            self.speaker.stop()
        if force or not self.brain.waiting_confirmation:
            self.brain.cancel()

    def run(self):
        self.emit("estado", "escuchando")
        while not self._parar.is_set():
            frame = self.src.read(0.1)
            if frame is None:
                continue
            hablando = bool(self.speaker and self.speaker.is_speaking())
            self.ep.set_strict(hablando)
            for ev, audio in self.ep.feed(frame):
                if ev == "start" and (hablando or (self._turno and self._turno.is_alive())):
                    self.barge_in()
                    self.emit("interrumpida", True)
                elif ev == "end":
                    self._atender(audio)

    def _atender(self, audio):
        prev = self._turno
        t = threading.Thread(target=self._turno_fn, args=(audio, prev), daemon=True, name="turno")
        self._turno = t
        t.start()

    def _turno_fn(self, audio, prev):
        try:
            if prev is not None and prev.is_alive():
                prev.join(5)
            self.emit("estado", "pensando")
            texto = self.tr.transcribe(audio)
            if not texto:
                self.emit("estado", "escuchando")
                return
            self.emit("usuario", texto)
            if self.brain.waiting_confirmation:          # respuesta hablada a un permiso
                if not self.brain.answer_confirmation(texto):
                    self.emit("aviso", "No te entendí: ¿sí o no?")
                return
            self.emit("ia_inicio", True)
            hablar = self.speaker is not None and self.cfg["voz"]["hablar_respuestas"]
            emo = {"v": "neutral"}
            self.brain.converse(
                texto,
                on_emotion=lambda e: (emo.__setitem__("v", e), self.emit("emocion", e)),
                on_delta=lambda d: self.emit("ia_delta", d),
                on_sentence=(lambda s: self.speaker.enqueue(s, emo["v"])) if hablar else None,
                on_tool=lambda n, a, r: self.emit("herramienta", {"nombre": n, "resultado": r[:200]}))
            self.emit("ia_fin", True)
            if hablar:
                self.speaker.wait_idle(120)
            self.emit("estado", "escuchando")
        except Exception as e:  # noqa: BLE001
            self.errores.append(str(e))
            self.emit("error", str(e))
            self.emit("estado", "escuchando")
