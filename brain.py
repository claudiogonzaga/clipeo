"""brain.py — o coração. UMA chamada de LLM por vídeo produz um objeto
estruturado: pilar, score, título neutro, resumo, pontos-chave, fatos para o
Anki e citações.

Plugável por provedor. Hoje há dois "cérebros":
  - gemini    (ATIVO)    — Google google-genai, modelo gemini-3.5-flash
  - anthropic (desligado) — SDK oficial, família Claude Haiku

O prompt (regras + perfis dos pilares) e o parser são compartilhados; só a
chamada à API muda por provedor. Para trocar de cérebro, edite llm.provider em
config.yaml. Adicionar um terceiro provedor = registrar uma função em PROVIDERS.
"""
import json
import os

from config import load

# ----------------------------------------------------------------------------
SYSTEM_RULES = """Você é um curador a serviço dos objetivos de vida do usuário (os "pilares" \
descritos abaixo). Sua tarefa é avaliar UM vídeo do YouTube e devolver um JSON \
estrito com a análise.

IDIOMA: escreva neutral_title, resumo, pontos_chave, fatos e citacoes SEMPRE no \
MESMO IDIOMA ORIGINAL do vídeo (o idioma do título/transcrição). NÃO traduza: se o \
vídeo é em inglês, responda em inglês; se em português, em português; etc. As CHAVES \
do JSON permanecem como abaixo.

Regras:
- Classifique o vídeo no pilar mais alinhado, ou "nenhum" se não servir a nenhum.
- Dê um score 0–100 de alinhamento aos objetivos do usuário (quanto realmente \
entrega de valor para os pilares, não quão popular é).
{regras_usuario}
- neutral_title: reescreva o título no idioma original para algo neutro e informativo \
(o que o vídeo realmente entrega), SEM CAPS, SEM emoji, SEM isca. Sentence case.
- resumo: 2 a 4 frases, no idioma original do vídeo.
- pontos_chave: pontos acionáveis (pode ser lista vazia se não houver).
- fatos_para_memorizar: SÓ inclua conhecimento atômico e testável (fatos, \
definições, princípios). Se o vídeo for opinião/narrativa, devolva lista vazia — \
não polua o Anki. Pode ser vazio mesmo em vídeos bons. Use {"tipo":"basic","frente":..,"verso":..} \
ou {"tipo":"cloze","texto":"... {{c1::lacuna}} ..."}.
- citacoes: trechos curtos com timestamp "mm:ss" (use os timestamps da transcrição \
quando houver). Lista vazia se não houver transcrição.
- Se NÃO houver transcrição, ranqueie e reescreva o título usando título+descrição, \
e deixe claro no resumo que ele é baseado só em metadados.

Responda APENAS com o JSON, sem texto antes ou depois, neste formato exato:
{
  "pillar": "saude | investimento | paternidade | nenhum",
  "score": 0,
  "is_clickbait": false,
  "neutral_title": "",
  "resumo": "",
  "pontos_chave": [],
  "fatos_para_memorizar": [],
  "citacoes": [{"texto": "", "timestamp": "mm:ss"}]
}"""

MAX_OUTPUT_TOKENS = 4096  # 1500 truncava o JSON em transcrições longas (parse falhava)


def _interesses_text(p):
    """O que o usuário quer saber neste tema. Campo novo 'interesses' (prosa);
    para temas legados, sintetiza de descricao + quero."""
    it = (p.get("interesses") or "").strip()
    if it:
        return it
    parts = []
    if p.get("descricao"):
        parts.append(str(p["descricao"]).strip())
    quero = p.get("quero") or []
    if isinstance(quero, str):
        quero = [s.strip() for s in quero.split(",") if s.strip()]
    if quero:
        parts.append("Interesses: " + ", ".join(quero))
    return " ".join(parts).strip()


def _evitar_text(p):
    nq = p.get("nao_quero") or []
    if isinstance(nq, str):
        return nq.strip()
    return ", ".join(nq)


def _pillars_block(pilares):
    linhas = [
        "Temas de interesse do usuário. Para cada tema, avalie o quanto o conteúdo "
        "entrega o que ele QUER SABER (não o quanto o tema é só 'tocado'). O peso "
        "(1–5) indica a importância do tema: temas de peso maior pesam mais no "
        "ranqueamento; conteúdo que serve a um tema de peso alto deve receber score "
        "mais alto que conteúdo igualmente bom de um tema de peso baixo."
    ]
    for key, p in pilares.items():
        quer = _interesses_text(p) or "(não especificado)"
        evitar = _evitar_text(p)
        bloco = (
            f"- {key} ({p.get('nome','')}) [peso {p.get('peso', 3)} de 5]:\n"
            f"    quer saber: {quer}"
        )
        if evitar:
            bloco += f"\n    evitar: {evitar}"
        linhas.append(bloco)
    return "\n".join(linhas)


