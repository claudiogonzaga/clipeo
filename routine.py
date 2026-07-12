"""routine.py — job agendado (launchd). Orquestra, idempotente:

  1. lê last_run (default: lookback_hours atrás)
  2. youtube: inscrições → uploads → vídeos novos → hidrata
  3. para cada vídeo ainda não no banco: transcript → brain → store.upsert
  4. (M3) escreve/atualiza a nota-digest do dia no Obsidian
  5. atualiza last_run = agora

Rodar duas vezes no mesmo dia NÃO reprocessa vídeos já no banco (cache por
video_id) nem refaz chamadas de LLM.
"""
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone

import store
from config import load


def _since(cfg, meta_key="last_run"):
    # janela de busca = a do período escolhido nas Configurações (dia/semana/mês),
    # mas nunca menor que o último run (evita reprocessar tudo à toa).
    # meta_key separa as janelas das fontes (YouTube usa last_run; X, last_run_x).
    import config

    hours = config.period_lookback_hours()
    floor = datetime.now(timezone.utc) - timedelta(hours=hours)
    last = store.get_meta(meta_key)
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            return min(last_dt, floor)  # o que cobrir MAIS tempo
        except ValueError:
            pass
    return floor


def _youtube_row(v, analysis, tr):
    """Monta a linha do banco para um vídeo do YouTube já analisado.
    Compartilhado entre a rotina diária e o 'adicionar link' avulso."""
    return {
        "video_id": v["video_id"],
        "channel": v.get("channel", ""),
        "channel_id": v.get("channel_id", ""),
        "channel_thumb": v.get("channel_thumb", ""),
        "original_title": v.get("title", ""),
        "neutral_title": analysis["neutral_title"],
        "url": v.get("url", ""),
        "published_at": v.get("published_at", ""),
        "duration": v.get("duration", ""),
        "pillar": analysis["pillar"],
        "score": analysis["score"],
        "is_clickbait": analysis["is_clickbait"],
        "resumo": analysis["resumo"],
        "pontos_chave": analysis["pontos_chave"],
        "fatos": analysis["fatos"],
        "citacoes": analysis["citacoes"],
        "transcript_available": 1 if tr.get("available") else 0,
        # guarda a transcrição completa para o Q&A (NotebookLM-like)
        "transcript_text": tr.get("text") if tr.get("available") else None,
    }


def _x_row(p, analysis):
    """Monta a linha do banco para um post do X já analisado."""
    return {
        "video_id": p["video_id"],
        "source": "x",
        "channel": p.get("channel", ""),
        "channel_id": p.get("channel_id", ""),
        "channel_thumb": p.get("channel_thumb", ""),
        "original_title": p.get("text", ""),
        "neutral_title": analysis["neutral_title"],
        "url": p.get("url", ""),
        "published_at": p.get("published_at", ""),
        "duration": "",  # post não tem duração
        "pillar": analysis["pillar"],
        "score": analysis["score"],
        "is_clickbait": analysis["is_clickbait"],
        "resumo": analysis["resumo"],
        "pontos_chave": analysis["pontos_chave"],
        "fatos": analysis["fatos"],
        "citacoes": analysis["citacoes"],
        "transcript_available": 0,
        # o texto do post é a "fonte da verdade" do Q&A (NotebookLM-like)
        "transcript_text": p.get("text", ""),
    }


def run(cfg=None, max_total=None):
    """Roda o pipeline idempotente. Se max_total for dado, para depois de
    processar esse número de vídeos NOVOS (usado pelo botão Atualizar do app,
    para não rodar minutos a fio). Retorna um resumo (dict)."""
    cfg = cfg or load()
    store.init()
    started = datetime.now(timezone.utc)
    since = _since(cfg)
    print(f"[routine] início {started.isoformat()} | buscando desde {since.isoformat()}")

    # imports tardios para não exigir as libs quando só se usa o app/seed
    import youtube
    import transcript as transcript_mod
    import brain

    videos = youtube.fetch_new_videos(since, cfg)
    print(f"[routine] {len(videos)} vídeos novos retornados pela API")

    processed = filtered = skipped = errors = 0
    threshold = cfg.get("score_threshold", 60)

    for v in videos:
        if max_total and processed >= max_total:
            break
        vid = v["video_id"]
        if store.has_video(vid):
            skipped += 1
            continue
        try:
            tr = transcript_mod.get_transcript(vid, cfg)
            analysis = brain.analyze(v, tr, cfg)
            store.upsert_video(_youtube_row(v, analysis, tr))
            processed += 1
            if analysis["score"] < threshold:
                filtered += 1
        except Exception as e:  # nunca derrube a rotina inteira por 1 vídeo
            errors += 1
            print(f"[routine] erro no vídeo {vid}: {e}", file=sys.stderr)
            traceback.print_exc()

    # backfill: avatar do canal em vídeos antigos (1 unidade de quota por 50 canais)
    try:
        missing = store.channel_ids_missing_thumb()
        if missing:
            thumbs = youtube.get_channel_thumbs(youtube.get_service(cfg), missing)
            for cid, url in thumbs.items():
                store.set_channel_thumb(cid, url)
            if thumbs:
                print(f"[routine] avatar preenchido para {len(thumbs)} canais antigos")
    except Exception as e:
        print(f"[routine] aviso: backfill de avatares falhou: {e}", file=sys.stderr)

    # passo 4 (M3): nota-digest do dia no Obsidian, se o módulo existir
    try:
        import obsidian

        if hasattr(obsidian, "write_daily_digest"):
            obsidian.write_daily_digest(cfg)
            print("[routine] digest diário do Obsidian atualizado")
    except ImportError:
        pass  # obsidian.py chega no Milestone 3
    except Exception as e:
        print(f"[routine] aviso: digest do Obsidian falhou: {e}", file=sys.stderr)

    store.set_meta("last_run", started.isoformat())
    print(
        f"[routine] fim | novos={processed} já_no_banco={skipped} "
        f"abaixo_do_limiar={filtered} erros={errors}"
    )
    return {
        "processed": processed,
        "filtered": filtered,
        "skipped": skipped,
        "errors": errors,
        "above": max(0, processed - filtered),
    }


