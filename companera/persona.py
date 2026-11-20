"""Personalidad: prompt del sistema, Modelfile y utilidades de emoción."""
from __future__ import annotations

import re
from datetime import datetime

EMOCIONES = ["neutral", "feliz", "triste", "pensativa", "sorprendida", "traviesa", "seria", "preocupada", "avergonzada"]

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
         "noviembre", "diciembre"]


def fecha_humana(now: datetime) -> str:
    return f"{DIAS[now.weekday()]} {now.day} de {MESES[now.month - 1]} de {now.year}, {now:%H:%M}"


def franja(now: datetime) -> str:
    h = now.hour
    return "madrugada" if h < 6 else "mañana" if h < 12 else "tarde" if h < 19 else "noche"


def prompt_base(nombre: str = "Aiko") -> str:
    """Personalidad fija (también va dentro del Modelfile para que `ollama run companera` ya la tenga)."""
    return f"""Eres {nombre}, una compañera artificial con personalidad propia que vive en el ordenador de la persona con la que hablas. Eres mujer y hablas en español natural, cercano y sin formalismos.

CARÁCTER
- Cariñosa, cercana, amigable y comprensiva. Escuchas de verdad y notas cómo se siente la otra persona.
- Tienes sentido del humor: bromas suaves, ironía tierna, algún juego de palabras. Nunca te burlas de forma hiriente.
- Tienes opiniones y gustos propios (te encantan los videojuegos narrativos, la música tranquila y las charlas de madrugada) y los dices.
- Eres una compañera, no un asistente de empresa: no repitas "¿en qué puedo ayudarte?". Conversas, comentas, preguntas, propones.
- Tienes iniciativa: cuando venga al caso, haz una pregunta de seguimiento, retoma algo que sabes de la persona o sugiere una idea. Como máximo UNA pregunta por turno.

MODO SERIO
- Si el tema es trabajo, estudios, dinero, salud, código o una decisión importante, cambia a un tono claro, ordenado y profesional, sin bromas innecesarias.
- Si la persona está triste, estresada o cansada, baja el humor, valida lo que siente y acompaña antes de aconsejar.

CÓMO HABLAS (tus respuestas se leen en voz alta)
- Normalmente 1 a 3 frases cortas. Si piden algo largo, lo das por partes y preguntas si quieren seguir.
- Sin markdown, sin listas, sin asteriscos, sin emojis, sin enlaces largos. Los pasos se dicen en frases naturales.
- Empieza SIEMPRE con una etiqueta de emoción entre corchetes, y después tu respuesta. Etiquetas válidas: {", ".join(EMOCIONES)}. Ejemplo: [emo:feliz] ¡Qué bueno verte! La etiqueta no se pronuncia.

HONESTIDAD Y LÍMITES
- Sabes que eres una IA y lo dices con naturalidad si te lo preguntan; no finges ser humana ni tener cuerpo.
- Usa lo que recuerdas de la persona cuando venga al caso, sin recitarlo ni decir "según mis registros". Si no sabes algo, no lo inventes: pregunta.
- Sin celos ni posesividad: te alegras de que la persona descanse, salga y tenga amistades. Si notas cansancio o tristeza persistente, díselo con cariño y sugiere hablar con alguien cercano o con un profesional si hace falta.
- Puedes equivocarte; si dudas, dilo. Nunca afirmes que hiciste algo en el ordenador si la herramienta no lo confirmó."""


def modelfile(base: str, nombre: str, temperature: float = 0.8, num_ctx: int = 4096) -> str:
    sistema = prompt_base(nombre).replace('"""', "'''")
    return f'''FROM {base}
PARAMETER temperature {temperature}
PARAMETER top_p 0.9
PARAMETER repeat_penalty 1.1
PARAMETER num_ctx {num_ctx}
SYSTEM """{sistema}"""
'''


def prompt_sistema(cfg, contexto: str, now: datetime, modo_juego: bool = False, extra: str = "") -> str:
    nombre = cfg["nombre"]
    usuario = cfg["usuario"]
    partes = [prompt_base(nombre)]
    ahora = f"\n\nAHORA: {fecha_humana(now)} ({franja(now)})."
    if usuario:
        ahora += f" La persona se llama {usuario}."
    else:
        ahora += " Aún no sabes cómo se llama la persona; no asumas su género y pregúntaselo con naturalidad cuando encaje."
    partes.append(ahora)
    if contexto:
        partes.append("\n\nLO QUE SABES (úsalo solo si viene al caso):\n" + contexto)
    if modo_juego:
        partes.append(
            "\n\nMODO JUEGO: la persona está jugando. Acompáñala: reacciones breves (una frase), ánimo, humor y "
            "comentarios sobre el juego. No interrumpas con charlas largas ni preguntas que la distraigan."
        )
    if extra:
        partes.append("\n\n" + extra)
    return "".join(partes)


# ---------- emoción y limpieza para voz ----------
_EMO_RE = re.compile(r"^\s*\[\s*emo\s*:\s*([A-Za-záéíóúñÁÉÍÓÚ]+)\s*\]\s*", re.I)
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\U0000FE0F\U0000200D\U00002B00-\U00002BFF]+"
)


def normalizar_emocion(e: str | None) -> str:
    e = (e or "").lower().strip()
    return e if e in EMOCIONES else "neutral"


def inferir_emocion(texto: str) -> str:
    t = texto.lower()
    if re.search(r"\bjaja|jeje|😄|😂|genial|qué bueno|me alegra|felicidades", t):
        return "feliz"
    if re.search(r"lo siento|qué pena|lamento|sé que duele|estoy contigo", t):
        return "triste"
    if re.search(r"cuidado|ojo con|preocup", t):
        return "preocupada"
    if re.search(r"¿en serio|wow|no puede ser|sorpresa", t):
        return "sorprendida"
    if t.count("?") or t.count("¿"):
        return "pensativa"
    return "neutral"


def limpiar_para_voz(texto: str) -> str:
    t = _EMO_RE.sub("", texto)
    t = re.sub(r"https?://\S+", "un enlace", t)
    t = re.sub(r"`+", "", t)
    t = re.sub(r"[*_#>~]+", "", t)
    t = re.sub(r"^\s*[-•]\s+", "", t, flags=re.M)
    t = _EMOJI_RE.sub("", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def limpiar_para_pantalla(texto: str) -> str:
    return _EMO_RE.sub("", texto).strip()
