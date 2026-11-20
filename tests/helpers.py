"""Piezas simuladas: un Ollama falso (HTTP real, streaming NDJSON), audio falso y motor de voz falso."""
from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

TEXTO_DEFECTO = "[emo:feliz] Hola, ¡qué gusto verte! Cuéntame cómo va tu día."


def _trozos(texto: str, n: int = 4):
    return [texto[i:i + n] for i in range(0, len(texto), n)]


class MockOllama:
    """Servidor HTTP que imita /api/version, /api/tags y /api/chat (streaming)."""

    def __init__(self, modelos=("companera:latest", "qwen2.5:7b", "llama3.2:latest"), delay: float = 0.0):
        self.modelos, self.delay = list(modelos), delay
        self.peticiones: list[dict] = []
        self.abortadas = 0
        self.respuesta = TEXTO_DEFECTO          # texto por defecto para charla normal
        self.fn = None                          # función opcional payload -> lista de objetos NDJSON
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path == "/api/version":
                    self._json({"version": "0.0-mock"})
                elif self.path == "/api/tags":
                    self._json({"models": [{"name": m} for m in outer.modelos]})
                else:
                    self._json({"error": "nope"}, 404)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                if self.path != "/api/chat":
                    return self._json({"error": "nope"}, 404)
                if payload.get("model") and payload["model"] not in outer.modelos and payload["model"] + ":latest" not in outer.modelos:
                    return self._json({"error": "model not found"}, 404)
                outer.peticiones.append(payload)
                if not payload.get("messages"):
                    return self._json({"done": True, "message": {"role": "assistant", "content": ""}})
                objs = (outer.fn or outer.defecto)(payload)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                try:
                    for o in objs:
                        self.wfile.write((json.dumps(o) + "\n").encode())
                        self.wfile.flush()
                        if outer.delay:
                            time.sleep(outer.delay)
                except (BrokenPipeError, ConnectionResetError):
                    outer.abortadas += 1

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    # ---- comportamiento por defecto
    def defecto(self, p):
        msgs = p["messages"]
        sistema = msgs[0]["content"] if msgs and msgs[0]["role"] == "system" else ""
        ultimo_user = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
        if p.get("format"):                                   # extracción de memoria
            return self._texto(self._extraccion(ultimo_user))
        if sistema.startswith("Resume en 2 o 3 frases"):
            return self._texto("Hablaron del día y de un examen; el usuario estaba algo nervioso.")
        if p.get("tools") and any(m["role"] == "tool" for m in msgs):
            return self._texto("[emo:feliz] Listo, ya lo hice. ¿Algo más?")
        if p.get("tools") and "spotify" in ultimo_user.lower():
            return [{"message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "abrir_app", "arguments": {"nombre": "spotify"}}}]}, "done": False}, {"done": True}]
        if p.get("tools") and "documento secreto" in ultimo_user.lower():
            return [{"message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "abrir_ruta", "arguments": {"ruta": "/etc/hostname"}}}]}, "done": False}, {"done": True}]
        if p.get("tools") and "json en texto" in ultimo_user.lower():
            return self._texto('{"name": "listar_tareas", "arguments": {}}')
        return self._texto(self.respuesta)

    def _texto(self, t):
        return [{"message": {"role": "assistant", "content": c}, "done": False} for c in _trozos(t)] + [{"done": True, "done_reason": "stop"}]

    def _extraccion(self, user):
        if "cumpleaños" in user.lower():
            return json.dumps({"recuerdos": [
                {"texto": "El cumpleaños de su hermana Laura es el 2026-11-21", "tipo": "evento", "importancia": 5, "fecha": "2026-11-21"},
                {"texto": "Hoy hizo ejercicio", "tipo": "hecho", "importancia": 1, "fecha": None}]})
        return json.dumps({"recuerdos": []})

    def cerrar(self):
        self.srv.shutdown()


# ---------------------------------------------------------------- audio falso
class FakeEngine:
    name = "fake"

    def __init__(self, seg=0.25):
        self.seg, self.textos = seg, []

    def synth(self, texto, mult=1.0):
        self.textos.append(texto)
        return np.zeros(int(24000 * self.seg), np.float32), 24000


class FakePlayer:
    def __init__(self):
        self.reproducidas, self.cortadas = 0, 0

    def play_blocking(self, samples, sr, should_stop):
        fin = time.time() + len(samples) / sr
        while time.time() < fin:
            if should_stop():
                self.cortadas += 1
                return
            time.sleep(0.01)
        self.reproducidas += 1


class FakeSource:
    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.rng = np.random.default_rng(1)

    def frame(self, amp):
        return (self.rng.standard_normal(480) * amp).astype(np.float32)

    def silencio(self, n):
        for _ in range(n):
            self.q.put(self.frame(0.002))

    def habla(self, n):
        for _ in range(n):
            self.q.put(self.frame(0.1))

    def read(self, timeout=0.1):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None


class FakeTranscriber:
    def __init__(self, textos):
        self.textos, self.llamadas = list(textos), 0

    def transcribe(self, audio):
        self.llamadas += 1
        return self.textos.pop(0) if self.textos else ""


def entorno_temporal():
    d = tempfile.mkdtemp(prefix="companera-test-")
    os.environ["COMPANERA_HOME"] = d
    return Path(d)


def esperar(cond, timeout=5.0, paso=0.02):
    fin = time.time() + timeout
    while time.time() < fin:
        if cond():
            return True
        time.sleep(paso)
    return False
