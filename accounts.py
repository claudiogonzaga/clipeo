"""accounts.py — login do YouTube dentro do app, com múltiplos canais.

Fluxo pensado para o usuário final (sem terminal):
  1. O usuário cola, numa tela do app, a credencial OAuth ("client secret"
     baixada do Google Cloud — tipo "Desktop app"). Guardamos em
     ~/.aspis/oauth_client.json. Cada usuário usa a SUA credencial.
  2. Clica em "Conectar": abre o navegador no login do Google. O seletor do
     próprio Google resolve "qual conta" e, em contas com vários canais
     (brand accounts), "qual canal".
  3. Pode conectar VÁRIOS canais (um login por canal). O app lista os canais
     conectados e o usuário marca qual é o ATIVO. A rotina e a leitura usam
     sempre o canal ativo.

Armazenamento (tudo em ~/.aspis/, fora do repositório):
  - oauth_client.json           credencial do app (colada pelo usuário)
  - accounts/index.json         {active, channels:[{channel_id,title,handle,thumb,token_file}]}
  - accounts/token_<id>.json    token OAuth por canal (sensível)

Os tokens nunca são expostos ao JS; a UI só vê metadados de canal.
"""
import json
import os

import config

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]


class ReauthRequired(RuntimeError):
    """A sessão OAuth do canal ativo morreu (invalid_grant, sem refresh token,
    ou token sumiu). Sinaliza à UI para oferecer um BOTÃO de reconexão (que abre
    direto o login do Google) em vez de só mostrar texto de erro."""
    pass


OAUTH_CLIENT = os.path.join(config.USER_DIR, "oauth_client.json")
ACCOUNTS_DIR = os.path.join(config.USER_DIR, "accounts")
INDEX_PATH = os.path.join(ACCOUNTS_DIR, "index.json")

_HERE = os.path.dirname(os.path.abspath(__file__))


def _bundled_client_candidates():
    """A credencial OAuth do app vai embutida no .app (Resources via RESOURCEPATH)
    ou em assets/oauth_client.json quando rodando do código-fonte. Assim o usuário
    final NÃO precisa colar nada: clica "Conectar com o Google" e cai direto na
    página de autorização."""
    cands = []
    res = os.environ.get("RESOURCEPATH")
    if res:
        cands.append(os.path.join(res, "oauth_client.json"))
    cands.append(os.path.join(_HERE, "assets", "oauth_client.json"))
    cands.append(os.path.join(_HERE, "oauth_client.json"))
    return cands


def _bundled_client_path():
    for p in _bundled_client_candidates():
        if os.path.exists(p):
            return p
    return None

_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"