def run_x(cfg=None, max_total=None):
    """Pipeline idempotente da fonte X (Twitter), espelhando run(): lê o timeline
    (ou as contas escolhidas), processa os posts NOVOS (cache por id), analisa
    cada um pelo cérebro e grava com source='x'. Retorna um resumo (dict)."""
    cfg = cfg or load()
    store.init()
    started = datetime.now(timezone.utc)
    since = _since(cfg, meta_key="last_run_x")
    print(f"[routine-x] início {started.isoformat()} | buscando desde {since.isoformat()}")

    import x as x_mod
    import brain

    posts = x_mod.fetch_new_posts(since, cfg, max_total=max_total)
    print(f"[routine-x] {len(posts)} posts novos retornados pelo X")

    processed = filtered = skipped = errors = 0
    threshold = cfg.get("score_threshold", 60)

    for p in posts:
        if max_total and processed >= max_total:
            break
        pid = p["video_id"]
        if store.has_video(pid):
            skipped += 1
            continue
        try:
            analysis = brain.analyze_post(p, cfg)
            store.upsert_video(_x_row(p, analysis))
            processed += 1
            if analysis["score"] < threshold:
                filtered += 1
        except Exception as e:  # nunca derrube a rotina inteira por 1 post
            errors += 1
            print(f"[routine-x] erro no post {pid}: {e}", file=sys.stderr)
            traceback.print_exc()

    store.set_meta("last_run_x", started.isoformat())
    print(
        f"[routine-x] fim | novos={processed} já_no_banco={skipped} "
        f"abaixo_do_limiar={filtered} erros={errors}"
    )
    return {
        "processed": processed,
        "filtered": filtered,
        "skipped": skipped,
        "errors": errors,
        "above": max(0, processed - filtered),
    }


# --- adicionar UM link avulso (como o Aspis Android) ------------------------
# Vídeo do YouTube: youtu.be/ID, youtube.com/watch?v=ID, /shorts/ID, /live/ID…
_YT_RE = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:[^ ]*&)?v=|shorts/|live/|embed/|v/))"
    r"([\w-]{11})"
)
# Post do X/Twitter: x.com/<user>/status/<id> (e variantes)
_X_RE = re.compile(r"(?:twitter\.com|x\.com)/[^/]+/status(?:es)?/(\d+)")


def detect_link(url):
    """Reconhece um link colado e devolve ('youtube'|'x', item_id) ou (None, None)."""
    url = (url or "").strip()
    m = _YT_RE.search(url)
    if m:
        return "youtube", m.group(1)
    m = _X_RE.search(url)
    if m:
        return "x", m.group(1)
    return None, None


def _add_youtube(vid, cfg):
    import brain
    import transcript as transcript_mod
    import youtube

    service = youtube.get_service(cfg)
    metas = youtube.hydrate(service, [vid])
    if not metas:
        raise ValueError("Vídeo não encontrado (pode estar privado ou ter sido removido).")
    v = metas[0]
    try:  # avatar do canal (1 unidade de quota) — não é fatal se falhar
        thumbs = youtube.get_channel_thumbs(service, [v.get("channel_id", "")])
        v["channel_thumb"] = thumbs.get(v.get("channel_id", ""), "")
    except Exception:
        v["channel_thumb"] = ""
    tr = transcript_mod.get_transcript(vid, cfg)
    analysis = brain.analyze(v, tr, cfg)
    store.upsert_video(_youtube_row(v, analysis, tr))
    return analysis


def _add_x(tid, cfg):
    import brain
    import x as x_mod

    p = x_mod.fetch_post(tid, cfg)
    if not p:
        raise ValueError("Post não encontrado (pode estar privado ou ter sido removido).")
    analysis = brain.analyze_post(p, cfg)
    store.upsert_video(_x_row(p, analysis))
    return analysis


def add_link(url, cfg=None):
    """Adiciona UM link avulso (vídeo do YouTube ou post do X) ao feed: busca os
    metadados/transcrição, passa pelo cérebro e grava — exatamente como a rotina
    faz com cada item. Retorna um resumo (dict). Idempotente: se já estiver no
    banco, não reprocessa."""
    cfg = cfg or load()
    store.init()
    source, item_id = detect_link(url)
    if not source:
        raise ValueError(
            "Link não reconhecido. Cole o link de um vídeo do YouTube ou de um post do X."
        )
    if store.has_video(item_id):
        return {"ok": True, "source": source, "video_id": item_id, "already": True}

    analysis = _add_youtube(item_id, cfg) if source == "youtube" else _add_x(item_id, cfg)
    threshold = cfg.get("score_threshold", 60)
    return {
        "ok": True,
        "source": source,
        "video_id": item_id,
        "already": False,
        "score": analysis["score"],
        "above": 1 if analysis["score"] >= threshold else 0,
    }


if __name__ == "__main__":
    if "--x" in sys.argv:
        run_x()
    else:
        run()
