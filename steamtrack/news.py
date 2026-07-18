"""Annonces et patch notes via ISteamNews.

Seule partie de l'historique reellement rattrapable a l'ajout d'un jeu : PICS ne
donne que le present, mais ISteamNews rend les dernieres annonces publiees.
La profondeur est limitee (de l'ordre de 200 entrees), pas illimitee.
"""

import html as html_mod
import json
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import db

log = logging.getLogger("news")

NEWS_URL = ("https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
            "?appid={appid}&count={count}&maxlength=0")
USER_AGENT = "steamtrack/1.0"


def clean_bbcode(text):
    """Le contenu Steam est du BBCode : on le rend lisible en texte brut."""
    text = re.sub(r"\[img\][^\[]*\[/img\]", "", text, flags=re.I)
    text = re.sub(r"\[url=([^\]]+)\](.*?)\[/url\]", r"\2 (\1)", text,
                  flags=re.I | re.DOTALL)
    text = re.sub(r"\[/?[a-z][^\]]*\]", "", text, flags=re.I)
    text = html_mod.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fetch(appid, count=200, attempts=3):
    url = NEWS_URL.format(appid=appid, count=count)
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=45) as resp:
                payload = json.load(resp)
            return payload.get("appnews", {}).get("newsitems", [])
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            log.warning("news %s tentative %d/%d : %s", appid, attempt, attempts, exc)
    return []


def backfill(conn, appid, count=200):
    """Enregistre les annonces disponibles. Renvoie le nombre de nouveautes."""
    added = 0
    for item in fetch(appid, count):
        stamp = datetime.fromtimestamp(int(item.get("date", 0)), tz=timezone.utc)
        event = {
            "kind": "news",
            "types": ["news"],
            "title": item.get("title", "(untitled)"),
            "change_number": None,
            "occurred_at": stamp.isoformat(),
            "source": "news",
            "payload": {
                "url": item.get("url", ""),
                "author": item.get("author", ""),
                "feed": item.get("feedlabel", ""),
                "body": clean_bbcode(item.get("contents", "")),
                "gid": item.get("gid"),
            },
        }
        # La deduplication est garantie par l'index unique partiel sur le gid
        # (voir db.dedupe_news) : INSERT OR IGNORE suffit, et c'est atomique --
        # une verification prealable laissait passer les doublons quand deux
        # processus initialisaient le meme jeu simultanement.
        if db.add_change(conn, appid, event):
            added += 1
    return added
