"""store.py — estado e cache em SQLite.

A rotina (routine.py) escreve aqui; o app (app.py) só lê. Campos que guardam
estruturas (pontos_chave, fatos, citações) são serializados em JSON dentro de
colunas TEXT. Idempotência: se um video_id já existe, não reprocessamos.
"""
import json
import sqlite3
from datetime import datetime, timezone

from config import db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    source TEXT DEFAULT 'youtube',      -- 'youtube' | 'x' (origem do item)
    channel TEXT, channel_id TEXT,
    original_title TEXT, neutral_title TEXT,
    url TEXT, published_at TEXT, duration TEXT,
    pillar TEXT, score INTEGER, is_clickbait INTEGER,
    resumo TEXT, pontos_chave TEXT, fatos TEXT, citacoes TEXT,  -- JSON em TEXT
    transcript_available INTEGER,
    fetched_at TEXT,
    feedback INTEGER DEFAULT 0,        -- -1 / 0 / +1
    saved_obsidian INTEGER DEFAULT 0,
    saved_anki INTEGER DEFAULT 0,
    downloaded INTEGER DEFAULT 0,
    read INTEGER DEFAULT 0,
    sent_android INTEGER DEFAULT 0,
    transcript_text TEXT,
    channel_thumb TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
-- histórico de perguntas/respostas por vídeo (estilo NotebookLM)
CREATE TABLE IF NOT EXISTS qa (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qa_video ON qa(video_id, id);
-- coleções (playlists / blocos de notas) e seus itens (muitos-para-muitos)
CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collection_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id INTEGER NOT NULL,
    item_type TEXT NOT NULL,        -- 'video' | 'resumo' | 'qa'
    video_id TEXT,                  -- vídeo de origem (sempre presente)
    qa_id INTEGER,                  -- quando item_type='qa'
    note TEXT,                      -- anotação livre opcional do usuário
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ci_coll ON collection_items(collection_id, id);
"""

# Colunas que viajam como JSON (lista/objeto) entre o banco e o resto do app.
_JSON_FIELDS = ("pontos_chave", "fatos", "citacoes")
_FLAGS = ("saved_obsidian", "saved_anki", "downloaded", "read", "sent_android")


def _connect():
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    return conn


def init():
    """Cria o schema se ainda não existir. Seguro chamar sempre."""
    with _connect() as conn:
        conn.executescript(SCHEMA)
        # migração: colunas adicionadas depois (bancos antigos)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(videos)")}
        if "read" not in cols:
            conn.execute("ALTER TABLE videos ADD COLUMN read INTEGER DEFAULT 0")
        if "sent_android" not in cols:
            conn.execute("ALTER TABLE videos ADD COLUMN sent_android INTEGER DEFAULT 0")
        if "transcript_text" not in cols:
            conn.execute("ALTER TABLE videos ADD COLUMN transcript_text TEXT")
        if "channel_thumb" not in cols:
            conn.execute("ALTER TABLE videos ADD COLUMN channel_thumb TEXT")
        if "source" not in cols:
            # bancos antigos: tudo que já existe é do YouTube
            conn.execute("ALTER TABLE videos ADD COLUMN source TEXT DEFAULT 'youtube'")
            conn.execute("UPDATE videos SET source = 'youtube' WHERE source IS NULL")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row):
    d = dict(row)
    for f in _JSON_FIELDS:
        raw = d.get(f)
        try:
            d[f] = json.loads(raw) if raw else []
        except (json.JSONDecodeError, TypeError):
            d[f] = []
    return d


def has_video(video_id):
    with _connect() as conn:
        cur = conn.execute("SELECT 1 FROM videos WHERE video_id = ?", (video_id,))
        return cur.fetchone() is not None


def upsert_video(v):
    """Insere/atualiza um vídeo. `v` é um dict com as chaves do schema;
    pontos_chave/fatos/citacoes podem vir como lista (serializamos)."""
    v = dict(v)
    for f in _JSON_FIELDS:
        if not isinstance(v.get(f), str):
            v[f] = json.dumps(v.get(f) or [], ensure_ascii=False)
    v.setdefault("fetched_at", now_iso())
    v.setdefault("source", "youtube")
    cols = [
        "video_id", "source", "channel", "channel_id", "original_title", "neutral_title",
        "url", "published_at", "duration", "pillar", "score", "is_clickbait",
        "resumo", "pontos_chave", "fatos", "citacoes", "transcript_available",
        "fetched_at", "transcript_text", "channel_thumb",
    ]
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "video_id")
    sql = (
        f"INSERT INTO videos ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(video_id) DO UPDATE SET {updates}"
    )
    with _connect() as conn:
        conn.execute(sql, [v.get(c) for c in cols])


def get_videos(filter_pillar=None, min_score=0, day=None, include_read=False,
               since_iso=None, source=None):
    """Lista vídeos acima do limiar, ordenados por score desc.
    `day` (YYYY-MM-DD) filtra por published_at exato, se informado.
    `since_iso` (ISO) filtra published_at >= since (janela do período).
    `include_read=False` (padrão) oculta os marcados como lidos.
    `source` ('youtube'|'x'), se informado, filtra pela origem."""
    q = "SELECT * FROM videos WHERE score >= ?"
    args = [min_score]
    if source:
        q += " AND COALESCE(source, 'youtube') = ?"
        args.append(source)
    if not include_read:
        q += " AND COALESCE(read, 0) = 0"
    if filter_pillar:
        q += " AND pillar = ?"
        args.append(filter_pillar)
    if day:
        q += " AND substr(published_at, 1, 10) = ?"
        args.append(day)
    if since_iso:
        q += " AND published_at >= ?"
        args.append(since_iso)
    q += " ORDER BY score DESC, published_at DESC"
    with _connect() as conn:
        rows = conn.execute(q, args).fetchall()
    return [_row_to_dict(r) for r in rows]


def count_read(min_score=0):
    """Quantos vídeos acima do limiar foram marcados como lidos."""
    with _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM videos WHERE score >= ? AND COALESCE(read,0) = 1",
            (min_score,),
        ).fetchone()[0]


def get_video(video_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def count_below(min_score, day=None, source=None):
    """Quantos vídeos ficaram ABAIXO do limiar (para o rodapé)."""
    q = "SELECT COUNT(*) FROM videos WHERE score < ?"
    args = [min_score]
    if source:
        q += " AND COALESCE(source, 'youtube') = ?"
        args.append(source)
    if day:
        q += " AND substr(published_at, 1, 10) = ?"
        args.append(day)
    with _connect() as conn:
        return conn.execute(q, args).fetchone()[0]


def count_at_or_above(min_score, day=None, source=None):
    q = "SELECT COUNT(*) FROM videos WHERE score >= ?"
    args = [min_score]
    if source:
        q += " AND COALESCE(source, 'youtube') = ?"
        args.append(source)
    if day:
        q += " AND substr(published_at, 1, 10) = ?"
        args.append(day)
    with _connect() as conn:
        return conn.execute(q, args).fetchone()[0]


def set_flag(video_id, field, value=1):
    if field not in _FLAGS and field != "feedback":
        raise ValueError(f"campo não permitido: {field}")
    with _connect() as conn:
        conn.execute(
            f"UPDATE videos SET {field} = ? WHERE video_id = ?", (value, video_id)
        )


def channel_ids_missing_thumb():
    """Canais de vídeos no banco ainda sem avatar (para backfill na rotina)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT channel_id FROM videos "
            "WHERE COALESCE(channel_thumb, '') = '' AND COALESCE(channel_id, '') != ''"
        ).fetchall()
    return [r[0] for r in rows]


