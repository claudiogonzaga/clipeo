"""x.py — posts novos do X (Twitter) via twikit (sessão logada).

Espelha youtube.fetch_new_videos: devolve uma lista de "posts" (dicts) com os
metadados que o brain e o store esperam. Sem API oficial — usa a SUA sessão
(cookies salvos por x_accounts.py).

Duas fontes possíveis (config `x.accounts`):
  - vazio  → seu timeline "Seguindo" (cronológico) — o escudo sobre o feed.
  - lista  → só os @perfis escolhidos (menos volume, menos ruído).

twikit é assíncrono. Todo o trabalho de rede acontece dentro de UM asyncio.run
(o Client/httpx fica preso a um event loop; não o atravessamos entre loops).
"""
import asyncio
from datetime import datetime, timezone

import x_accounts
from config import get_x_config, load


def _parse_dt(tweet):
    """Data do tweet como datetime aware (UTC). twikit expõe
    created_at_datetime; com fallback ao formato textual do X."""
    dt = getattr(tweet, "created_at_datetime", None)
    if dt is not None:
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    raw = getattr(tweet, "created_at", None)
    if raw:
        try:  # "Wed Oct 10 20:19:24 +0000 2018"
            return datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
        except ValueError:
            pass
    return None


def _tweet_to_post(t):
    """Normaliza um Tweet do twikit para o dict que a rotina/brain consomem."""
    u = getattr(t, "user", None)
    handle = (getattr(u, "screen_name", "") if u else "") or ""
    name = (getattr(u, "name", "") if u else "") or handle
    thumb = (getattr(u, "profile_image_url", "") if u else "") or ""
    text = getattr(t, "full_text", None) or getattr(t, "text", "") or ""
    tid = str(getattr(t, "id", "") or "")
    url = (
        f"https://x.com/{handle}/status/{tid}" if handle
        else f"https://x.com/i/status/{tid}"
    )
    return {
        "video_id": tid,
        "channel": name,
        "channel_id": handle,
        "channel_thumb": thumb.replace("_normal", "_400x400") if thumb else "",
        "text": text,
        "url": url,
        "published_at": "",  # preenchido em _collect com a data parseada
        "likes": int(getattr(t, "favorite_count", 0) or 0),
        "retweets": int(getattr(t, "retweet_count", 0) or 0),
    }


async def _gather_tweets(client, xcfg):
    """Coleta os tweets crus conforme a config (contas escolhidas x timeline)."""
    accounts = xcfg["accounts"]
    max_per = xcfg["max_posts_per_account"]
    tweets = []
    if accounts:
        for handle in accounts:
            try:
                user = await client.get_user_by_screen_name(handle)
                got = await user.get_tweets("Tweets", count=max_per)
                tweets.extend(list(got))
            except Exception:
                # uma conta inacessível não derruba as demais
                continue
    else:
        # "Seguindo" (cronológico) é mais fiel ao escudo que o "Para você".
        try:
            got = await client.get_latest_timeline(count=max_per)
        except Exception:
            got = await client.get_timeline(count=max_per)
        tweets.extend(list(got))
    return tweets


async def _fetch_async(since_dt, xcfg, max_total):
    client = x_accounts.build_client_with_cookies()
    raw = await _gather_tweets(client, xcfg)

    out, seen = [], set()
    min_likes = xcfg["min_likes"]
    for t in raw:
        p = _tweet_to_post(t)
        if not p["video_id"] or p["video_id"] in seen:
            continue
        dt = _parse_dt(t)
        if dt is None:
            continue
        if since_dt and dt <= since_dt:
            continue
        if min_likes and p["likes"] < min_likes:
            continue
        p["published_at"] = dt.isoformat()
        seen.add(p["video_id"])
        out.append(p)
        if max_total and len(out) >= max_total:
            break
    # mais novos primeiro
    out.sort(key=lambda p: p["published_at"], reverse=True)
    return out


def fetch_new_posts(since_dt, cfg=None, max_total=None):
    """Pipeline de leitura do X: sessão → timeline/contas → filtra por data e
    curtidas → dicts prontos para o brain. Síncrono (envelopa o async)."""
    cfg = cfg or load()
    xcfg = get_x_config()
    return asyncio.run(_fetch_async(since_dt, xcfg, max_total))


async def _fetch_one_async(tweet_id):
    client = x_accounts.build_client_with_cookies()
    t = await client.get_tweet_by_id(str(tweet_id))
    if t is None:
        return None
    p = _tweet_to_post(t)
    dt = _parse_dt(t)
    p["published_at"] = dt.isoformat() if dt else ""
    return p


def fetch_post(tweet_id, cfg=None):
    """Busca UM post avulso pelo id (link colado pelo usuário). Devolve o dict
    no mesmo formato de fetch_new_posts, ou None se não encontrado. Síncrono."""
    return asyncio.run(_fetch_one_async(tweet_id))


if __name__ == "__main__":
    from datetime import timedelta

    since = datetime.now(timezone.utc) - timedelta(hours=36)
    posts = fetch_new_posts(since)
    print(f"{len(posts)} posts novos desde {since.isoformat()}")
    for p in posts[:10]:
        print(f"  @{p['channel_id']} ({p['likes']}♥): {p['text'][:80]}")
