"""Línea de comandos: app de escritorio (por defecto), modo texto, diagnóstico y creación de modelos."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

from . import __version__, persona
from .config import Config, data_dir


def crear_modelos(cfg: Config, solo_ligero: bool = False) -> int:
    """Crea en Ollama los modelos personalizados con la personalidad (ollama create)."""
    rc = 0
    pares = [(cfg["modelo_ligero_base"], cfg["modelo_ligero"])]
    if not solo_ligero:
        pares.insert(0, (cfg["modelo_base"], cfg["modelo"]))
    else:
        cfg["usar_ligero"] = True      # equipos de prueba: arranca directamente con llama3.2
        cfg.save()
    for base, nombre in pares:
        mf = data_dir() / f"Modelfile.{nombre}"
        mf.write_text(persona.modelfile(base, cfg["nombre"], cfg["temperature"], cfg["num_ctx"]), encoding="utf-8")
        print(f"→ ollama create {nombre} (sobre {base})")
        try:
            r = subprocess.run(["ollama", "create", nombre, "-f", str(mf)])
            rc |= r.returncode
        except FileNotFoundError:
            print("No encuentro el comando `ollama`. Instálalo desde https://ollama.com/download")
            return 1
    return rc


def diagnostico(cfg: Config) -> int:
    from .llm import Ollama
    from .memory import Memory
    ok = True
    print(f"Compañera {__version__} · datos en {data_dir()}")
    ll = Ollama(cfg["ollama_url"])
    if ll.ping():
        mods = ll.models()
        print(f"[ok] Ollama responde. Modelos: {', '.join(mods) or 'ninguno'}")
        for m in (cfg["modelo"], cfg["modelo_base"]):
            print(f"  {'[ok]' if ll.has_model(m) else '[--]'} {m}")
    else:
        ok = False
        print("[!!] Ollama no responde (abre la app de Ollama o ejecuta `ollama serve`).")
    mem = Memory(data_dir() / "memoria.db")
    print(f"[ok] Memoria: {mem.stats()}")
    mem.close()
    try:
        import sounddevice as sd
        print(f"[ok] Audio: entrada={sd.query_devices(kind='input')['name']} · salida={sd.query_devices(kind='output')['name']}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"[!!] Audio no disponible: {e}")
    try:
        import faster_whisper  # noqa: F401
        print(f"[ok] Whisper (faster-whisper) instalado; modelo '{cfg['voz']['stt_modelo']}' se descarga la 1ª vez")
    except ImportError:
        ok = False
        print("[!!] Falta faster-whisper (pip install -r requirements.txt)")
    from .speech import make_tts
    eng = make_tts(cfg)
    print(f"[{'ok' if eng else '!!'}] Voz de salida: {eng.name if eng else 'ninguna (ejecuta instalar.bat para bajar Kokoro)'}")
    if os.name == "nt":
        try:
            import webview  # noqa: F401
            print("[ok] pywebview instalado (interfaz)")
        except ImportError:
            print("[--] Sin pywebview: se usará el modo navegador (también funciona)")
    else:
        print("[ok] Interfaz: modo navegador (ventana tipo app con Chromium/Chrome si está instalado)")
    return 0 if ok else 1


def modo_texto(cfg: Config) -> int:
    from .runtime import Runtime
    rt = Runtime(cfg, emit=lambda *a, **k: None, con_voz=False)
    if not rt.llm.ensure_running():
        print("Ollama no responde.")
        return 1
    print(f"{cfg['nombre']}: (escribe 'salir' para terminar)\n")
    rt.brain.proactive("saludo", on_delta=lambda d: print(d, end="", flush=True))
    print()
    try:
        while True:
            t = input("\ntú> ").strip()
            if t.lower() in {"salir", "exit", "quit"}:
                break
            if t:
                print(f"{cfg['nombre']}> ", end="")
                rt.brain.converse(t, on_delta=lambda d: print(d, end="", flush=True),
                                  on_tool=lambda n, a, r: print(f"\n  [herramienta {n}: {r[:80]}]\n", end=""))
                print()
    except (EOFError, KeyboardInterrupt):
        pass
    rt.cerrar()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="companera")
    ap.add_argument("--texto", action="store_true", help="conversar por terminal (sin interfaz ni voz)")
    ap.add_argument("--diagnostico", action="store_true", help="comprobar Ollama, audio, voz y memoria")
    ap.add_argument("--crear-modelos", action="store_true", help="crear los modelos personalizados en Ollama")
    ap.add_argument("--ligero", action="store_true", help="con --crear-modelos: solo llama3.2 (para probar en equipos modestos)")
    ap.add_argument("--web", action="store_true", help="interfaz en el navegador (por defecto en Linux/macOS)")
    ap.add_argument("--sin-abrir", action="store_true", help="con --web: no abrir la ventana, solo mostrar la URL")
    ap.add_argument("--puerto", type=int, default=0, help="con --web: puerto fijo (por defecto uno libre)")
    ap.add_argument("--descargar-voz", action="store_true", help="descargar los archivos de voz de Kokoro")
    a = ap.parse_args(argv)
    cfg = Config()
    if a.crear_modelos:
        return crear_modelos(cfg, a.ligero)
    if a.diagnostico:
        return diagnostico(cfg)
    if a.descargar_voz:
        from .setup_voz import descargar
        return descargar(cfg)
    if a.texto:
        return modo_texto(cfg)
    from .app import lanzar, lanzar_web
    if a.web or os.name != "nt":
        return lanzar_web(cfg, abrir=not a.sin_abrir, puerto=a.puerto)
    return lanzar(cfg)
