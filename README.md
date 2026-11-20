# Compañera local (Ollama + Whisper + Kokoro) — Windows 11 y Linux

Compañera artificial con personalidad propia, voz, memoria persistente, avatar anime 2D y control del PC con permisos.
Todo corre **en tu equipo** (sin Docker, sin suscripciones, sin nube). Se construye *sobre* Ollama.

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

## Versión Linux (para probar sin Windows)
Aun no tenemos la version en linux, estamos trabajando en eso.