def set_channel_thumb(channel_id, url):
    with _connect() as conn:
        conn.execute(
            "UPDATE videos SET channel_thumb = ? WHERE channel_id = ?",
            (url, channel_id),
        )


def set_transcript_text(video_id, text):
    with _connect() as conn:
        conn.execute(
            "UPDATE videos SET transcript_text = ? WHERE video_id = ?", (text, video_id)
        )


def get_transcript_text(video_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT transcript_text FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()
    return row[0] if row and row[0] else None


# --- histórico de perguntas/respostas por vídeo (NotebookLM-like) -----------
def add_qa(video_id, question, answer):
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO qa (video_id, question, answer, created_at) VALUES (?,?,?,?)",
            (video_id, question, answer, now_iso()),
        )
        return cur.lastrowid


def get_qa(video_id):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, question, answer, created_at FROM qa WHERE video_id = ? ORDER BY id",
            (video_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_qa(qa_id):
    with _connect() as conn:
        conn.execute("DELETE FROM qa WHERE id = ?", (qa_id,))


def get_qa_one(qa_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, video_id, question, answer, created_at FROM qa WHERE id = ?",
            (qa_id,),
        ).fetchone()
    return dict(row) if row else None


# --- coleções (playlists / blocos de notas) ---------------------------------
def create_collection(name):
    name = (name or "").strip() or "Sem título"
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO collections (name, created_at) VALUES (?, ?)", (name, now_iso())
        )
        return cur.lastrowid


def rename_collection(cid, name):
    with _connect() as conn:
        conn.execute("UPDATE collections SET name = ? WHERE id = ?",
                     ((name or "").strip() or "Sem título", cid))


