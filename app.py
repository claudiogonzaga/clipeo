"""Aspis — janela pywebview + ponte Python<->JS.

Milestone 2: a UI passa a ler do SQLite (store.py), preenchido pela rotina
(routine.py). As ações de leitura são reais; as ações de escrita do segundo
cérebro (Obsidian/Anki/download) marcam flags no banco e ganham execução de
verdade no Milestone 3.

Para demonstrar sem rodar o pipeline real:
    ./.venv/bin/python store.py --seed   # popula vídeos de exemplo
    ./.venv/bin/python app.py
"""
import os
import subprocess
import sys
from datetime import datetime, timezone

import webview

import store
from config import load

UI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", "index.html")


class Api:
    """Ponte exposta ao JS como pywebview.api.*"""

    def __init__(self):
        self.cfg = load()
        self._refreshing = False
        store.init()

    @property
    def threshold(self):
        # derivado das estrelas mínimas escolhidas nas Configurações
        import config

        return config.get_threshold()

    # --- leitura ---
    def _since_iso(self):
        import config
        from datetime import datetime, timedelta, timezone

        h = config.period_lookback_hours()
        return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()

    def _decorate(self, videos):
        """Adiciona 'stars' (0–5) a cada vídeo."""
        import config

        for v in videos:
            v["stars"] = config.score_to_stars(v.get("score", 0))
        return videos

    def get_videos(self, pillar=None, include_read=False):
        vids = store.get_videos(
            filter_pillar=pillar, min_score=self.threshold,
            include_read=include_read, since_iso=self._since_iso(),
        )
        return self._decorate(vids)

    def get_synthesis(self, video_id):
        v = store.get_video(video_id)
        if v:
            import config
            v["stars"] = config.score_to_stars(v.get("score", 0))
        return v

    def new_count(self):
        # "novos" = acima do limiar, não lidos, dentro do período
        return len(store.get_videos(
            min_score=self.threshold, include_read=False, since_iso=self._since_iso()))

    def read_count(self):
        return len(store.get_videos(
            min_score=self.threshold, include_read=True, since_iso=self._since_iso())) \
            - self.new_count()

    def set_read(self, video_id, value=1):
        store.set_flag(video_id, "read", int(value))
        return {"ok": True}

    # --- estrelas / período / regras (Configurações) ---
    def get_view_prefs(self):
        import config

        return {"min_stars": config.get_min_stars(), "period": config.get_period()}

    def set_min_stars(self, value):
        import config

        return {"ok": True, "min_stars": config.set_min_stars(value)}

    def set_period(self, value):
        import config

        return {"ok": True, "period": config.set_period(value)}

    def get_rules(self):
        import config

        return {"rules": config.get_rules()}

    def set_rules(self, text):
        import config

        return {"ok": True, "rules": config.set_rules(text)}

    def refresh(self, max_total=25):
        """Botão Atualizar: roda o pipeline na hora (busca novos → sintetiza →
        grava) e devolve um resumo. Exige um canal do YouTube conectado e a
        chave do Gemini. Limita a `max_total` vídeos novos por clique."""
        if self._refreshing:
            return {"ok": False, "error": "Atualização já em andamento."}
        import accounts  # usado também no except ReauthRequired abaixo
        # pré-checagens amigáveis
        try:
            if not accounts.status().get("active"):
                return {"ok": False, "error": "Conecte um canal do YouTube primeiro."}
        except Exception:
            pass
        import keystore

        env, _ = self._llm_env()
        if not keystore.has(env):
            return {"ok": False, "error": "Configure a chave do Gemini primeiro."}

        self._refreshing = True
        try:
            import routine

            summary = routine.run(self.cfg, max_total=max_total)
            return {"ok": True, **summary}
        except accounts.ReauthRequired as e:
            # sessão do canal morreu → a UI mostra um BOTÃO de reconexão
            return {"ok": False, "error": str(e), "reauth": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        finally:
            self._refreshing = False

    def filtered_count(self, day=None):
        return store.count_below(self.threshold, day=day)

    # --- ações ---
    def save_obsidian(self, video_id):
        # Execução real chega no Milestone 3 (obsidian.py). Por ora, registra o estado.
        try:
            import obsidian

            obsidian.save(video_id, self.cfg)
        except ImportError:
            print(f"[m2] save_obsidian({video_id}) — obsidian.py chega no M3")
        store.set_flag(video_id, "saved_obsidian", 1)
        return {"ok": True}

    def save_anki(self, video_id):
        try:
            import anki

            anki.save(video_id, self.cfg)
        except ImportError:
            print(f"[m2] save_anki({video_id}) — anki.py chega no M3")
        store.set_flag(video_id, "saved_anki", 1)
        return {"ok": True}

    def download(self, video_id):
        try:
            import download as dl

            dl.download(video_id, self.cfg)
        except ImportError:
            print(f"[m2] download({video_id}) — download.py chega no M3")
        store.set_flag(video_id, "downloaded", 1)
        return {"ok": True}

    def watch(self, video_id):
        """Baixa (se preciso) e abre o arquivo LOCAL no player do sistema —
        nunca o YouTube. Devolve erro legível em vez de travar."""
        try:
            import download as dl
        except ImportError:
            return {"ok": False, "error": "Módulo de download indisponível."}

        try:
            path = dl.local_path(video_id, self.cfg)
            if not os.path.exists(path):
                # download() devolve o caminho REAL (pode não ser .mp4 sem ffmpeg)
                path = dl.download(video_id, self.cfg)
            if not path or not os.path.exists(path):
                return {"ok": False, "error": "O download não produziu um arquivo."}
            store.set_flag(video_id, "downloaded", 1)
            subprocess.run(["open", path], check=False)
            return {"ok": True, "path": path}
        except Exception as e:  # noqa: BLE001 — erro vai para a UI
            msg = str(e)
            if "ffmpeg" in msg.lower():
                msg = "Falta o ffmpeg para juntar vídeo+áudio. Instale com: brew install ffmpeg"
            elif "429" in msg or "Too Many Requests" in msg:
                msg = "O YouTube limitou os downloads agora (tente mais tarde)."
            return {"ok": False, "error": msg}

    def send_android(self, video_id):
        """Copia o vídeo para a pasta sincronizada com o Android (baixa se
        preciso). Erro legível em vez de exceção silenciosa."""
        try:
            import download as dl
        except ImportError:
            return {"ok": False, "error": "Módulo de download indisponível."}
        try:
            dest = dl.send_to_android(video_id, self.cfg)
            store.set_flag(video_id, "sent_android", 1)
            return {"ok": True, "path": dest}
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "ffmpeg" in msg.lower():
                msg = "Falta o ffmpeg para baixar o vídeo. Instale: brew install ffmpeg"
            elif "429" in msg or "Too Many Requests" in msg:
                msg = "O YouTube limitou os downloads agora (tente mais tarde)."
            return {"ok": False, "error": msg}

    def set_feedback(self, video_id, value):
        store.set_flag(video_id, "feedback", int(value))
        return {"ok": True}

    # --- Q&A por vídeo (explorar/aprofundar, estilo NotebookLM) ---
    def get_qa(self, video_id):
        return store.get_qa(video_id)

    def ask_video(self, video_id, question):
        """Pergunta sobre o vídeo, respondida pela transcrição completa. Salva no
        histórico. Erro legível em vez de exceção silenciosa."""
        question = (question or "").strip()
        if not question:
            return {"ok": False, "error": "Escreva uma pergunta."}
        v = store.get_video(video_id)
        if not v:
            return {"ok": False, "error": "Vídeo não encontrado."}

        # transcrição: usa a salva; se não houver, tenta buscar agora e guarda
        tt = store.get_transcript_text(video_id)
        if not tt:
            try:
                import transcript as tmod

                tr = tmod.get_transcript(video_id, self.cfg)
                if tr.get("available") and tr.get("text"):
                    tt = tr["text"]
                    store.set_transcript_text(video_id, tt)
            except Exception:
                tt = None

        try:
            import brain

            history = store.get_qa(video_id)
            answer = brain.ask(v, tt, question, history=history, cfg=self.cfg)
            qa_id = store.add_qa(video_id, question, answer)
            return {"ok": True, "id": qa_id, "question": question, "answer": answer,
                    "had_transcript": bool(tt)}
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "429" in msg or "Too Many Requests" in msg:
                msg = "O serviço limitou agora — tente em instantes."
            elif "chave" in msg.lower() or "key" in msg.lower():
                msg = "Configure a chave do Gemini em Configurações."
            return {"ok": False, "error": msg}

    def delete_qa(self, qa_id):
        store.delete_qa(qa_id)
        return {"ok": True}

    # --- coleções (playlists / blocos de notas) ---
    def list_collections(self):
        return store.list_collections()

    def create_collection(self, name):
        return {"ok": True, "id": store.create_collection(name)}

    def rename_collection(self, cid, name):
        store.rename_collection(cid, name)
        return {"ok": True}

    def delete_collection(self, cid):
        store.delete_collection(cid)
        return {"ok": True}

    def get_collection(self, cid):
        return store.get_collection(cid)

    def add_to_collection(self, cid, item_type, video_id, qa_id=None):
        if item_type not in ("video", "resumo", "qa"):
            return {"ok": False, "error": "tipo inválido"}
        item_id = store.add_to_collection(cid, item_type, video_id, qa_id=qa_id)
        return {"ok": True, "item_id": item_id}

    def remove_collection_item(self, item_id):
        store.remove_collection_item(item_id)
        return {"ok": True}

    def collections_for_video(self, video_id):
        return store.collections_for_video(video_id)

    # --- conta do YouTube (login multi-canal dentro do app) ---
    def yt_status(self):
        import accounts

        return accounts.status()

    def yt_save_client(self, text):
        """Salva a credencial OAuth colada pelo usuário."""
        import accounts

        return accounts.save_client(text)

    def yt_connect(self):
        """Abre o navegador para o usuário logar e escolher a conta/canal.
        Bloqueia até concluir; devolve o canal conectado ou erro."""
        import accounts

        return accounts.connect()

    def yt_set_active(self, channel_id):
        import accounts

        return accounts.set_active(channel_id)

    def yt_remove(self, channel_id):
        import accounts

        return accounts.remove(channel_id)

    def yt_reset_client(self):
        import accounts

        return accounts.clear_client()

    # --- chave do LLM (Gemini) ---
    def _llm_env(self):
        """Nome da variável de ambiente da chave do provedor ativo."""
        llm = self.cfg.get("llm", {})
        prov = llm.get("provider", "gemini")
        pcfg = llm.get("providers", {}).get(prov, {})
        return pcfg.get("api_key_env", "GEMINI_API_KEY"), prov

    def gemini_status(self):
        import keystore

        env, prov = self._llm_env()
        return {"provider": prov, "env": env,
                "configured": keystore.has(env), "masked": keystore.masked(env)}

    def save_gemini_key(self, value):
        import keystore

        env, _ = self._llm_env()
        if not (value or "").strip():
            return {"ok": False, "error": "Cole a chave."}
        return keystore.set_key(env, value)

    def clear_gemini_key(self):
        import keystore

        env, _ = self._llm_env()
        keystore.set_key(env, "")
        return {"ok": True}

    # --- atualizações (GitHub) ---
    def check_update(self):
        import updater

        return updater.check()

    def install_update(self, dmg_url):
        import updater

        r = updater.install(dmg_url)
        if r.get("ok") and r.get("relaunch"):
            # fecha o app atual logo após disparar a abertura do novo
            def _quit():
                import time

                time.sleep(1.5)
                try:
                    webview.windows[0].destroy()
                except Exception:
                    pass
                os._exit(0)

            import threading

            threading.Thread(target=_quit, daemon=True).start()
        return r

    # --- Whisper (fallback de transcrição) ---
    def whisper_status(self):
        import config

        w = config.get_whisper()
        # marca quais modelos já estão baixados em cache local
        downloaded = set()
        try:
            import os

            hub = os.path.expanduser("~/.cache/huggingface/hub")
            if os.path.isdir(hub):
                for name in os.listdir(hub):
                    for m in config.WHISPER_MODELS:
                        tag = "large-v3" if m == "large-v3" else m
                        if f"faster-whisper-{tag}" in name:
                            downloaded.add(m)
        except Exception:
            pass
        return {
            "model": w.get("model", "base"),
            "enabled": w.get("enabled", False),
            "auto_on_block": w.get("auto_on_block", True),
            "models": config.WHISPER_MODELS,
            "downloaded": sorted(downloaded),
        }

    def save_whisper(self, model=None, enabled=None, auto_on_block=None):
        import config

        cfg = config.save_whisper(model=model, enabled=enabled, auto_on_block=auto_on_block)
        return {"ok": True, **cfg}

    # --- objetivos (pilares) editáveis, com peso ---
    def get_pilares(self):
        import config

        return [
            {"id": k, "nome": p.get("nome", k),
             "descricao": p.get("descricao", ""), "peso": p.get("peso", 3),
             "quero": ", ".join(p.get("quero", []) or []),
             "nao_quero": ", ".join(p.get("nao_quero", []) or [])}
            for k, p in config.get_pilares().items()
        ]

    def save_pilares(self, items):
        import re
        import unicodedata

        import config

        def slug(s):
            s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
            s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
            return s[:24] or "obj"

        prev = config.get_pilares()
        out = {}
        for it in (items or []):
            nome = (it.get("nome") or "").strip()
            if not nome:
                continue
            key = (it.get("id") or "").strip().lower() or slug(nome)
            base, n = key, 2
            while key in out:
                key = f"{base}-{n}"
                n += 1
            try:
                peso = int(it.get("peso", 3))
            except (TypeError, ValueError):
                peso = 3
            old = prev.get(it.get("id", ""), {})

            def _list(field):
                # aceita string "a, b, c" (da UI) ou lista; senão preserva antiga
                val = it.get(field, None)
                if isinstance(val, str):
                    return [s.strip() for s in val.split(",") if s.strip()]
                if isinstance(val, list):
                    return val
                return old.get(field, [])

            out[key] = {
                "nome": nome,
                "descricao": (it.get("descricao") or "").strip(),
                "quero": _list("quero"),
                "nao_quero": _list("nao_quero"),
                "peso": max(1, min(5, peso)),
            }
        if not out:
            return {"ok": False, "error": "Defina ao menos um objetivo."}
        config.save_pilares(out)
        return {"ok": True, "pilares": self.get_pilares()}


def main():
    if not os.path.exists(UI_PATH):
        print(f"UI não encontrada em {UI_PATH}", file=sys.stderr)
        sys.exit(1)
    webview.create_window(
        "Aspis",
        UI_PATH,
        js_api=Api(),
        width=720,
        height=820,
        min_size=(560, 640),
    )
    webview.start()


if __name__ == "__main__":
    main()