# --- credencial OAuth (colada pelo usuário) --------------------------------
def _normalize_client(text):
    """Aceita o JSON baixado do Google ('installed'/'web') ou um objeto cru
    com client_id/client_secret. Retorna config no formato {'installed': {...}}.
    Lança ValueError com mensagem amigável se inválido."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Cole o conteúdo do arquivo de credencial (client secret JSON).")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("Isso não parece um JSON válido. Cole o arquivo inteiro baixado do Google Cloud.")

    inner = data.get("installed") or data.get("web") or data
    cid = inner.get("client_id")
    secret = inner.get("client_secret")
    if not cid or not secret:
        raise ValueError("Faltou client_id e/ou client_secret. Use a credencial OAuth do tipo 'App para computador'.")

    return {
        "installed": {
            "client_id": cid,
            "client_secret": secret,
            "auth_uri": inner.get("auth_uri", _AUTH_URI),
            "token_uri": inner.get("token_uri", _TOKEN_URI),
            "redirect_uris": inner.get("redirect_uris", ["http://localhost"]),
        }
    }


def save_client(text):
    """Valida e salva a credencial colada. Retorna {'ok':bool, 'error':str?}."""
    try:
        cfg = _normalize_client(text)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    os.makedirs(config.USER_DIR, exist_ok=True)
    with open(OAUTH_CLIENT, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    return {"ok": True}


def client_configured():
    # credencial do usuário (colada) OU a embutida no app
    return os.path.exists(OAUTH_CLIENT) or _bundled_client_path() is not None


def clear_client():
    """Remove a credencial colada (para trocar a key). Não mexe nos canais já
    conectados — eles seguem válidos pelos tokens salvos."""
    if os.path.exists(OAUTH_CLIENT):
        try:
            os.remove(OAUTH_CLIENT)
        except OSError:
            pass
    return status()


def _client_config():
    # prioridade: credencial colada pelo usuário; senão a embutida no app
    path = OAUTH_CLIENT if os.path.exists(OAUTH_CLIENT) else _bundled_client_path()
    if not path:
        raise RuntimeError("Credencial do Google não disponível no app.")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# --- índice de canais -------------------------------------------------------
def _load_index():
    if not os.path.exists(INDEX_PATH):
        return {"active": None, "channels": []}
    try:
        with open(INDEX_PATH, "r", encoding="utf-8") as fh:
            idx = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {"active": None, "channels": []}
    idx.setdefault("active", None)
    idx.setdefault("channels", [])
    return idx


def _save_index(idx):
    os.makedirs(ACCOUNTS_DIR, exist_ok=True)
    with open(INDEX_PATH, "w", encoding="utf-8") as fh:
        json.dump(idx, fh, indent=2)


def _public_channels(idx):
    """Metadados seguros (sem caminho de token) para mandar ao JS."""
    return [
        {
            "channel_id": c["channel_id"],
            "title": c.get("title", ""),
            "handle": c.get("handle", ""),
            "thumb": c.get("thumb", ""),
            "active": c["channel_id"] == idx.get("active"),
        }
        for c in idx.get("channels", [])
    ]


def status():
    """Estado para a UI: credencial configurada? canais conectados? qual ativo?"""
    idx = _load_index()
    return {
        "client_configured": client_configured(),
        "channels": _public_channels(idx),
        "active": idx.get("active"),
    }


def list_channels():
    return _public_channels(_load_index())


def set_active(channel_id):
    idx = _load_index()
    if any(c["channel_id"] == channel_id for c in idx["channels"]):
        idx["active"] = channel_id
        _save_index(idx)
    return status()


def remove(channel_id):
    idx = _load_index()
    keep = []
    for c in idx["channels"]:
        if c["channel_id"] == channel_id:
            tf = os.path.join(ACCOUNTS_DIR, c.get("token_file", ""))
            if c.get("token_file") and os.path.exists(tf):
                try:
                    os.remove(tf)
                except OSError:
                    pass
        else:
            keep.append(c)
    idx["channels"] = keep
    if idx.get("active") == channel_id:
        idx["active"] = keep[0]["channel_id"] if keep else None
    _save_index(idx)
    return status()


# --- conectar (abre navegador) ---------------------------------------------
def connect():
    """Roda o fluxo OAuth (abre o navegador), descobre o canal autenticado e
    o adiciona como canal ativo. BLOQUEIA até o usuário concluir no navegador.
    Retorna {'ok':bool, 'channel':{...}?, 'error':str?}."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        flow = InstalledAppFlow.from_client_config(_client_config(), SCOPES)
        # prompt=consent+select_account: força o seletor de conta/canal E a tela
        # de consentimento — sem o "consent", reconexões de uma conta que já
        # autorizou o app voltam SEM refresh_token e a sessão morre em 1 h.
        # access_type=offline: garante refresh_token para uso pela rotina.
        creds = flow.run_local_server(
            port=0,
            prompt="consent select_account",
            access_type="offline",
            authorization_prompt_message="",
            success_message="Conta conectada ao Aspis. Pode fechar esta aba e voltar ao app.",
        )

        service = build("youtube", "v3", credentials=creds, cache_discovery=False)
        resp = service.channels().list(part="snippet", mine=True, maxResults=1).execute()
        items = resp.get("items", [])
        if not items:
            return {"ok": False, "error": "Nenhum canal encontrado nessa conta."}
        ch = items[0]
        cid = ch["id"]
        sn = ch.get("snippet", {})
        thumbs = sn.get("thumbnails", {})
        thumb = (thumbs.get("default") or thumbs.get("medium") or {}).get("url", "")

        os.makedirs(ACCOUNTS_DIR, exist_ok=True)
        token_file = f"token_{cid}.json"
        with open(os.path.join(ACCOUNTS_DIR, token_file), "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())

        idx = _load_index()
        idx["channels"] = [c for c in idx["channels"] if c["channel_id"] != cid]
        idx["channels"].append({
            "channel_id": cid,
            "title": sn.get("title", ""),
            "handle": sn.get("customUrl", ""),
            "thumb": thumb,
            "token_file": token_file,
        })
        idx["active"] = cid  # canal recém-conectado vira o ativo
        _save_index(idx)

        return {"ok": True, "channel": {
            "channel_id": cid, "title": sn.get("title", ""),
            "handle": sn.get("customUrl", ""), "thumb": thumb,
        }}
    except Exception as e:  # noqa: BLE001 — devolve erro legível à UI
        return {"ok": False, "error": str(e)}


# --- serviço para o canal ativo (usado por youtube.py/routine) -------------
def creds_for_active():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    idx = _load_index()
    active = idx.get("active")
    if not active:
        raise ReauthRequired("Nenhum canal do YouTube conectado. Conecte um canal no app.")
    entry = next((c for c in idx["channels"] if c["channel_id"] == active), None)
    if not entry:
        raise ReauthRequired("Canal ativo inválido. Reconecte no app.")
    label = entry.get("title") or active
    token_path = os.path.join(ACCOUNTS_DIR, entry["token_file"])
    if not os.path.exists(token_path):
        raise ReauthRequired(f"A sessão do canal “{label}” não está mais salva. Reconecte para continuar.")

    creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            from google.auth.exceptions import RefreshError

            try:
                creds.refresh(Request())
            except RefreshError as e:
                # invalid_grant: o Google revogou/expirou o refresh token (acontece
                # em 7 dias quando o app OAuth está em modo "Testing" no Google
                # Cloud). O token morto não serve mais — remove para a UI mostrar
                # o canal como desconectado, e sinaliza reconexão (botão na UI).
                if "invalid_grant" in str(e):
                    try:
                        os.remove(token_path)
                    except OSError:
                        pass
                    raise ReauthRequired(
                        f"A sessão do Google do canal “{label}” expirou. "
                        "Clique para reconectar."
                    ) from e
                raise
            with open(token_path, "w", encoding="utf-8") as fh:
                fh.write(creds.to_json())
        else:
            raise ReauthRequired(f"A sessão do canal “{label}” expirou. Clique para reconectar.")
    return creds


def service_for_active():
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=creds_for_active(), cache_discovery=False)