def _system_text(cfg):
    import config
    # injeta as regras editáveis do usuário (cada linha vira um marcador)
    regras = config.get_rules().strip()
    regras_fmt = "\n".join("- " + ln.strip() for ln in regras.splitlines() if ln.strip())
    head = SYSTEM_RULES.replace("{regras_usuario}", regras_fmt)
    return head + "\n\n" + _pillars_block(config.get_pilares())


def _build_user_message(video, transcript, cfg):
    parts = [
        f"Título original: {video.get('title','')}",
        f"Canal: {video.get('channel','')}",
        f"Duração: {video.get('duration','')}",
        f"Descrição:\n{(video.get('description') or '')[:2000]}",
    ]
    if transcript and transcript.get("available") and transcript.get("text"):
        limit = cfg["llm"].get("max_transcript_chars", 14000)
        text = transcript["text"][:limit]
        parts.append(f"\nTranscrição (pode estar truncada):\n{text}")
        marks = [f'{s["ts"]}: {s["text"]}' for s in transcript["segments"][:40]]
        if marks:
            parts.append(
                "\nReferências de timestamp (início da transcrição):\n" + "\n".join(marks)
            )
    else:
        parts.append("\n[Sem transcrição disponível — avalie por título e descrição.]")
    return "\n".join(parts)


# --- provedores -------------------------------------------------------------
# Cada função recebe (system_text, user_msg, pcfg) e devolve o texto cru do modelo.

def _require_key(pcfg, default_env):
    env = pcfg.get("api_key_env", default_env)
    # Prioriza a chave colada no app (~/.aspis/secrets.json); cai no ambiente.
    try:
        import keystore

        key = keystore.get(env)
    except Exception:
        key = os.environ.get(env)
    if not key:
        raise RuntimeError(
            f"Chave do LLM não configurada. Cole a chave em Configurações no app "
            f"(ou exporte a variável de ambiente {env})."
        )
    return key


def _call_gemini(system_text, user_msg, pcfg):
    from google import genai
    from google.genai import types

    key = _require_key(pcfg, "GEMINI_API_KEY")
    client = genai.Client(api_key=key)
    resp = client.models.generate_content(
        model=pcfg["model"],
        contents=user_msg,
        config=types.GenerateContentConfig(
            system_instruction=system_text,
            response_mime_type="application/json",  # força JSON puro
            temperature=pcfg.get("temperature", 0.3),
            max_output_tokens=MAX_OUTPUT_TOKENS,
        ),
    )
    return resp.text


def _call_anthropic(system_text, user_msg, pcfg):
    import anthropic

    key = _require_key(pcfg, "ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model=pcfg["model"],
        max_tokens=MAX_OUTPUT_TOKENS,
        temperature=pcfg.get("temperature", 0.3),
        system=[
            {
                "type": "text",
                "text": system_text,
                # cache do bloco estável (regras + pilares) entre vídeos
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_msg}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


PROVIDERS = {
    "gemini": _call_gemini,
    "anthropic": _call_anthropic,
}


def _resolve_provider(cfg):
    name = cfg["llm"].get("provider", "gemini")
    providers = cfg["llm"].get("providers", {})
    pcfg = providers.get(name)
    if pcfg is None:
        raise RuntimeError(f"Provedor '{name}' não configurado em llm.providers.")
    if not pcfg.get("enabled", True):
        raise RuntimeError(
            f"Provedor ativo '{name}' está com enabled: false em config.yaml."
        )
    fn = PROVIDERS.get(name)
    if fn is None:
        raise RuntimeError(f"Provedor '{name}' desconhecido. Opções: {list(PROVIDERS)}.")
    return name, fn, pcfg


# --- parsing / normalização -------------------------------------------------
def _parse_json(text):
    """Parse tolerante: remove cercas de código, tenta direto, depois recorta do
    primeiro { ao último }."""
    text = (text or "").strip()
    if not text:
        raise ValueError("resposta do LLM veio vazia (possível truncamento/bloqueio)")
    # remove cercas markdown ```json ... ```
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j != -1 and j > i:
        return json.loads(text[i : j + 1])
    raise ValueError("resposta do LLM não continha JSON válido")


def _valid_pillars():
    """Chaves de pilar aceitas = as configuradas pelo usuário + 'nenhum'."""
    import config

    return set(config.get_pilares().keys()) | {"nenhum"}


def _coerce(obj, video):
    pillar = obj.get("pillar", "nenhum")
    if pillar not in _valid_pillars():
        pillar = "nenhum"
    try:
        score = int(round(float(obj.get("score", 0))))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))
    return {
        "pillar": pillar,
        "score": score,
        "is_clickbait": 1 if obj.get("is_clickbait") else 0,
        "neutral_title": (obj.get("neutral_title") or video.get("title") or "").strip(),
        "resumo": (obj.get("resumo") or "").strip(),
        "pontos_chave": obj.get("pontos_chave") or [],
        "fatos": obj.get("fatos_para_memorizar") or [],
        "citacoes": obj.get("citacoes") or [],
    }


