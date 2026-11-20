#!/usr/bin/env bash
# Instalación para Linux (Ubuntu/Debian, Fedora, Arch). Sin Docker.
#   ./instalar.sh            -> modelos completos (qwen2.5:7b + llama3.2)
#   ./instalar.sh --ligero   -> solo llama3.2 (3B, ~2 GB): ideal para probar rápido
set -u
cd "$(dirname "$0")"
LIGERO=0; [ "${1:-}" = "--ligero" ] && LIGERO=1
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

say "1/6 Paquetes del sistema (Python, audio, voz de reserva)"
if command -v apt-get >/dev/null; then
  $SUDO apt-get update -y && $SUDO apt-get install -y python3 python3-venv python3-pip libportaudio2 espeak-ng curl
elif command -v dnf >/dev/null; then
  $SUDO dnf install -y python3 python3-pip portaudio espeak-ng curl
elif command -v pacman >/dev/null; then
  $SUDO pacman -Sy --needed --noconfirm python python-pip portaudio espeak-ng curl
else
  echo "[!] Gestor de paquetes desconocido. Instala a mano: python3 (venv, pip), PortAudio, espeak-ng, curl."
fi
python3 -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info < (3,14) else 1)' || { echo "[!] Necesitas Python 3.10 a 3.13"; exit 1; }

say "2/6 Ollama"
if ! command -v ollama >/dev/null; then
  echo "Ollama no está instalado. Se usará el instalador oficial (curl -fsSL https://ollama.com/install.sh | sh)."
  read -r -p "¿Continuar? [S/n] " r; [ "${r:-S}" = "n" ] && { echo "Instálalo desde https://ollama.com/download y repite."; exit 1; }
  curl -fsSL https://ollama.com/install.sh | sh || { echo "[!] Falló la instalación de Ollama"; exit 1; }
fi
# Si no hay servicio en marcha, lo arrancamos en segundo plano
curl -fs http://127.0.0.1:11434/api/version >/dev/null || { (nohup ollama serve >/tmp/ollama-serve.log 2>&1 &); sleep 4; }

say "3/6 Entorno Python"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip >/dev/null
pip install -r requirements.txt || { echo "[!] Fallo al instalar dependencias"; exit 1; }

say "4/6 Modelos de Ollama"
if [ "$LIGERO" -eq 1 ]; then
  ollama pull llama3.2 && python -m companera --crear-modelos --ligero
else
  ollama pull qwen2.5:7b && ollama pull llama3.2 && python -m companera --crear-modelos
fi

say "5/6 Voz de Kokoro (~100 MB; si falla usará espeak-ng)"
python -m companera --descargar-voz || true

say "6/6 Diagnóstico"
python -m companera --diagnostico || true
echo; echo "Listo. Ejecuta ./iniciar.sh"
