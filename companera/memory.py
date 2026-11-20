"""Memoria local persistente (SQLite + búsqueda de texto completo).

- turnos:     todo lo que se dice (historial crudo, buscable)
- recuerdos:  hechos importantes extraídos (con importancia 1-5, tipo y fecha de evento opcional)
- resumenes:  resumen de cada sesión
- tareas / recordatorios / meta
Los recuerdos de importancia >= 3 se conservan; los de 1-2 caducan si no se usan (charla pasajera).
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import unicodedata
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

TIPOS = ("hecho", "preferencia", "evento", "experiencia", "persona", "objetivo", "resumen")

STOP = set(
    "a al algo ante aquel aqui como con cual cuando de del desde donde el ella ellos en entre era eres es esa ese "
    "eso esta este esto fue ha hay la las le les lo los me mi mis mucho muy no nos o para pero por que se si sin "
    "sobre su sus te ti tu tus un una uno unos y yo hoy ayer estoy esta estas somos son fue ser tengo tiene".split()
)
_SUF = ("mente", "iendo", "ando", "aron", "ieron", "ado", "ido", "ar", "er", "ir", "es", "as", "os", "s")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _stem(t: str) -> str:
    for suf in _SUF:
        if t.endswith(suf) and len(t) - len(suf) >= 3:
            return t[: -len(suf)]
    return t


def tokens(s: str) -> list[str]:
    out, vistos = [], set()
    for t in re.findall(r"[a-z0-9]{3,}", norm(s)):
        if t in STOP:
            continue
        t = _stem(t)
        if t not in vistos:
            vistos.add(t)
            out.append(t)
    return out


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class Memory:
    def __init__(self, path, now=datetime.now):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.now = now
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.fts = self._has_fts5()
        self._init()

    # ---------------------------------------------------------------- esquema
    def _has_fts5(self) -> bool:
        try:
            c = sqlite3.connect(":memory:")
            c.execute("CREATE VIRTUAL TABLE t USING fts5(a)")
            c.close()
            return True
        except sqlite3.Error:
            return False

    def _init(self) -> None:
        with self._lock, self.db:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS turnos(id INTEGER PRIMARY KEY, sesion TEXT, rol TEXT, texto TEXT, ts TEXT);
                CREATE TABLE IF NOT EXISTS recuerdos(
                    id INTEGER PRIMARY KEY, tipo TEXT, texto TEXT, busq TEXT, importancia INTEGER,
                    creado TEXT, ultimo_uso TEXT, usos INTEGER DEFAULT 0, fecha_evento TEXT, activo INTEGER DEFAULT 1);
                CREATE TABLE IF NOT EXISTS resumenes(id INTEGER PRIMARY KEY, sesion TEXT, texto TEXT, ts TEXT);
                CREATE TABLE IF NOT EXISTS tareas(id INTEGER PRIMARY KEY, texto TEXT, prioridad INTEGER DEFAULT 2,
                    limite TEXT, hecha INTEGER DEFAULT 0, creada TEXT);
                CREATE TABLE IF NOT EXISTS recordatorios(id INTEGER PRIMARY KEY, texto TEXT, cuando TEXT, hecho INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
                """
            )
            if self.fts:
                self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS recuerdos_fts USING fts5(busq)")
                self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS turnos_fts USING fts5(busq)")

    # ---------------------------------------------------------------- meta
    def meta_get(self, k, default=None):
        with self._lock:
            r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    def meta_set(self, k, v) -> None:
        with self._lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v)))

    # ---------------------------------------------------------------- turnos
    def new_session(self) -> str:
        return self.now().strftime("%Y%m%d-%H%M%S")

    def add_turn(self, sesion: str, rol: str, texto: str) -> None:
        if not texto.strip():
            return
        with self._lock, self.db:
            cur = self.db.execute("INSERT INTO turnos(sesion,rol,texto,ts) VALUES(?,?,?,?)",
                                  (sesion, rol, texto, _iso(self.now())))
            if self.fts:
                self.db.execute("INSERT INTO turnos_fts(rowid,busq) VALUES(?,?)", (cur.lastrowid, norm(texto)))

    def last_turns(self, n: int = 6, max_age_h: float | None = None) -> list[dict]:
        with self._lock:
            rows = self.db.execute("SELECT rol,texto,ts FROM turnos ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        rows = list(reversed(rows))
        if max_age_h is not None and rows:
            if self.now() - datetime.fromisoformat(rows[-1]["ts"]) > timedelta(hours=max_age_h):
                return []
        return [dict(r) for r in rows]

    def search_turns(self, query: str, k: int = 5) -> list[dict]:
        toks = tokens(query)
        if not toks:
            return []
        with self._lock:
            if self.fts:
                q = " OR ".join(f'"{t}"*' for t in toks[:8])
                rows = self.db.execute(
                    "SELECT t.rol,t.texto,t.ts FROM turnos_fts f JOIN turnos t ON t.id=f.rowid "
                    "WHERE turnos_fts MATCH ? ORDER BY bm25(turnos_fts) LIMIT ?", (q, k)).fetchall()
            else:
                like = " OR ".join("texto LIKE ?" for _ in toks[:6])
                rows = self.db.execute(f"SELECT rol,texto,ts FROM turnos WHERE {like} ORDER BY id DESC LIMIT ?",
                                       [f"%{t}%" for t in toks[:6]] + [k]).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- recuerdos
    def add_memory(self, texto: str, tipo: str = "hecho", importancia: int = 3, fecha_evento: str | None = None):
        """Guarda un recuerdo. Devuelve (id, 'nuevo'|'actualizado')."""
        texto = re.sub(r"\s+", " ", texto).strip()
        if len(texto) < 4:
            return None, "ignorado"
        tipo = tipo if tipo in TIPOS else "hecho"
        importancia = max(1, min(5, int(importancia)))
        fecha_evento = self._valid_date(fecha_evento)
        ahora = _iso(self.now())
        with self._lock, self.db:
            dup = self._find_similar(texto)
            if dup is not None:
                self.db.execute(
                    "UPDATE recuerdos SET texto=?, busq=?, importancia=MAX(importancia,?), ultimo_uso=?, "
                    "fecha_evento=COALESCE(?,fecha_evento), activo=1 WHERE id=?",
                    (texto, norm(texto), importancia, ahora, fecha_evento, dup))
                if self.fts:
                    self.db.execute("DELETE FROM recuerdos_fts WHERE rowid=?", (dup,))
                    self.db.execute("INSERT INTO recuerdos_fts(rowid,busq) VALUES(?,?)", (dup, norm(texto)))
                return dup, "actualizado"
            cur = self.db.execute(
                "INSERT INTO recuerdos(tipo,texto,busq,importancia,creado,ultimo_uso,fecha_evento) VALUES(?,?,?,?,?,?,?)",
                (tipo, texto, norm(texto), importancia, ahora, ahora, fecha_evento))
            if self.fts:
                self.db.execute("INSERT INTO recuerdos_fts(rowid,busq) VALUES(?,?)", (cur.lastrowid, norm(texto)))
            return cur.lastrowid, "nuevo"

    @staticmethod
    def _valid_date(s):
        if not s:
            return None
        try:
            return date.fromisoformat(str(s)[:10]).isoformat()
        except ValueError:
            return None

    # Datos de valor único: "El usuario vive en X" reemplaza al anterior en vez de acumular contradicciones.
    _SLOT = re.compile(r"\b(vive en|vive con|trabaja en|trabaja como|trabaja de|estudia|se llama|cumpleanos es|tiene \d+ anos)\b")

    def _slot(self, nt: str):
        m = self._SLOT.search(nt)
        return (nt[: m.start()].strip(), m.group(1)) if m else None

    def _find_similar(self, texto: str):
        nt = norm(texto)
        tk = set(tokens(texto))
        slot = self._slot(nt)
        for r in self.db.execute("SELECT id,busq FROM recuerdos WHERE activo=1").fetchall():
            if slot and self._slot(r["busq"]) == slot:
                return r["id"]
            if SequenceMatcher(None, nt, r["busq"]).ratio() >= 0.85:
                return r["id"]
            otk = set(tokens(r["busq"]))
            if tk and otk and len(tk & otk) / len(tk | otk) >= 0.75:
                return r["id"]
        return None

    def search(self, query: str, k: int = 5, tipos=None) -> list[dict]:
        toks = tokens(query)
        if not toks:
            return []
        with self._lock:
            if self.fts:
                q = " OR ".join(f'"{t}"*' for t in toks[:8])
                rows = self.db.execute(
                    "SELECT r.*, bm25(recuerdos_fts) AS rank FROM recuerdos_fts f JOIN recuerdos r ON r.id=f.rowid "
                    "WHERE recuerdos_fts MATCH ? AND r.activo=1 ORDER BY rank LIMIT 30", (q,)).fetchall()
            else:
                like = " OR ".join("busq LIKE ?" for _ in toks[:6])
                rows = [dict(r, rank=-1.0) for r in self.db.execute(
                    f"SELECT * FROM recuerdos WHERE activo=1 AND ({like}) LIMIT 30", [f"%{t}%" for t in toks[:6]])]
        ahora = self.now()
        res = []
        for r in rows:
            r = dict(r)
            if tipos and r["tipo"] not in tipos:
                continue
            dias = max(0.0, (ahora - datetime.fromisoformat(r["ultimo_uso"])).total_seconds() / 86400)
            r["score"] = -float(r["rank"]) + 0.5 * r["importancia"] + 0.8 * math.exp(-dias / 45)
            res.append(r)
        res.sort(key=lambda x: x["score"], reverse=True)
        return res[:k]

    def core(self, limit: int = 6) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM recuerdos WHERE activo=1 AND importancia>=5 ORDER BY ultimo_uso DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def touch(self, ids) -> None:
        ids = list(ids)
        if not ids:
            return
        with self._lock, self.db:
            self.db.executemany("UPDATE recuerdos SET usos=usos+1, ultimo_uso=? WHERE id=?",
                                [(_iso(self.now()), i) for i in ids])

    def upcoming(self, dias: int = 2) -> list[dict]:
        hoy = self.now().date()
        fin = hoy + timedelta(days=dias)
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM recuerdos WHERE activo=1 AND fecha_evento BETWEEN ? AND ? ORDER BY fecha_evento",
                (hoy.isoformat(), fin.isoformat())).fetchall()
        return [dict(r) for r in rows]

    def all_memories(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM recuerdos WHERE activo=1 ORDER BY importancia DESC, id DESC LIMIT ?",
                                   (limit,)).fetchall()
        return [dict(r) for r in rows]

    def forget(self, mem_id: int) -> bool:
        with self._lock, self.db:
            cur = self.db.execute("DELETE FROM recuerdos WHERE id=?", (mem_id,))
            if self.fts:
                self.db.execute("DELETE FROM recuerdos_fts WHERE rowid=?", (mem_id,))
            return cur.rowcount > 0

    def find_to_forget(self, texto: str, k: int = 3) -> list[dict]:
        return self.search(texto, k=k)

    def maintenance(self, dias_caducidad: int = 60) -> int:
        """Los recuerdos de importancia 1-2 que no se usan en `dias_caducidad` días se desactivan."""
        limite = _iso(self.now() - timedelta(days=dias_caducidad))
        with self._lock, self.db:
            cur = self.db.execute("UPDATE recuerdos SET activo=0 WHERE importancia<=2 AND ultimo_uso<? AND activo=1",
                                  (limite,))
            return cur.rowcount

    # ---------------------------------------------------------------- resúmenes
    def add_summary(self, sesion: str, texto: str) -> None:
        with self._lock, self.db:
            self.db.execute("INSERT INTO resumenes(sesion,texto,ts) VALUES(?,?,?)", (sesion, texto.strip(), _iso(self.now())))

    def recent_summaries(self, n: int = 2) -> list[dict]:
        with self._lock:
            rows = self.db.execute("SELECT texto,ts FROM resumenes ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- tareas
    def add_task(self, texto: str, prioridad: int = 2, limite: str | None = None) -> int:
        with self._lock, self.db:
            cur = self.db.execute("INSERT INTO tareas(texto,prioridad,limite,creada) VALUES(?,?,?,?)",
                                  (texto.strip(), max(1, min(3, int(prioridad))), self._valid_date(limite), _iso(self.now())))
            return cur.lastrowid

    def list_tasks(self, pendientes: bool = True) -> list[dict]:
        with self._lock:
            sql = "SELECT * FROM tareas " + ("WHERE hecha=0 " if pendientes else "") + \
                  "ORDER BY hecha, prioridad DESC, COALESCE(limite,'9999') LIMIT 50"
            return [dict(r) for r in self.db.execute(sql).fetchall()]

    def complete_task(self, ref) -> dict | None:
        with self._lock, self.db:
            row = None
            if str(ref).isdigit():
                row = self.db.execute("SELECT * FROM tareas WHERE id=? AND hecha=0", (int(ref),)).fetchone()
            if row is None:
                nt = tokens(str(ref))
                best, score = None, 0.0
                for r in self.db.execute("SELECT * FROM tareas WHERE hecha=0").fetchall():
                    tk = tokens(r["texto"])
                    if not tk or not nt:
                        continue
                    s = len(set(tk) & set(nt)) / len(set(nt))
                    if s > score:
                        best, score = r, s
                row = best if score >= 0.5 else None
            if row is None:
                return None
            self.db.execute("UPDATE tareas SET hecha=1 WHERE id=?", (row["id"],))
            return dict(row)

    # ---------------------------------------------------------------- recordatorios
    def add_reminder(self, texto: str, cuando: datetime) -> int:
        with self._lock, self.db:
            cur = self.db.execute("INSERT INTO recordatorios(texto,cuando) VALUES(?,?)", (texto.strip(), _iso(cuando)))
            return cur.lastrowid

    def due_reminders(self, now: datetime | None = None) -> list[dict]:
        now = now or self.now()
        with self._lock:
            rows = self.db.execute("SELECT * FROM recordatorios WHERE hecho=0 AND cuando<=? ORDER BY cuando",
                                   (_iso(now),)).fetchall()
        return [dict(r) for r in rows]

    def mark_reminder_done(self, rid: int) -> None:
        with self._lock, self.db:
            self.db.execute("UPDATE recordatorios SET hecho=1 WHERE id=?", (rid,))

    def list_reminders(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM recordatorios WHERE hecho=0 ORDER BY cuando LIMIT 30").fetchall()]

    # ---------------------------------------------------------------- utilidades
    def stats(self) -> dict:
        with self._lock:
            q = lambda s: self.db.execute(s).fetchone()[0]
            return {"recuerdos": q("SELECT COUNT(*) FROM recuerdos WHERE activo=1"), "turnos": q("SELECT COUNT(*) FROM turnos"),
                    "resumenes": q("SELECT COUNT(*) FROM resumenes"), "fts5": self.fts}

    def export_json(self) -> str:
        data = {"recuerdos": self.all_memories(10000), "resumenes": self.recent_summaries(1000),
                "tareas": self.list_tasks(False)}
        return json.dumps(data, ensure_ascii=False, indent=2)

    def close(self) -> None:
        with self._lock:
            self.db.close()
