# Compañera local (Ollama + Whisper + Kokoro) — Windows 11 y Linux

Compañera artificial con personalidad propia, voz, memoria persistente, avatar anime 2D y control del PC con permisos.
Todo corre **en tu equipo** (sin Docker, sin suscripciones, sin nube). Se construye *sobre* Ollama: no hizo falta recompilar
el código fuente que subiste (`ollama_tar.gz`); la personalidad va en un `Modelfile` y en el prompt dinámico.

## Instalar (una vez)
1. Descomprime esta carpeta en tu SSD.
2. Doble clic en **instalar.bat** (instala Ollama con winget si falta, crea el entorno, baja `qwen2.5:7b` y `llama3.2`,
   crea los modelos `companera` y `companera-ligera`, baja la voz de Kokoro y corre un diagnóstico).
   Necesitas Python 3.11 o 3.12.
3. Doble clic en **iniciar.bat**.

## Uso
- Escribe, o pulsa el micrófono para el **modo llamada**: habla normal; si hablas mientras ella habla, se calla y te escucha (Esc también la corta).
- **Juego**: respuestas de una frase y sin herramientas. **Silencio**: no inicia conversaciones.
- Engranaje: nombre de ella/tuyo, modo ligero (llama3.2), auriculares, y lista de **lo que recuerda** con botón *Olvidar*.
- Cerrar la ventana la cierra; *Minimizar* la deja en la bandeja del sistema.
- Terminal: `texto.bat` (sin voz) · `diagnostico.bat` · `python -m companera --crear-modelos`.

## Dónde está cada requisito
| Requisito | Dónde |
|---|---|
| Personalidad femenina, cariñosa, humor, modo serio, iniciativa | `persona.py` (prompt + Modelfile), `initiative.py` |
| Emociones al hablar | etiqueta `[emo:x]` → expresión del avatar y velocidad de la voz |
| Voz: micrófono, Whisper, Kokoro `ef_dora`, interrupción | `speech.py`, `voice_loop.py` (frases en streaming → poca latencia) |
| Memoria a largo plazo | `memory.py` (SQLite local): hechos con importancia 1-5, fechas, resúmenes de sesión, tareas, recordatorios; lo trivial caduca |
| Avatar anime, minimalista, segundo plano | `ui/index.html` (SVG 2D, sin 3D), bandeja del sistema |
| Control del PC | `tools.py`: lista blanca de apps, buscar/abrir archivos, notas, recordatorios, tareas, web; pide permiso fuera de tus carpetas |

## Seguridad
- Solo abre apps de la lista (`config.json → pc → apps`). No hay borrado ni terminal libre.
- Fuera de tus carpetas, programas ejecutables y comandos guardados (`pc → comandos`) **siempre piden permiso** (botón o “sí/no” por voz). Sin respuesta en 45 s = no.
- Todo se guarda en `%APPDATA%\Companera` (memoria.db, config.json, notas). Nada sale de tu PC salvo la búsqueda web si la pides.

## Rendimiento en tu equipo (i3 7ª gen, RX 580 8 GB, 12 GB RAM)
- `qwen2.5:7b` (~4.7 GB) cabe en los 8 GB de la RX 580 vía Vulkan. Comprueba con `ollama ps` que dice GPU. Si usa CPU o elige la gráfica integrada
  del i3, define la variable de usuario `GGML_VK_VISIBLE_DEVICES` con el índice de la RX 580 (0 o 1) y reinicia Ollama.
- Whisper corre en CPU (la RX 580 no sirve para él): `base` por defecto; `small` entiende mejor pero tarda más.
- Si notas lentitud: activa **Modo ligero** (llama3.2). La memoria se extrae solo cuando llevas unos segundos sin hablar.
- Sin altavoces con eco, marca “Uso auriculares” para interrumpirla con más facilidad.

## Límites honestos
- No pude probar con tus modelos reales ni con micrófono/altavoces (mi entorno no tiene GPU, audio ni red): verifiqué la lógica con un Ollama simulado
  (45 pruebas) y la interfaz en un navegador. La latencia real y la calidad de voz/reconocimiento dependen de tu equipo: ejecuta `diagnostico.bat`.
- “Reconocer mi voz” = entender lo que dices. Distinguir *quién* habla (verificación de hablante) no está incluido.
- La memoria busca por palabras (FTS5), no por significado; un paso futuro es añadir embeddings.
- Un 7B a veces falla al usar herramientas; por eso solo se le ofrecen cuando tu frase lo sugiere y se le avisa si una acción no se realizó.

## Pruebas
`python -m unittest discover -s tests -t .` (necesita `playwright` solo para `test_ui.py`).

## Versión Linux (para probar sin Windows)
```bash
tar xzf companera-linux.tar.gz && cd companera
./instalar.sh --ligero     # solo llama3.2 (~2 GB): la forma más rápida de probar
# ./instalar.sh            # o completo: qwen2.5:7b + llama3.2
./iniciar.sh               # abre la interfaz; ./iniciar.sh --texto para terminal
```
- La interfaz en Linux va en **modo navegador**: levanta un servidor solo en `127.0.0.1` (con token aleatorio) y abre una ventana tipo app con
  Chromium/Chrome si están instalados, o tu navegador. Si prefieres otra, usa `./iniciar.sh --sin-abrir` y abre la URL que imprime. Ctrl+C la cierra y guarda la memoria.
- Micrófono y altavoces los maneja Python (PortAudio), no el navegador: no se piden permisos de micrófono al navegador.
- Voz: Kokoro si se descargó; si no, **espeak-ng** (robótica, solo para probar). Apps y carpetas se detectan solas (carpetas XDG: Documentos, Descargas…).
- GPU en Linux: `ollama ps` debe decir GPU. En AMD antiguas como la RX 580 Ollama usa Vulkan (instala `mesa-vulkan-drivers`); ROCm no la soporta oficialmente.
- Sin sesión gráfica (SSH): `./iniciar.sh --texto`.
# Partner
