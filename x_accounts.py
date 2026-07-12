"""x_accounts.py — login do X (Twitter) dentro do app, via twikit.

O X não tem API gratuita. O caminho prático para um app pessoal é reutilizar a
SUA sessão: o usuário informa, numa tela do app, usuário/e-mail/senha UMA vez; o
twikit faz o login e nós guardamos só os COOKIES de sessão. A senha NUNCA é
salva. Depois a leitura (x.py) e a rotina reaproveitam esses cookies.

Espelha a ideia do accounts.py (YouTube): status() / connect() / remove(), só que
aqui é uma conta única (a sua) em vez de múltiplos canais.

Armazenamento (em ~/.aspis/x/, fora do repositório):
  - cookies.json    sessão do X (sensível)
  - account.json    metadados públicos da conta (handle, nome, avatar)

twikit é assíncrono; as funções públicas aqui são síncronas (envelopam com
asyncio.run) para casar com a ponte síncrona do pywebview.
"""
import asyncio
import json
import os

import config

X_DIR = os.path.join(config.USER_DIR, "x")
COOKIES_PATH = os.path.join(X_DIR, "cookies.json")
ACCOUNT_PATH = os.path.join(X_DIR, "account.json")

# Idioma das requisições ao X (não é o idioma do conteúdo, só dos cabeçalhos).
LANG = "en-US"


class XLoginRequired(RuntimeError):
    """Não há sessão do X salva (ou expirou). A UI oferece o login na aba X."""
    pass


def _new_client():
    """Constrói um Client do twikit (sem I/O de rede). Import tardio para o app
    não exigir a lib quando o X não é usado."""
    try:
        from twikit import Client
    except ImportError as e:  # mensagem amigável em vez de stack trace
        raise RuntimeError(
            "A biblioteca 'twikit' não está instalada. Rode: "
            "./.venv/bin/pip install twikit"
        ) from e
    return Client(LANG)


def connected():
    return os.path.exists(COOKIES_PATH)


def _load_account():
    if not os.path.exists(ACCOUNT_PATH):
        return None
    try:
        with open(ACCOUNT_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def _save_account(acc):
    os.makedirs(X_DIR, exist_ok=True)
    with open(ACCOUNT_PATH, "w", encoding="utf-8") as fh:
        json.dump(acc, fh, ensure_ascii=False, indent=2)


def status():
    """Estado para a UI: conectado? qual conta (handle/nome/avatar)?"""
    return {"connected": connected(), "account": _load_account()}


def build_client_with_cookies():
    """Client já com os cookies da sessão carregados, pronto para ler. Síncrono
    (só constrói e lê arquivo) — pode ser chamado de dentro de um event loop.
    Lança XLoginRequired se não houver sessão salva."""
    if not connected():
        raise XLoginRequired("Conecte sua conta do X primeiro (aba X → Conta).")
    client = _new_client()
    client.load_cookies(COOKIES_PATH)
    return client


# --- login (assíncrono por baixo) -------------------------------------------
async def _login_async(username, email, password):
    client = _new_client()
    # auth_info_1 = usuário OU e-mail; auth_info_2 = o outro (e-mail/telefone),
    # pedido pelo X quando há verificação extra. password = senha.
    await client.login(
        auth_info_1=username,
        auth_info_2=email or username,
        password=password,
    )
    os.makedirs(X_DIR, exist_ok=True)
    client.save_cookies(COOKIES_PATH)

    # metadados públicos da conta logada (best-effort)
    acc = {"screen_name": username.lstrip("@"), "name": username.lstrip("@"), "thumb": ""}
    try:
        me = await client.user()
        acc = {
            "screen_name": getattr(me, "screen_name", "") or acc["screen_name"],
            "name": getattr(me, "name", "") or acc["name"],
            # avatar em tamanho normal; troca _normal por _400x400 fica maior
            "thumb": (getattr(me, "profile_image_url", "") or "").replace("_normal", "_400x400"),
        }
    except Exception:
        pass
    return acc


def _friendly(msg):
    low = (msg or "").lower()
    if "could not authenticate" in low or "incorrect" in low or "password" in low:
        return "Usuário ou senha incorretos (ou o X pediu verificação extra)."
    if "suspended" in low:
        return "Esta conta do X está suspensa."
    if "rate limit" in low or "429" in low:
        return "O X limitou as tentativas agora — espere alguns minutos e tente de novo."
    if "twikit" in low and "instal" in low:
        return msg
    return f"Não foi possível conectar ao X: {msg}"


def login(username, email, password):
    """Faz o login (abre nada no navegador — usa a API interna do X via twikit) e
    salva os cookies. Retorna {'ok':bool, 'account':{...}?, 'error':str?}."""
    username = (username or "").strip()
    email = (email or "").strip()
    password = password or ""
    if not username or not password:
        return {"ok": False, "error": "Informe ao menos o usuário e a senha do X."}
    try:
        acc = asyncio.run(_login_async(username, email, password))
        _save_account(acc)
        return {"ok": True, "account": acc}
    except Exception as e:  # noqa: BLE001 — erro legível para a UI
        return {"ok": False, "error": _friendly(str(e))}


def remove():
    """Desconecta: apaga cookies e metadados da conta."""
    for p in (COOKIES_PATH, ACCOUNT_PATH):
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    return status()


if __name__ == "__main__":
    print(json.dumps(status(), ensure_ascii=False, indent=2))
