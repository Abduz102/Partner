"""Voz: detección de habla (VAD), Whisper (oído), Kokoro/SAPI (voz), reproducción interrumpible."""
from __future__ import annotations

import collections
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

from .config import data_dir
from .memory import norm

SR_IN = 16000
FRAME = 480  # 30 ms a 16 kHz


# =============================================================== VAD / fin de frase
class Endpointer:
    """Detector de inicio/fin de habla por energía con ruido de fondo adaptativo (sin dependencias pesadas)."""

    def __init__(self, sr=SR_IN, frame_ms=30, silence_ms=900, min_speech_ms=240, max_ms=20000, preroll_ms=360,
                 abs_min=0.010, mult=3.0, strict_mult=3.0, strict_ms=450, start_ms=90):
        self.sr, self.fms = sr, frame_ms
        self.silence_ms, self.min_speech_ms, self.max_ms = silence_ms, min_speech_ms, max_ms
        self.abs_min, self.mult, self.strict_mult = abs_min, mult, strict_mult
        self.start_n = max(1, start_ms // frame_ms)
        self.strict_n = max(1, strict_ms // frame_ms)
        self.strict = False
        self.floor: float | None = None
        self._pre = collections.deque(maxlen=max(1, preroll_ms // frame_ms))
        self._reset()

    def _reset(self):
        self.in_speech, self._buf, self._run, self._sil, self._loud = False, [], 0, 0, 0

    def set_strict(self, on: bool):
        self.strict = on

    def threshold(self) -> float:
        base = max(self.abs_min, (self.floor or 0) * self.mult)
        return base * (self.strict_mult if self.strict else 1.0)

    def feed(self, frame: np.ndarray) -> list[tuple]:
        ev: list[tuple] = []
        f = frame.astype(np.float32)
        rms = float(np.sqrt(np.mean(f * f)) + 1e-9)
        if self.floor is None:
            self.floor = rms
        th = self.threshold()
        if not self.in_speech:
            if rms < th:  # silencio: adaptar el ruido de fondo (baja rápido, sube lento)
                self.floor = 0.8 * self.floor + 0.2 * rms if rms < self.floor else 0.97 * self.floor + 0.03 * rms
                self._run = 0
                self._pre.append(f)
                return ev
            self._run += 1
            self._pre.append(f)
            if self._run >= (self.strict_n if self.strict else self.start_n):
                self.in_speech, self._buf, self._sil, self._loud = True, list(self._pre), 0, self._run
                ev.append(("start", None))
            return ev
        self._buf.append(f)
        if rms >= th * 0.6:
            self._sil, self._loud = 0, self._loud + 1
        else:
            self._sil += 1
        total_ms = len(self._buf) * self.fms
        if self._sil * self.fms >= self.silence_ms or total_ms >= self.max_ms:
            habla_ms = self._loud * self.fms   # solo cuenta frames con voz (no el pre-roll ni el silencio)
            audio = np.concatenate(self._buf) if self._buf else np.zeros(0, np.float32)
            self._reset()
            self._pre.clear()
            ev.append(("end", audio) if habla_ms >= self.min_speech_ms else ("discard", None))
        return ev


# =============================================================== entrada de micrófono
class MicSource:
    def __init__(self, device=None):
        import sounddevice as sd
        self.q: queue.Queue = queue.Queue(maxsize=400)
        self.stream = sd.InputStream(samplerate=SR_IN, channels=1, dtype="float32", blocksize=FRAME, device=device,
                                     callback=self._cb)

    def _cb(self, indata, frames, t, status):
        try:
            self.q.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass

    def start(self):
        self.stream.start()

    def stop(self):
        self.stream.stop()
        self.stream.close()

    def read(self, timeout=0.2):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None


# =============================================================== Whisper
_FANTASMAS = ["subtitulos realizados por", "amara org", "gracias por ver", "suscribete", "hasta la proxima",
              "no olvides suscribirte", "subtitulado por"]


class Transcriber:
    def __init__(self, modelo="base", idioma="es", nombre="Aiko"):
        self.modelo, self.idioma, self.nombre = modelo, idioma, nombre
        self._m = None
        self._lock = threading.Lock()

    def _load(self):
        if self._m is None:
            from faster_whisper import WhisperModel
            self._m = WhisperModel(self.modelo, device="cpu", compute_type="int8", cpu_threads=max(2, (os.cpu_count() or 4)))
        return self._m

    @staticmethod
    def es_alucinacion(texto: str) -> bool:
        n = norm(texto)
        if len(n.strip(" .,¿?¡!")) < 2:
            return True
        return len(n.split()) < 9 and any(f in n for f in _FANTASMAS)

    def transcribe(self, audio: np.ndarray) -> str:
        with self._lock:
            m = self._load()
            segs, _ = m.transcribe(audio.astype(np.float32), language=self.idioma, beam_size=1, vad_filter=True,
                                   vad_parameters={"min_silence_duration_ms": 500}, condition_on_previous_text=False,
                                   initial_prompt=f"Conversación cotidiana en español con {self.nombre}.",
                                   no_speech_threshold=0.6)
            partes = [s.text.strip() for s in segs if getattr(s, "no_speech_prob", 0) < 0.7]
        texto = " ".join(p for p in partes if p).strip()
        return "" if self.es_alucinacion(texto) else texto


# =============================================================== TTS
VELOCIDAD_EMO = {"feliz": 1.06, "triste": 0.92, "seria": 0.97, "preocupada": 0.95, "sorprendida": 1.08, "traviesa": 1.04}


class KokoroEngine:
    name = "kokoro"

    def __init__(self, voz="ef_dora", velocidad=1.0, carpeta: Path | None = None):
        self.voz, self.vel = voz, velocidad
        self.dir = Path(carpeta) if carpeta else data_dir() / "modelos"
        self._k = None
        self._lock = threading.Lock()

    def _archivos(self):
        modelo = next((self.dir / n for n in ("kokoro-v1.0.int8.onnx", "kokoro-v1.0.onnx") if (self.dir / n).exists()), None)
        voces = self.dir / "voices-v1.0.bin"
        return modelo, voces

    def available(self) -> bool:
        m, v = self._archivos()
        if not (m and v.exists()):
            return False
        try:
            import kokoro_onnx  # noqa: F401
            return True
        except ImportError:
            return False

    def synth(self, texto: str, mult: float = 1.0):
        with self._lock:
            if self._k is None:
                from kokoro_onnx import Kokoro
                m, v = self._archivos()
                self._k = Kokoro(str(m), str(v))
            samples, sr = self._k.create(texto, voice=self.voz, speed=self.vel * mult, lang="es")
        return np.asarray(samples, dtype=np.float32), int(sr)


_PS = r"""Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'es-*' -and $_.VoiceInfo.Gender -eq 'Female' } | Select-Object -First 1
if ($null -eq $v) { $v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'es-*' } | Select-Object -First 1 }
if ($null -ne $v) { $s.SelectVoice($v.VoiceInfo.Name) }
$s.Rate = [int]$env:COMPANERA_RATE
$s.SetOutputToWaveFile($env:COMPANERA_WAV)
$s.Speak($env:COMPANERA_TXT)
$s.Dispose()
"""


class SapiEngine:
    """Voz de reserva: voces de Windows (Sabina/Helena si el idioma español está instalado). Sin descargas."""
    name = "sapi"

    def __init__(self, velocidad=1.0):
        self.vel = velocidad
        self.script = data_dir() / "sapi.ps1"

    def available(self) -> bool:
        return os.name == "nt"

    def synth(self, texto: str, mult: float = 1.0):
        self.script.write_text(_PS, encoding="utf-8")
        with tempfile.TemporaryDirectory() as d:
            wav = os.path.join(d, "s.wav")
            env = dict(os.environ, COMPANERA_WAV=wav, COMPANERA_TXT=texto,
                       COMPANERA_RATE=str(max(-5, min(5, round((self.vel * mult - 1) * 10)))))
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self.script)],
                           env=env, check=True, capture_output=True, timeout=30, creationflags=0x08000000)
            with wave.open(wav) as w:
                raw, sr, ch = w.readframes(w.getnframes()), w.getframerate(), w.getnchannels()
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        return (a.reshape(-1, ch).mean(axis=1) if ch > 1 else a), sr


class EspeakEngine:
    """Voz de reserva en Linux/macOS: espeak-ng (robótica pero sin descargas). Sirve para probar el flujo completo."""
    name = "espeak"

    def __init__(self, velocidad=1.0):
        self.vel = velocidad
        self.exe = shutil.which("espeak-ng") or shutil.which("espeak")

    def available(self) -> bool:
        return self.exe is not None

    def synth(self, texto: str, mult: float = 1.0):
        with tempfile.TemporaryDirectory() as d:
            wav = os.path.join(d, "s.wav")
            subprocess.run([self.exe, "-v", "es+f3", "-s", str(int(165 * self.vel * mult)), "--stdin", "-w", wav],
                           input=texto.encode("utf-8"), check=True, capture_output=True, timeout=30)
            with wave.open(wav) as w:
                raw, sr = w.readframes(w.getnframes()), w.getframerate()
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0, sr


def make_tts(cfg):
    v = cfg["voz"]
    motor = v["tts_motor"]
    if motor == "ninguno":
        return None
    kokoro = KokoroEngine(v["tts_voz"], v["tts_velocidad"], Path(cfg["voz"].get("kokoro_dir", "")) if cfg["voz"].get("kokoro_dir") else None)
    if motor in ("auto", "kokoro") and kokoro.available():
        return kokoro
    sapi = SapiEngine(v["tts_velocidad"])
    if motor in ("auto", "sapi") and sapi.available():
        return sapi
    esp = EspeakEngine(v["tts_velocidad"])
    if motor in ("auto", "espeak") and esp.available():
        return esp
    return None


# =============================================================== reproducción
class Player:
    """Reproduce audio en bloques pequeños para poder cortarlo al instante (interrupción)."""

    def __init__(self):
        import sounddevice as sd
        self.sd = sd
        self._stream = None
        self._sr = None

    def play_blocking(self, samples: np.ndarray, sr: int, should_stop) -> None:
        if self._stream is None or self._sr != sr:
            if self._stream is not None:
                self._stream.close()
            self._stream = self.sd.OutputStream(samplerate=sr, channels=1, dtype="float32")
            self._stream.start()
            self._sr = sr
        bloque = max(256, sr // 20)
        for i in range(0, len(samples), bloque):
            if should_stop():
                self._stream.abort()
                self._stream.start()
                return
            self._stream.write(np.ascontiguousarray(samples[i:i + bloque].reshape(-1, 1)))

    def close(self):
        if self._stream is not None:
            self._stream.close()
            self._stream = None


class Speaker:
    """Cola de frases → síntesis → reproducción, en un hilo. `stop()` corta todo al instante."""

    def __init__(self, engine, player, emit=None):
        self.engine, self.player = engine, player
        self.emit = emit or (lambda *a, **k: None)
        self._q: queue.Queue = queue.Queue()
        self._gen = 0
        self._pending = 0
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self.errors: list[str] = []
        threading.Thread(target=self._run, daemon=True, name="voz-salida").start()

    def enqueue(self, texto: str, emocion: str | None = None) -> None:
        with self._lock:
            self._pending += 1
            self._idle.clear()
            self._q.put((self._gen, texto, emocion))

    def _hecho(self) -> None:
        with self._lock:
            self._pending -= 1
            if self._pending <= 0:
                self._pending = 0
                self._idle.set()
                self.emit("callada")

    def _run(self) -> None:
        while True:
            gen, texto, emo = self._q.get()
            try:
                if gen == self._gen:
                    samples, sr = self.engine.synth(texto, VELOCIDAD_EMO.get(emo or "", 1.0))
                    if gen == self._gen:
                        self.emit("hablando")
                        self.player.play_blocking(samples, sr, lambda g=gen: g != self._gen)
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"tts: {e}")
            finally:
                self._hecho()

    def stop(self) -> None:
        with self._lock:
            self._gen += 1
            drenados = 0
            while True:
                try:
                    self._q.get_nowait()
                    drenados += 1
                except queue.Empty:
                    break
            self._pending = max(0, self._pending - drenados)
            if self._pending == 0:
                self._idle.set()

    def is_speaking(self) -> bool:
        return not self._idle.is_set()

    def wait_idle(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout)
