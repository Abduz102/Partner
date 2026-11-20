#!/usr/bin/env bash
# ./iniciar.sh            -> interfaz (ventana tipo app o navegador)
# ./iniciar.sh --texto    -> conversar por terminal
# ./iniciar.sh --diagnostico
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "Primero ejecuta ./instalar.sh"; exit 1; }
exec .venv/bin/python -m companera "$@"
