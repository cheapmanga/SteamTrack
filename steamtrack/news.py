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

    # Les puces de liste s'ecrivent [*] : elles ne commencent pas par une
    # lettre et survivaient donc au nettoyage generique ci-dessous, laissant
    # des "[*]" en clair au milieu des patch notes.
    text = re.sub(r"\[/?list[^\]]*\]", "\n", text, flags=re.I)
    # La fermante [/*] doit partir AVANT l'ouvrante, sinon le "/" reste. Ni
    # l'une ni l'autre ne commencent par une lettre, donc le nettoyage
    # generique plus bas ne les voit pas.
    text = re.sub(r"\[/\*\]", "", text)
    text = re.sub(r"\[\*\]\s*", "\n- ", text)

    # Les titres et separateurs meritent un saut de ligne, sinon tout le patch
    # note se retrouve sur un seul paragraphe illisible.
    text = re.sub(r"\[/?h[1-6]\]", "\n", text, flags=re.I)
    text = re.sub(r"\[hr\]\[/hr\]|\[hr\]", "\n", text, flags=re.I)

    text = re.sub(r"\[/?[a-z][^\]]*\]", "", text, flags=re.I)
    # Steam echappe parfois les crochets litteraux : "\[ MAPS ]".
    text = text.replace("\\[", "[").replace("\\]", "]")
    text = html_mod.unescape(text)

    text = re.sub(r"[ \t]+\n", "\n", text)
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


def reclean(conn, appid=None):
    """Repasse le nettoyage BBCode sur les annonces deja enregistrees.

    Le corps est fige a l'import : ameliorer clean_bbcode ne corrige pas
    l'existant, et re-importer ne suffit pas non plus puisque la
    deduplication ecarte les annonces connues.
    """
    sql = "SELECT id, payload FROM changes WHERE source = 'news'"
    args = []
    if appid:
        sql += " AND appid = ?"
        args.append(appid)

    fixed = 0
    for row in conn.execute(sql, args).fetchall():
        payload = json.loads(row["payload"])
        raw = payload.get("raw") or payload.get("body") or ""
        cleaned = clean_bbcode(raw)
        if cleaned != payload.get("body"):
            payload["body"] = cleaned
            conn.execute("UPDATE changes SET payload = ? WHERE id = ?",
                         (json.dumps(payload, ensure_ascii=False), row["id"]))
            fixed += 1
    return fixed


def poll(conn, appid, count=20):
    """Verifie s'il est paru une annonce depuis le dernier passage.

    Meme mecanique que backfill, mais sur les dernieres entrees seulement :
    en regime permanent on cherche une nouveaute, pas a reconstruire deux cents
    annonces a chaque tour. Le dedoublonnage par gid rend l'appel sans effet
    quand rien n'est paru, donc le seul cout est la requete HTTP.

    Une fenetre de 20 laisse une marge tres large : il faudrait que vingt
    annonces paraissent entre deux passages pour en perdre une.
    """
    return backfill(conn, appid, count=count)


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