def analyze(video, transcript, cfg=None, max_retries=2):
    """Analisa um único vídeo com o cérebro ATIVO (config llm.provider).
    `video` é dict com title/channel/description/duration; `transcript` é o dict
    de transcript.get_transcript (ou None). Retorna o dict pronto para
    store.upsert_video."""
    cfg = cfg or load()
    name, call, pcfg = _resolve_provider(cfg)
    system_text = _system_text(cfg)
    user_msg = _build_user_message(video, transcript, cfg)

    last_err = None
    for _ in range(max_retries + 1):
        try:
            text = call(system_text, user_msg, pcfg)
            obj = _parse_json(text)
            return _coerce(obj, video)
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e  # JSON inválido → tenta de novo
            continue
    raise RuntimeError(
        f"brain.analyze ({name}) falhou após {max_retries+1} tentativas: {last_err}"
    )


# --- análise de UM post do X (Twitter) --------------------------------------
POST_SYSTEM_RULES = """Você é um curador a serviço dos objetivos de vida do usuário (os "pilares" \
descritos abaixo). Sua tarefa é avaliar UM post do X (Twitter) — pode ser um tweet \
isolado ou uma thread — e devolver um JSON estrito com a análise.

IDIOMA: escreva neutral_title, resumo, pontos_chave, fatos e citacoes SEMPRE no \
MESMO IDIOMA ORIGINAL do post. NÃO traduza. As CHAVES do JSON permanecem como abaixo.

Regras:
- Classifique o post no pilar mais alinhado, ou "nenhum" se não servir a nenhum.
- Dê um score 0–100 de alinhamento aos objetivos do usuário (quanto realmente \
entrega de valor para os pilares, não quão viral/popular é).
{regras_usuario}
- O X premia indignação e isca de engajamento. Penalize com força: rage bait, \
dunks, takes vazios, "thread 🧵" de coach, FOMO, autopromoção, polêmica fabricada. \
Marque is_clickbait e reduza o score. Um post curto PODE ter score alto se for \
denso e útil (um dado, um princípio, um link de valor).
- neutral_title: uma linha neutra e informativa no idioma original (o que o post \
realmente diz), SEM CAPS, SEM emoji, SEM isca. Sentence case.
- resumo: 1 a 3 frases, no idioma original. Para posts triviais, seja breve.
- pontos_chave: pontos acionáveis (lista vazia se não houver — o normal num tweet).
- fatos_para_memorizar: SÓ conhecimento atômico e testável (fato, definição, \
princípio). Quase sempre VAZIO num tweet — não polua o Anki com opinião.
- citacoes: o trecho central do post entre aspas, SEM timestamp (use "" no campo \
timestamp). Lista vazia se não fizer sentido.

Responda APENAS com o JSON, sem texto antes ou depois, neste formato exato:
{
  "pillar": "<uma das chaves de pilar> | nenhum",
  "score": 0,
  "is_clickbait": false,
  "neutral_title": "",
  "resumo": "",
  "pontos_chave": [],
  "fatos_para_memorizar": [],
  "citacoes": [{"texto": "", "timestamp": ""}]
}"""


def _post_system_text(cfg):
    import config

    regras = config.get_rules().strip()
    regras_fmt = "\n".join("- " + ln.strip() for ln in regras.splitlines() if ln.strip())
    head = POST_SYSTEM_RULES.replace("{regras_usuario}", regras_fmt)
    return head + "\n\n" + _pillars_block(config.get_pilares())


def _build_post_message(post, cfg):
    parts = [
        f"Autor: {post.get('channel','')} (@{post.get('channel_id','')})",
        f"Curtidas: {post.get('likes', 0)} · Reposts: {post.get('retweets', 0)}",
        f"\nTexto do post:\n{(post.get('text') or '').strip()}",
    ]
    return "\n".join(parts)


