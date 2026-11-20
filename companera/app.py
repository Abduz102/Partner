"""Aplicación de escritorio: ventana minimalista (pywebview/WebView2, ya incluido en Windows 11) + bandeja del sistema."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from .runtime import Runtime

UI = Path(__file__).parent / "ui" / "index.html"

AJUSTES_PERMITIDOS = {
    "nombre": ("nombre",), "usuario": ("usuario",), "usar_ligero": ("usar_ligero",),
    "hablar_respuestas": ("voz", "hablar_respuestas"), "auriculares": ("voz", "auriculares"),
    "stt_modelo": ("voz", "stt_modelo"), "iniciativa": ("iniciativa", "activa"),
    "iniciativa_hablar": ("iniciativa", "hablar"), "inactividad_min": ("iniciativa", "inactividad_min"),
}


class Api:
    """Métodos que la interfaz (JavaScript) puede llamar. Todo lo demás es privado (guion bajo)."""

    def __init__(self, rt: Runtime):
        self._rt = rt
        self._win = None
        self._tray = None
        self._on_close = None   # modo navegador: función que apaga todo

    # ---- estado inicial
    def iniciar(self):
        c = self._rt.cfg
        return {"nombre": c["nombre"], "usuario": c["usuario"], "voz": self._rt.voz_info, "juego": bool(c["modo_juego"]),
                "dnd": bool(c["iniciativa"]["no_molestar"]), "llamada": self._rt.loop is not None,
                "ajustes": self.ajustes(), "avisos": list(self._rt.avisos)}

    # ---- conversación
    def enviar(self, texto: str):
        texto = (texto or "").strip()
        if not texto:
            return False
        if self._rt.brain.waiting_confirmation and self._rt.brain.answer_confirmation(texto):
            return True
        if self._rt.speaker:
            self._rt.speaker.stop()
        self._rt.brain.cancel()
        self._rt.emit("usuario", texto)
        threading.Thread(target=self._rt.hablar_texto, args=(texto,), daemon=True, name="texto").start()
        return True

    def interrumpir(self):
        if self._rt.speaker:
            self._rt.speaker.stop()
        self._rt.brain.cancel()
        return True

    def confirmar(self, ok: bool):
        self._rt.brain.answer_confirmation(bool(ok))
        return True

    # ---- modos
    def alternar_llamada(self):
        if self._rt.loop is None:
            ok = self._rt.iniciar_llamada()
        else:
            self._rt.terminar_llamada()
            ok = False
        return ok

    def alternar_juego(self):
        c = self._rt.cfg
        c["modo_juego"] = not c["modo_juego"]
        c.save()
        return bool(c["modo_juego"])

    def alternar_dnd(self):
        c = self._rt.cfg
        c["iniciativa"]["no_molestar"] = not c["iniciativa"]["no_molestar"]
        c.save()
        return bool(c["iniciativa"]["no_molestar"])

    # ---- memoria y ajustes
    def recuerdos(self):
        return [{"id": r["id"], "texto": r["texto"], "tipo": r["tipo"], "importancia": r["importancia"],
                 "fecha": r["fecha_evento"]} for r in self._rt.mem.all_memories(150)]

    def borrar_recuerdo(self, mem_id: int):
        return self._rt.mem.forget(int(mem_id))

    def ajustes(self):
        c = self._rt.cfg
        out = {}
        for k, ruta in AJUSTES_PERMITIDOS.items():
            v = c.data
            for p in ruta:
                v = v[p]
            out[k] = v
        return out

    def guardar_ajustes(self, nuevos: dict):
        c = self._rt.cfg
        for k, v in (nuevos or {}).items():
            ruta = AJUSTES_PERMITIDOS.get(k)
            if not ruta:
                continue
            d = c.data
            for p in ruta[:-1]:
                d = d[p]
            actual = d[ruta[-1]]
            d[ruta[-1]] = type(actual)(v) if actual is not None and not isinstance(actual, str) else v
        c.save()
        if self._rt.transcriber and "stt_modelo" in nuevos:
            self._rt.transcriber.modelo, self._rt.transcriber._m = c["voz"]["stt_modelo"], None
        return self.ajustes()

    # ---- ventana
    def siempre_visible(self, v: bool):
        if self._win is not None:
            self._win.on_top = bool(v)
        self._rt.cfg["ventana"]["siempre_visible"] = bool(v)
        return bool(v)

    def minimizar(self):
        if self._win is None:
            return False
        if self._tray is not None:
            self._win.hide()
        else:
            self._win.minimize()
        return True

    def cerrar(self):
        if self._win is not None:
            self._win.destroy()
        elif self._on_close is not None:
            threading.Timer(0.2, self._on_close).start()
        return True


def _icono():
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((4, 4, 60, 60), fill=(122, 92, 214, 255))
    d.ellipse((18, 24, 28, 38), fill=(255, 255, 255, 255))
    d.ellipse((36, 24, 46, 38), fill=(255, 255, 255, 255))
    d.arc((22, 34, 42, 50), 20, 160, fill=(255, 255, 255, 255), width=3)
    return im


def arranque(rt: Runtime, cfg, emit) -> None:
    """Comprobaciones iniciales + calentar el modelo + iniciativa. Común a ventana y navegador."""
    rt.avisos += rt.preparar()
    emit("avisos", rt.avisos)
    if any("responde" in a or "No encuentro" in a for a in rt.avisos):
        return
    threading.Thread(target=lambda: rt.llm.warmup(cfg.modelo_activo, cfg["keep_alive"]), daemon=True).start()
    rt.iniciar_iniciativa()
    if cfg["iniciativa"]["activa"] and not cfg["iniciativa"]["no_molestar"]:
        rt._lanzar_iniciativa("saludo", "")


def lanzar_web(cfg, abrir: bool = True, puerto: int = 0) -> int:
    """Modo navegador: sirve la interfaz en 127.0.0.1 y la abre como ventana tipo app."""
    from .webserver import WebApp, abrir_ventana
    web = WebApp(None, puerto)
    rt = Runtime(cfg, emit=web.emit)
    api = Api(rt)
    web.api = api
    parar = threading.Event()

    def apagar():
        rt.cerrar()
        web.detener()
        parar.set()

    api._on_close = apagar
    web.servir()
    print(f"\n{cfg['nombre']} está lista en:\n  {web.url}\n(Ctrl+C para cerrar)\n")
    if abrir:
        print("Ventana:", abrir_ventana(web.url))
    threading.Thread(target=arranque, args=(rt, cfg, web.emit), daemon=True).start()
    try:
        while not parar.wait(0.5):
            pass
    except KeyboardInterrupt:
        print("\nCerrando… (guardando memoria)")
        apagar()
    return 0


def lanzar(cfg) -> int:
    try:
        import webview
    except ImportError:
        print("pywebview no está instalado: abro el modo navegador.")
        return lanzar_web(cfg)
    holder: dict = {"win": None}

    def emit(tipo, valor=None):
        w = holder["win"]
        if w is None:
            return
        try:
            w.evaluate_js(f"window.ui && window.ui.evento({json.dumps({'t': tipo, 'v': valor}, ensure_ascii=False)})")
        except Exception:  # noqa: BLE001 - la ventana puede estar cerrándose
            pass

    rt = Runtime(cfg, emit=emit)
    api = Api(rt)
    v = cfg["ventana"]
    win = webview.create_window(cfg["nombre"], url=str(UI), js_api=api, width=v["ancho"], height=v["alto"], min_size=(340, 560),
                                frameless=True, easy_drag=False, on_top=bool(v["siempre_visible"]), background_color="#14121f")
    holder["win"] = api._win = win

    # bandeja del sistema (opcional): permite cerrar la ventana y seguir en segundo plano
    try:
        import pystray

        def mostrar(icon=None, item=None):
            win.show()
            win.restore()

        def salir(icon=None, item=None):
            icon.stop()
            win.destroy()

        tray = pystray.Icon("companera", _icono(), cfg["nombre"],
                            pystray.Menu(pystray.MenuItem("Mostrar", mostrar, default=True), pystray.MenuItem("Salir", salir)))
        tray.run_detached()
        api._tray = tray
        rt.on_proactivo = lambda t: (not win.on_top) and tray.notify(t[:200], cfg["nombre"])
    except Exception as e:  # noqa: BLE001
        rt.avisos.append(f"Sin icono de bandeja ({e}); minimizar usará la barra de tareas.")

    def al_cerrar():
        try:
            win.hide()
        except Exception:  # noqa: BLE001
            pass
        rt.cerrar()
        if api._tray is not None:
            try:
                api._tray.stop()
            except Exception:  # noqa: BLE001
                pass
        return True

    win.events.closing += al_cerrar

    def arrancar():
        arranque(rt, cfg, emit)

    webview.start(arrancar)
    return 0