def delete_collection(cid):
    with _connect() as conn:
        conn.execute("DELETE FROM collection_items WHERE collection_id = ?", (cid,))
        conn.execute("DELETE FROM collections WHERE id = ?", (cid,))


def list_collections():
    """Coleções com contagem de itens, mais recentes primeiro."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT c.id, c.name, c.created_at, "
            "  (SELECT COUNT(*) FROM collection_items ci WHERE ci.collection_id = c.id) AS n "
            "FROM collections c ORDER BY c.id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def add_to_collection(cid, item_type, video_id, qa_id=None, note=None):
    """Adiciona um item à coleção. Evita duplicar o mesmo (tipo, vídeo, qa)."""
    with _connect() as conn:
        dup = conn.execute(
            "SELECT id FROM collection_items WHERE collection_id=? AND item_type=? "
            "AND IFNULL(video_id,'')=IFNULL(?, '') AND IFNULL(qa_id,0)=IFNULL(?,0)",
            (cid, item_type, video_id, qa_id),
        ).fetchone()
        if dup:
            return dup[0]
        cur = conn.execute(
            "INSERT INTO collection_items (collection_id, item_type, video_id, qa_id, note, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (cid, item_type, video_id, qa_id, note, now_iso()),
        )
        return cur.lastrowid


def remove_collection_item(item_id):
    with _connect() as conn:
        conn.execute("DELETE FROM collection_items WHERE id = ?", (item_id,))


def get_collection(cid):
    """Coleção com seus itens já resolvidos (título do vídeo, resumo, Q&A)."""
    with _connect() as conn:
        crow = conn.execute(
            "SELECT id, name, created_at FROM collections WHERE id = ?", (cid,)
        ).fetchone()
        if not crow:
            return None
        rows = conn.execute(
            "SELECT id, item_type, video_id, qa_id, note, created_at "
            "FROM collection_items WHERE collection_id = ? ORDER BY id",
            (cid,),
        ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        v = get_video(d["video_id"]) if d.get("video_id") else None
        d["video_title"] = (v or {}).get("neutral_title") or (v or {}).get("original_title") or ""
        d["video_channel"] = (v or {}).get("channel", "")
        d["video_url"] = (v or {}).get("url", "")
        if d["item_type"] == "resumo":
            d["text"] = (v or {}).get("resumo", "")
        elif d["item_type"] == "qa" and d.get("qa_id"):
            qa = get_qa_one(d["qa_id"])
            d["question"] = (qa or {}).get("question", "")
            d["answer"] = (qa or {}).get("answer", "")
        items.append(d)
    out = dict(crow)
    out["items"] = items
    return out


def collections_for_video(video_id):
    """IDs das coleções que contêm algo deste vídeo (para marcar na UI)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT collection_id FROM collection_items WHERE video_id = ?",
            (video_id,),
        ).fetchall()
    return [r[0] for r in rows]


def get_meta(key, default=None):
    with _connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(key, value):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


