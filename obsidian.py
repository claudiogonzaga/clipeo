"""obsidian.py — escreve o conhecimento no "segundo cérebro" (vault Obsidian).

Para cada vídeo salvo:
  - cria/atualiza uma nota .md em {vault}/Aspis/ com frontmatter,
    resumo, pontos-chave e citações;
  - garante um link na MOC do pilar ({Pilar} - MOC.md);
  - acrescenta um link na nota diária ({daily_notes_folder}/{YYYY-MM-DD}.md).

E, ao fim da rotina, escreve a nota-digest do dia (lista ranqueada + resumos).
Tudo idempotente: salvar de novo regenera a nota e não duplica links.
"""
import os
import re
from datetime import datetime, timezone

import store
from config import expand, load

PILLAR_NAMES = {
    "saude": "Saúde",
    "investimento": "Investimento",
    "paternidade": "Paternidade",
    "nenhum": "Geral",
}
SUBFOLDER = "Aspis"


def _sanitize(name, max_len=80):
    name = re.sub(r"[\\/:*?\"<>|#\[\]^]", "", name or "").strip()
    name = re.sub(r"\s+", " ", name)
    return name[:max_len].strip() or "sem-titulo"


def _vault(cfg):
    return expand(cfg["obsidian"]["vault_path"])


def _ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def _note_basename(v):
    return f"{_sanitize(v['neutral_title'])} ({v['video_id']})"


def _note_path(cfg, v):
    folder = os.path.join(_vault(cfg), SUBFOLDER)
    _ensure_dir(folder)
    return os.path.join(folder, _note_basename(v) + ".md")


def _audio_path(cfg, v):
    folder = os.path.join(_vault(cfg), SUBFOLDER)
    _ensure_dir(folder)
    return os.path.join(folder, _note_basename(v) + ".wav")


def _render_note(v, audio_name=None):
    pillar_name = PILLAR_NAMES.get(v["pillar"], "Geral")
    data = (v.get("published_at") or "")[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    autor = (v.get("channel") or "").replace('"', "'")

    lines = [
        "---",
        "fonte: youtube",
        f'autor: "{autor}"',
        f"pilar: {v['pillar']}",
        f"data: {data}",
        f"url: {v.get('url','')}",
        f"score: {v.get('score', 0)}",
        f"tags: [{v['pillar']}, aspis]",
        "---",
        "",
        f"# {v['neutral_title']}",
        "",
    ]
    # nota de áudio: embed do .wav (o Obsidian toca inline)
    if audio_name:
        lines += ["## Áudio", f"![[{audio_name}]]", ""]
    lines += [
        "## Resumo",
        v.get("resumo", "") or "_(sem resumo)_",
        "",
    ]
    pontos = v.get("pontos_chave") or []
    if pontos:
        lines.append("## Pontos-chave")
        lines += [f"- {p}" for p in pontos]
        lines.append("")
    citacoes = v.get("citacoes") or []
    if citacoes:
        lines.append("## Citações")
        for c in citacoes:
            ts = c.get("timestamp", "")
            txt = c.get("texto", "")
            lines.append(f"> {txt}" + (f" — {ts}" if ts else ""))
        lines.append("")
    lines.append(f"[[{pillar_name} - MOC]]")
    lines.append("")
    return "\n".join(lines)


def _append_link_once(file_path, link_line, header=None):
    """Acrescenta `link_line` a `file_path` se ainda não estiver lá."""
    existing = ""
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as fh:
            existing = fh.read()
    if link_line.strip() in existing:
        return  # já presente → idempotente
    with open(file_path, "a", encoding="utf-8") as fh:
        if not existing:
            if header:
                fh.write(header + "\n")
        elif not existing.endswith("\n"):
            fh.write("\n")
        elif header and header not in existing:
            fh.write("\n" + header + "\n")
        fh.write(link_line + "\n")


def _update_moc(cfg, v):
    pillar_name = PILLAR_NAMES.get(v["pillar"], "Geral")
    folder = os.path.join(_vault(cfg), SUBFOLDER)
    _ensure_dir(folder)
    moc_path = os.path.join(folder, f"{pillar_name} - MOC.md")
    link = f"- [[{_note_basename(v)}]]"
    header = f"# {pillar_name} — MOC\n\nMapa de conteúdo do pilar **{pillar_name}**, curado pelo Aspis.\n"
    _append_link_once(moc_path, link, header=header)


def _update_daily_note(cfg, v):
    daily_folder = cfg["obsidian"].get("daily_notes_folder", "Daily")
    folder = os.path.join(_vault(cfg), daily_folder)
    _ensure_dir(folder)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_path = os.path.join(folder, f"{today}.md")
    link = f"- [[{_note_basename(v)}]] · {PILLAR_NAMES.get(v['pillar'],'Geral')}"
    _append_link_once(daily_path, link, header="## Aspis")


def save(video_id, cfg=None):
    """Salva o vídeo no vault: nota + MOC + nota diária. Idempotente.
    Retorna o caminho da nota criada."""
    cfg = cfg or load()
    v = store.get_video(video_id)
    if not v:
        raise ValueError(f"vídeo {video_id} não está no banco")

    path = _note_path(cfg, v)
    audio = _audio_path(cfg, v)
    audio_name = os.path.basename(audio) if os.path.exists(audio) else None
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_render_note(v, audio_name))
    _update_moc(cfg, v)
    _update_daily_note(cfg, v)
    store.set_flag(video_id, "saved_obsidian", 1)
    return path


def save_audio(video_id, wav_bytes, cfg=None):
    """Grava o .wav da análise ao lado da nota e regrava a nota com o embed
    (vira uma nota de áudio, tocável no Obsidian). Retorna o caminho do .wav."""
    cfg = cfg or load()
    v = store.get_video(video_id)
    if not v:
        raise ValueError(f"vídeo {video_id} não está no banco")
    audio = _audio_path(cfg, v)
    with open(audio, "wb") as fh:
        fh.write(wav_bytes)
    save(video_id, cfg)  # regrava a nota já com o embed do áudio
    return audio


def write_daily_digest(cfg=None):
    """Escreve/atualiza a nota-digest do dia: lista ranqueada + resumos dos
    vídeos acima do limiar. Chamada pela rotina."""
    cfg = cfg or load()
    threshold = cfg.get("score_threshold", 60)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    videos = store.get_videos(min_score=threshold, day=today)

    folder = os.path.join(_vault(cfg), SUBFOLDER, "Digests")
    _ensure_dir(folder)
    path = os.path.join(folder, f"{today} - Digest.md")

    filtered = store.count_below(threshold, day=today)
    lines = [
        "---",
        f"data: {today}",
        "tags: [aspis, digest]",
        "---",
        "",
        f"# Aspis — {today}",
        "",
        f"{len(videos)} vídeo(s) acima do limiar · {filtered} filtrado(s).",
        "",
    ]
    for v in videos:
        pill = PILLAR_NAMES.get(v["pillar"], "Geral")
        lines.append(f"## {v['score']} · {v['neutral_title']}")
        lines.append(f"_{v.get('channel','')} · {v.get('duration','')} · {pill}_")
        lines.append("")
        if v.get("resumo"):
            lines.append(v["resumo"])
            lines.append("")
        lines.append(f"[[{_note_basename(v)}]] · {v.get('url','')}")
        lines.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


if __name__ == "__main__":
    import sys

    store.init()
    cfg = load()
    if len(sys.argv) > 1:
        print("nota:", save(sys.argv[1], cfg))
    else:
        print("digest:", write_daily_digest(cfg))
