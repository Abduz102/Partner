"""Descarga los archivos de voz de Kokoro (≈100 MB, una sola vez) a la carpeta de datos."""
from __future__ import annotations

import urllib.request
from pathlib import Path

from .config import data_dir


def _bajar(url: str, destino: Path) -> bool:
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(destino.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length", 0))
            hecho = 0
            while chunk := r.read(1 << 20):
                f.write(chunk)
                hecho += len(chunk)
                if total:
                    print(f"\r  {destino.name}: {hecho * 100 // total}%", end="", flush=True)
        tmp.replace(destino)
        print()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"\n  No pude descargar {url}\n  Motivo: {e}")
        tmp.unlink(missing_ok=True)
        return False


def descargar(cfg) -> int:
    carpeta = data_dir() / "modelos"
    urls = cfg["kokoro_urls"]
    ok = True
    for clave, nombre in (("modelo", "kokoro-v1.0.int8.onnx"), ("voces", "voices-v1.0.bin")):
        dest = carpeta / nombre
        if dest.exists():
            print(f"[ok] {nombre} ya está")
            continue
        ok &= _bajar(urls[clave], dest)
    if not ok:
        print("\nSi falla, baja los dos archivos a mano desde https://github.com/thewh1teagle/kokoro-onnx/releases "
              f"y ponlos en {carpeta}\nMientras tanto usaré la voz en español de Windows.")
    return 0 if ok else 1
