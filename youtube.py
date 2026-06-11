"""youtube.py — leitura das inscrições e dos vídeos novos via YouTube Data API v3.

Default: OAuth (escopo youtube.readonly) para ler as inscrições do usuário
automaticamente. No primeiro uso abre o navegador para consentimento e salva o
token em token.json; depois reaproveita/atualiza.

Modo alternativo (sem OAuth): se config youtube.channels_manuais estiver
preenchido, usamos uma API key e essa lista fixa de canais.

Quota: subscriptions/channels/playlistItems/videos custam 1 unidade/chamada.
Nunca usamos search.list (100 unidades).
"""
import re
from datetime import datetime, timezone

from config import load

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]


# --- duração ISO 8601 → "mm min" -------------------------------------------
_ISO_DUR = re.compile(
    r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?"
)


def iso_duration_to_human(iso):
    if not iso:
        return ""
    m = _ISO_DUR.match(iso)
    if not m:
        return ""
    days, hours, minutes, seconds = (int(x) if x else 0 for x in m.groups())
    total_min = days * 24 * 60 + hours * 60 + minutes + (1 if seconds >= 30 else 0)
    if total_min >= 60:
        h, mm = divmod(total_min, 60)
        return f"{h} h {mm:02d} min" if mm else f"{h} h"
    return f"{total_min} min"


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


# --- construção do client ---------------------------------------------------
# O login OAuth multi-canal vive em accounts.py (conectado pelo app). Aqui só
# resta o modo manual por API key, e o get_service() que escolhe entre os dois.
def _apikey_service(cfg):
    import os

    from googleapiclient.discovery import build

    key = os.environ.get(cfg["youtube"].get("api_key_env", "YOUTUBE_API_KEY"))
    if not key:
        raise RuntimeError(
            "Modo manual exige YOUTUBE_API_KEY no ambiente (ou use OAuth)."
        )
    return build("youtube", "v3", developerKey=key, cache_discovery=False)


def get_service(cfg=None):
    cfg = cfg or load()
    # Modo manual (API key + lista fixa de canais) continua disponível.
    if cfg["youtube"].get("channels_manuais"):
        return _apikey_service(cfg)
    # Default: usa o canal ATIVO conectado pelo app (accounts.py).
    import accounts

    return accounts.service_for_active()


# --- API calls --------------------------------------------------------------
def get_subscriptions(service):
    """Retorna lista de channel_id das inscrições do usuário (paginado)."""
    channel_ids = []
    page_token = None
    while True:
        resp = (
            service.subscriptions()
            .list(part="snippet", mine=True, maxResults=50, pageToken=page_token)
            .execute()
        )
        for item in resp.get("items", []):
            cid = item["snippet"]["resourceId"]["channelId"]
            channel_ids.append(cid)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return channel_ids


def _thumb_url(snippet):
    thumbs = snippet.get("thumbnails", {})
    return (thumbs.get("default") or thumbs.get("medium") or {}).get("url", "")


def get_uploads_playlists(service, channel_ids):
    """Retorna (uploads, thumbs): channel_id → uploads playlist id e
    channel_id → URL do avatar do canal. Lotes de 50, 1 unidade por lote
    (o part=snippet extra não custa quota adicional)."""
    uploads_map, thumbs = {}, {}
    for batch in _chunks(channel_ids, 50):
        resp = (
            service.channels()
            .list(part="contentDetails,snippet", id=",".join(batch), maxResults=50)
            .execute()
        )
        for item in resp.get("items", []):
            uploads = (
                item.get("contentDetails", {})
                .get("relatedPlaylists", {})
                .get("uploads")
            )
            if uploads:
                uploads_map[item["id"]] = uploads
            url = _thumb_url(item.get("snippet", {}))
            if url:
                thumbs[item["id"]] = url
    return uploads_map, thumbs


def get_channel_thumbs(service, channel_ids):
    """channel_id → URL do avatar. Usado para preencher vídeos antigos no banco."""
    out = {}
    for batch in _chunks(list(channel_ids), 50):
        resp = (
            service.channels()
            .list(part="snippet", id=",".join(batch), maxResults=50)
            .execute()
        )
        for item in resp.get("items", []):
            url = _thumb_url(item.get("snippet", {}))
            if url:
                out[item["id"]] = url
    return out


def get_new_video_ids(service, playlist_id, since_dt, max_videos):
    """IDs de vídeos publicados depois de `since_dt` numa uploads playlist."""
    ids = []
    page_token = None
    while True:
        resp = (
            service.playlistItems()
            .list(
                part="snippet,contentDetails",
                playlistId=playlist_id,
                maxResults=min(50, max_videos) if max_videos else 50,
                pageToken=page_token,
            )
            .execute()
        )
        stop = False
        for item in resp.get("items", []):
            published = item["snippet"].get("publishedAt")
            vid = item["contentDetails"].get("videoId")
            if not published or not vid:
                continue
            pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if pub_dt > since_dt:
                ids.append(vid)
            else:
                # uploads vêm em ordem decrescente de data → pode parar
                stop = True
            if max_videos and len(ids) >= max_videos:
                stop = True
                break
        page_token = resp.get("nextPageToken")
        if stop or not page_token:
            break
    return ids


def hydrate(service, video_ids):
    """videos.list em lotes de 50. Retorna lista de dicts com metadados."""
    out = []
    for batch in _chunks(list(video_ids), 50):
        resp = (
            service.videos()
            .list(part="snippet,contentDetails,statistics", id=",".join(batch))
            .execute()
        )
        for item in resp.get("items", []):
            sn = item.get("snippet", {})
            cd = item.get("contentDetails", {})
            st = item.get("statistics", {})
            out.append(
                {
                    "video_id": item["id"],
                    "title": sn.get("title", ""),
                    "channel": sn.get("channelTitle", ""),
                    "channel_id": sn.get("channelId", ""),
                    "description": sn.get("description", ""),
                    "published_at": sn.get("publishedAt", ""),
                    "duration": iso_duration_to_human(cd.get("duration", "")),
                    "views": int(st.get("viewCount", 0)) if st.get("viewCount") else 0,
                    "url": f"https://www.youtube.com/watch?v={item['id']}",
                }
            )
    return out


def fetch_new_videos(since_dt, cfg=None):
    """Pipeline de leitura: inscrições → uploads → novos → hidrata.
    Retorna lista de dicts de vídeo (metadados), prontos para o brain."""
    cfg = cfg or load()
    service = get_service(cfg)
    max_per = cfg.get("max_videos_per_channel", 8)

    manual = cfg["youtube"].get("channels_manuais")
    channel_ids = manual if manual else get_subscriptions(service)

    uploads, thumbs = get_uploads_playlists(service, channel_ids)
    new_ids = []
    for _cid, playlist_id in uploads.items():
        new_ids.extend(get_new_video_ids(service, playlist_id, since_dt, max_per))

    # dedup preservando ordem
    seen = set()
    new_ids = [v for v in new_ids if not (v in seen or seen.add(v))]
    videos = hydrate(service, new_ids)
    for v in videos:
        v["channel_thumb"] = thumbs.get(v["channel_id"], "")
    return videos


if __name__ == "__main__":
    from datetime import timedelta

    since = datetime.now(timezone.utc) - timedelta(hours=36)
    vids = fetch_new_videos(since)
    print(f"{len(vids)} vídeos novos desde {since.isoformat()}")
    for v in vids[:10]:
        print(f"  [{v['duration']}] {v['channel']} — {v['title']}")
