"""Iniciativa: ella también empieza conversaciones (recordatorios, eventos, charla espontánea), sin ser pesada."""
from __future__ import annotations

import threading
import time
from datetime import datetime


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def en_silencio(now: datetime, desde: str, hasta: str) -> bool:
    n, a, b = now.hour * 60 + now.minute, _hm(desde), _hm(hasta)
    return (a <= n or n < b) if a > b else (a <= n < b)


def decidir(cfg, mem, now: datetime, idle_s: float, ocupada: bool, ult_charla_ts: float, ahora_ts: float):
    """Función pura (fácil de probar). Devuelve None o (tipo, dato, id)."""
    ini = cfg["iniciativa"]
    if ocupada or not ini["activa"] or ini["no_molestar"]:
        return None
    for r in mem.due_reminders(now):                       # los recordatorios que pediste siempre cuentan
        return ("recordatorio", r["texto"], r["id"])
    if en_silencio(now, ini["silencio_desde"], ini["silencio_hasta"]):
        return None
    hoy = now.date().isoformat()
    for ev in mem.upcoming(1):
        clave = f"aviso_evento_{ev['id']}_{hoy}"
        if mem.meta_get(clave) is None:
            cuando = "hoy" if ev["fecha_evento"] == hoy else "mañana"
            return ("evento", f"{ev['texto']} ({cuando})", clave)
    if mem.meta_get("charlas_fecha") != hoy:
        mem.meta_set("charlas_fecha", hoy)
        mem.meta_set("charlas_n", 0)
    n = int(mem.meta_get("charlas_n", 0))
    espera = ini["inactividad_min"] * 60
    if n < ini["max_por_dia"] and idle_s >= espera and ahora_ts - ult_charla_ts >= espera:
        return ("charla", "", None)
    return None


class Initiative(threading.Thread):
    def __init__(self, cfg, mem, brain, lanzar, ocupada, intervalo: float = 30.0, now=datetime.now):
        super().__init__(daemon=True, name="iniciativa")
        self.cfg, self.mem, self.brain, self.lanzar, self.ocupada = cfg, mem, brain, lanzar, ocupada
        self.intervalo, self.now = intervalo, now
        self._parar = threading.Event()
        self.ult_charla = time.time()

    def detener(self):
        self._parar.set()

    def tick(self) -> bool:
        d = decidir(self.cfg, self.mem, self.now(), self.brain.idle_seconds(), self.ocupada(), self.ult_charla, time.time())
        if d is None:
            return False
        tipo, dato, ref = d
        if tipo == "recordatorio":
            self.mem.mark_reminder_done(ref)
        elif tipo == "evento":
            self.mem.meta_set(ref, "1")
        else:
            self.mem.meta_set("charlas_n", int(self.mem.meta_get("charlas_n", 0)) + 1)
            self.ult_charla = time.time()
        self.lanzar(tipo, dato)
        return True

    def run(self):
        while not self._parar.wait(self.intervalo):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - el hilo nunca debe morir
                pass
