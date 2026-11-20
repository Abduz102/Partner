"""Configuración, rutas y valores por defecto. Todo se guarda en local (carpeta de datos del usuario)."""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path


def data_dir() -> Path:
    env = os.environ.get("COMPANERA_HOME")
    if env:
        p = Path(env)
    elif os.name == "nt":
        p = Path(os.environ.get("APPDATA", str(Path.home()))) / "Companera"
    else:
        p = Path.home() / ".companera"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _xdg_dirs() -> list[Path]:
    """Carpetas de usuario en Linux, con sus nombres localizados (Documentos, Descargas...)."""
    home, res = Path.home(), []
    f = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "user-dirs.dirs"
    try:
        for linea in f.read_text(encoding="utf-8").splitlines():
            m = re.match(r'^XDG_(DOCUMENTS|DESKTOP|DOWNLOAD|PICTURES|VIDEOS|MUSIC)_DIR="?([^"]+)"?', linea.strip())
            if m:
                res.append(Path(m.group(2).replace("$HOME", str(home))))
    except OSError:
        pass
    return res


def _carpetas_usuario() -> list[str]:
    home = Path.home()
    candidatas = []
    for base in (home, home / "OneDrive"):
        for n in ("Documents", "Desktop", "Downloads", "Pictures", "Videos", "Music", "Escritorio", "Documentos"):
            candidatas.append(base / n)
    if os.name != "nt":
        candidatas = _xdg_dirs() + candidatas
    vistas, res = set(), []
    for c in candidatas:
        if c.is_dir() and str(c) not in vistas:
            vistas.add(str(c))
            res.append(str(c))
    return res


# Aplicaciones que ella puede abrir por nombre. El usuario puede añadir más en config.json
# (solo se abre lo que está en esta lista: nunca nombres arbitrarios inventados por el modelo).
APPS_WINDOWS = {
    "navegador": "msedge",
    "edge": "msedge",
    "chrome": "chrome",
    "firefox": "firefox",
    "bloc de notas": "notepad",
    "calculadora": "calc",
    "paint": "mspaint",
    "explorador": "explorer",
    "terminal": "wt",
    "administrador de tareas": "taskmgr",
    "configuracion": "ms-settings:",
    "spotify": "spotify",
    "steam": "steam://open/main",
    "discord": "discord://",
    "vscode": "code",
    "word": "winword",
    "excel": "excel",
    "powerpoint": "powerpnt",
}

# En Linux cada app es una lista de candidatos: se usa el primero que esté instalado.
APPS_LINUX = {
    "navegador": ["firefox", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "brave-browser"],
    "firefox": ["firefox"],
    "chrome": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"],
    "terminal": ["gnome-terminal", "konsole", "xfce4-terminal", "kitty", "alacritty", "x-terminal-emulator"],
    "calculadora": ["gnome-calculator", "kcalc", "galculator", "mate-calc"],
    "explorador": ["nautilus", "dolphin", "thunar", "nemo", "pcmanfm"],
    "archivos": ["nautilus", "dolphin", "thunar", "nemo", "pcmanfm"],
    "bloc de notas": ["gedit", "kate", "mousepad", "xed", "gnome-text-editor"],
    "monitor del sistema": ["gnome-system-monitor", "plasma-systemmonitor", "xfce4-taskmanager"],
    "configuracion": ["gnome-control-center", "systemsettings", "xfce4-settings-manager"],
    "spotify": ["spotify"],
    "steam": ["steam"],
    "discord": ["discord"],
    "vscode": ["code", "codium"],
}

DEFAULTS: dict = {
    "nombre": "Aiko",
    "usuario": "",
    "ollama_url": "http://127.0.0.1:11434",
    "modelo": "companera",            # creado con el Modelfile sobre qwen2.5:7b
    "modelo_base": "qwen2.5:7b",
    "modelo_ligero": "companera-ligera",  # creado sobre llama3.2 (3B): más rápido
    "modelo_ligero_base": "llama3.2",
    "usar_ligero": False,
    "num_ctx": 4096,
    "temperature": 0.8,
    "keep_alive": "30m",
    "max_historial": 14,
    "voz": {
        "stt_modelo": "base",       # tiny / base / small (small es mejor pero más lento en CPU)
        "stt_idioma": "es",
        "tts_motor": "auto",        # auto | kokoro | sapi | ninguno
        "tts_voz": "ef_dora",       # voz femenina en español de Kokoro
        "tts_velocidad": 1.0,
        "auriculares": False,       # True = interrupción más sensible (sin eco de altavoces)
        "silencio_ms": 900,
        "hablar_respuestas": True,
    },
    "iniciativa": {
        "activa": True,
        "hablar": True,
        "inactividad_min": 25,
        "max_por_dia": 6,
        "silencio_desde": "23:00",
        "silencio_hasta": "08:00",
        "no_molestar": False,
    },
    "pc": {
        "carpetas": [],
        "apps": {},
        "comandos": {},             # nombre -> comando (se ejecutan SOLO con confirmación)
    },
    "modo_juego": False,
    "ventana": {"siempre_visible": True, "ancho": 400, "alto": 700},
    "kokoro_urls": {
        "modelo": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx",
        "voces": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
    },
}


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else data_dir() / "config.json"
        self.data: dict = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self.data = _merge(DEFAULTS, json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                self.data = copy.deepcopy(DEFAULTS)
        if not self.data["pc"]["carpetas"]:
            self.data["pc"]["carpetas"] = _carpetas_usuario()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    def get(self, k, d=None):
        return self.data.get(k, d)

    @property
    def modelo_activo(self) -> str:
        return self.data["modelo_ligero"] if self.data["usar_ligero"] else self.data["modelo"]

    @property
    def apps(self) -> dict:
        base = dict(APPS_WINDOWS) if os.name == "nt" else dict(APPS_LINUX)
        base.update(self.data["pc"].get("apps", {}))
        return base