# --- dados de exemplo (dev) -------------------------------------------------
def seed_mock():
    """Popula o banco com os mesmos vídeos de exemplo do Milestone 1, para
    demonstrar a UI sem precisar rodar o pipeline real. Idempotente."""
    from datetime import timedelta

    today = datetime.now(timezone.utc)
    samples = [
        dict(
            video_id="m1", channel="Huberman Lab", channel_id="UCmock1",
            original_title="ESTE hábito MATINAL vai MUDAR sua vida (cientificamente provado!)",
            neutral_title="Exposição à luz solar nas primeiras horas regula o ritmo circadiano",
            url="https://www.youtube.com/watch?v=m1",
            published_at=today.isoformat(), duration="18 min",
            pillar="saude", score=92, is_clickbait=0,
            resumo=("O vídeo resume evidências sobre exposição à luz natural logo após "
                    "acordar e seu efeito na regulação do cortisol e do sono. Apresenta "
                    "um protocolo simples de 5–10 minutos ao ar livre e discute os "
                    "mecanismos circadianos."),
            pontos_chave=[
                "Buscar 5–10 min de luz solar nos 30–60 min após acordar.",
                "Em dias nublados, aumentar o tempo de exposição.",
                "Evitar óculos escuros nessa janela; janelas filtram parte do espectro útil.",
            ],
            fatos=[
                {"tipo": "basic", "frente": "Quando buscar luz solar para regular o ritmo circadiano?",
                 "verso": "Nos primeiros 30–60 minutos após acordar."},
                {"tipo": "cloze", "texto": "A luz matinal eleva o {{c1::cortisol}} no momento certo do dia."},
            ],
            citacoes=[{"texto": "a luz é o sinal mais forte para o relógio biológico", "timestamp": "04:12"}],
            transcript_available=1,
        ),
        dict(
            video_id="m2", channel="Ben Felix", channel_id="UCmock2",
            original_title="The Truth About Index Funds (what they DON'T tell you)",
            neutral_title="Por que a diversificação global supera a concentração em um único mercado",
            url="https://www.youtube.com/watch?v=m2",
            published_at=(today - timedelta(hours=3)).isoformat(), duration="22 min",
            pillar="investimento", score=88, is_clickbait=0,
            resumo=("Análise sóbria, baseada em dados históricos, sobre os riscos de "
                    "concentrar investimentos em um único país. Defende alocação global "
                    "passiva e discute o viés doméstico como fonte de risco não compensado."),
            pontos_chave=[
                "Viés doméstico expõe a risco específico sem retorno esperado adicional.",
                "Diversificação global reduz a variância sem sacrificar retorno esperado.",
                "Rebalanceamento periódico mantém o perfil de risco pretendido.",
            ],
            fatos=[
                {"tipo": "basic", "frente": "O que é viés doméstico (home bias)?",
                 "verso": "Tendência de concentrar a carteira em ativos do próprio país."},
            ],
            citacoes=[{"texto": "risco não compensado é o que você quer eliminar", "timestamp": "11:30"}],
            transcript_available=1,
        ),
        dict(
            video_id="m3", channel="Janet Lansbury", channel_id="UCmock3",
            original_title="Stop tantrums INSTANTLY with this ONE trick",
            neutral_title="Acolher a emoção da criança antes de buscar a solução durante birras",
            url="https://www.youtube.com/watch?v=m3",
            published_at=(today - timedelta(hours=6)).isoformat(), duration="14 min",
            pillar="paternidade", score=81, is_clickbait=0,
            resumo=("Discute uma abordagem de disciplina positiva para lidar com birras: "
                    "validar o sentimento da criança e manter limites com calma, em vez de "
                    "suprimir a emoção. Inclui exemplos de linguagem para usar no momento."),
            pontos_chave=[
                "Nomear a emoção da criança ajuda a regular o sistema nervoso dela.",
                "Manter o limite com firmeza e sem raiva é o que dá segurança.",
                "Evitar resolver no calor do momento; conectar primeiro.",
            ],
            fatos=[],
            citacoes=[{"texto": "conexão antes de correção", "timestamp": "06:05"}],
            transcript_available=1,
        ),
        dict(
            video_id="m4", channel="Some Macro Channel", channel_id="UCmock4",
            original_title="💥 O CRASH ESTÁ CHEGANDO!! VENDA TUDO AGORA 🚨",
            neutral_title="Comentário sobre indicadores macro recentes e possíveis cenários de mercado",
            url="https://www.youtube.com/watch?v=m4",
            published_at=(today - timedelta(hours=8)).isoformat(), duration="31 min",
            pillar="investimento", score=67, is_clickbait=1,
            resumo=("Panorama de indicadores macroeconômicos recentes com tom alarmista. "
                    "Apesar do sensacionalismo, traz alguns dados de inflação e juros que "
                    "podem servir de contexto. Tratar previsões com ceticismo."),
            pontos_chave=[
                "Dados de inflação recentes citados como contexto.",
                "Previsões de curto prazo têm baixo poder preditivo.",
            ],
            fatos=[],
            citacoes=[],
            transcript_available=1,
        ),
        # abaixo do limiar (60): não aparece na lista, mas conta no rodapé.
        dict(
            video_id="m5", channel="Hype Trading", channel_id="UCmock5",
            original_title="🚀🚀 COMPRE ESSA CRIPTO AGORA OU VAI SE ARREPENDER 🚀🚀",
            neutral_title="Promoção de uma criptomoeda específica com promessas de retorno",
            url="https://www.youtube.com/watch?v=m5",
            published_at=(today - timedelta(hours=10)).isoformat(), duration="9 min",
            pillar="investimento", score=12, is_clickbait=1,
            resumo="Conteúdo promocional sem base analítica, com forte apelo a FOMO.",
            pontos_chave=[], fatos=[], citacoes=[], transcript_available=0,
        ),
    ]
    for s in samples:
        upsert_video(s)
    set_meta("last_run", now_iso())
    print(f"[seed] {len(samples)} vídeos de exemplo inseridos no banco.")


if __name__ == "__main__":
    import sys

    init()
    if "--seed" in sys.argv:
        seed_mock()
    else:
        print(f"Banco em {db_path()} — schema garantido.")
        print(f"Vídeos: {count_at_or_above(0)} | acima de 60: {count_at_or_above(60)}")