def analyze_post(post, cfg=None, max_retries=2):
    """Analisa um único post do X com o cérebro ATIVO. `post` é dict com
    text/channel/channel_id/likes (de x.fetch_new_posts). Retorna o dict pronto
    para store.upsert_video (mesmo formato de analyze)."""
    cfg = cfg or load()
    name, call, pcfg = _resolve_provider(cfg)
    system_text = _post_system_text(cfg)
    user_msg = _build_post_message(post, cfg)

    fallback = {"title": (post.get("text") or "")[:120]}
    last_err = None
    for _ in range(max_retries + 1):
        try:
            text = call(system_text, user_msg, pcfg)
            obj = _parse_json(text)
            return _coerce(obj, fallback)
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e
            continue
    raise RuntimeError(
        f"brain.analyze_post ({name}) falhou após {max_retries+1} tentativas: {last_err}"
    )


# --- Q&A sobre um vídeo (texto livre, baseado na transcrição) ---------------
ASK_MAX_TOKENS = 1200
ASK_TRANSCRIPT_CHARS = 24000  # contexto da transcrição enviado ao modelo


def _call_gemini_text(system_text, user_msg, pcfg):
    from google import genai
    from google.genai import types

    key = _require_key(pcfg, "GEMINI_API_KEY")
    client = genai.Client(api_key=key)
    resp = client.models.generate_content(
        model=pcfg["model"],
        contents=user_msg,
        config=types.GenerateContentConfig(
            system_instruction=system_text,
            temperature=pcfg.get("temperature", 0.3),
            max_output_tokens=ASK_MAX_TOKENS,
        ),
    )
    return resp.text


def _call_anthropic_text(system_text, user_msg, pcfg):
    import anthropic

    key = _require_key(pcfg, "ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model=pcfg["model"],
        max_tokens=ASK_MAX_TOKENS,
        temperature=pcfg.get("temperature", 0.3),
        system=[{"type": "text", "text": system_text}],
        messages=[{"role": "user", "content": user_msg}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


_TEXT_PROVIDERS = {"gemini": _call_gemini_text, "anthropic": _call_anthropic_text}


def ask(video, transcript_text, question, history=None, cfg=None):
    """Responde a uma pergunta SOBRE o vídeo, usando a transcrição completa como
    fonte. `video` é o dict do banco (tem neutral_title, resumo, etc.).
    `history` é lista de {question, answer} anteriores (contexto da conversa).
    Responde em texto livre, no idioma da pergunta. Retorna a string da resposta."""
    cfg = cfg or load()
    name, _, pcfg = _resolve_provider(cfg)
    call = _TEXT_PROVIDERS.get(name)
    if not call:
        raise RuntimeError(f"Provedor '{name}' não suporta Q&A.")

    sys_txt = (
        "Você é um assistente de estudo que ajuda o usuário a aprofundar UM vídeo "
        "específico. Responda à pergunta APENAS com base na transcrição e nos "
        "metadados fornecidos. Se a resposta não estiver na transcrição, diga isso "
        "claramente e ofereça o que dá para inferir, sem inventar fatos. Seja conciso "
        "e direto. Responda no MESMO IDIOMA da pergunta do usuário. Quando útil, cite "
        "trechos curtos entre aspas."
    )

    parts = [
        f"VÍDEO: {video.get('neutral_title') or video.get('original_title','')}",
        f"Canal: {video.get('channel','')}",
    ]
    if video.get("resumo"):
        parts.append(f"Resumo já gerado: {video['resumo']}")
    tt = (transcript_text or "").strip()
    if tt:
        parts.append("\nTRANSCRIÇÃO (fonte da verdade):\n" + tt[:ASK_TRANSCRIPT_CHARS])
    else:
        parts.append("\n[Sem transcrição disponível — responda só pelos metadados e diga isso.]")
    if history:
        hist = "\n".join(
            f"P: {h.get('question','')}\nR: {h.get('answer','')}" for h in history[-6:]
        )
        parts.append("\nCONVERSA ANTERIOR:\n" + hist)
    parts.append(f"\nPERGUNTA DO USUÁRIO:\n{question}")

    answer = call(sys_txt, "\n".join(parts), pcfg)
    return (answer or "").strip()


if __name__ == "__main__":
    cfg = load()
    name, _, pcfg = _resolve_provider(cfg)
    print(f"Cérebro ativo: {name} | modelo: {pcfg['model']} | key_env: {pcfg.get('api_key_env')}")
