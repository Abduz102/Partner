"""Cliente mínimo de la API de Ollama (solo biblioteca estándar, con streaming cancelable)."""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request


class OllamaError(RuntimeError):
    pass


class Ollama:
    def __init__(self, url: str = "http://127.0.0.1:11434", timeout: float = 180.0):
        self.url = url.rstrip("/")
        self.timeout = timeout

    # ------------------------------------------------------------ bajo nivel
    def _open(self, path: str, payload: dict | None = None, timeout: float | None = None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self.url + path, data=data, method="POST" if payload is not None else "GET",
                                     headers={"Content-Type": "application/json"})
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as e:
            cuerpo = e.read().decode("utf-8", "replace")[:300]
            if e.code == 404:
                raise OllamaError(f"Modelo no encontrado en Ollama ({cuerpo}). Ejecuta instalar.bat o `ollama pull`.") from e
            raise OllamaError(f"Ollama respondió {e.code}: {cuerpo}") from e
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            raise OllamaError(f"No puedo conectar con Ollama en {self.url}: {e}") from e

    # ------------------------------------------------------------ estado
    def ping(self) -> bool:
        try:
            with self._open("/api/version", timeout=3) as r:
                return r.status == 200
        except OllamaError:
            return False

    def ensure_running(self, espera: float = 20.0) -> bool:
        """Si Ollama no responde, intenta arrancarlo en segundo plano (Windows/Linux/macOS)."""
        if self.ping():
            return True
        try:
            flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
            subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=flags)
        except (OSError, ValueError):
            return False
        fin = time.time() + espera
        while time.time() < fin:
            if self.ping():
                return True
            time.sleep(0.5)
        return False

    def models(self) -> list[str]:
        with self._open("/api/tags", timeout=10) as r:
            return [m["name"] for m in json.load(r).get("models", [])]

    def has_model(self, name: str) -> bool:
        try:
            lista = self.models()
        except OllamaError:
            return False
        return name in lista or f"{name}:latest" in lista or any(m.split(":")[0] == name for m in lista if ":" not in name)

    # ------------------------------------------------------------ chat
    def chat_stream(self, model, messages, tools=None, options=None, keep_alive="30m", fmt=None, cancel=None):
        """Generador de eventos: ('text', str) | ('tools', [calls]) | ('done', dict)."""
        payload = {"model": model, "messages": messages, "stream": True, "keep_alive": keep_alive}
        if tools:
            payload["tools"] = tools
        if options:
            payload["options"] = options
        if fmt:
            payload["format"] = fmt
        resp = self._open("/api/chat", payload)
        try:
            for raw in resp:
                if cancel is not None and cancel.is_set():
                    return
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if "error" in obj:
                    raise OllamaError(str(obj["error"]))
                msg = obj.get("message") or {}
                if msg.get("content"):
                    yield "text", msg["content"]
                if msg.get("tool_calls"):
                    yield "tools", msg["tool_calls"]
                if obj.get("done"):
                    yield "done", obj
                    return
        finally:
            resp.close()  # cerrar la conexión hace que Ollama pare de generar

    def chat(self, model, messages, options=None, keep_alive="30m", fmt=None, cancel=None) -> str:
        return "".join(d for k, d in self.chat_stream(model, messages, None, options, keep_alive, fmt, cancel) if k == "text")

    def warmup(self, model: str, keep_alive="30m") -> None:
        """Carga el modelo en memoria para que la primera respuesta no tarde."""
        try:
            with self._open("/api/chat", {"model": model, "messages": [], "keep_alive": keep_alive}, timeout=120):
                pass
        except OllamaError:
            pass

    def unload(self, model: str) -> None:
        try:
            with self._open("/api/chat", {"model": model, "messages": [], "keep_alive": 0}, timeout=10):
                pass
        except OllamaError:
            pass
