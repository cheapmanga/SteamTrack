#!/usr/bin/env python3
"""Import d'un historique SteamDB sauvegarde en HTML.

C'est le seul moyen de recuperer le passe d'un jeu : Steam ne conserve pas les
changelists anciens, et SteamDB bloque tout acces automatise (Cloudflare 403).
On part donc d'une sauvegarde manuelle de la page History, faite depuis le
navigateur (Ctrl+S).

Les evenements produits ont exactement le meme format que ceux du collecteur --
l'interface les rend de la meme façon ; seule leur source (import) les
distingue, et le collecteur prend le relais pour la suite.

    python3 -m steamtrack.cli import 2467880 "Fading Echo History.html"
"""

import json
import re
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path


# Balises dont le contenu textuel devient un segment type dans le rendu.
SEGMENT_TAGS = {"del": "del", "ins": "ins"}

# Medias previsualisables. Les CDN Steam repondent avec
# 'access-control-allow-origin: *', ce qui autorise le telechargement par fetch
# cote navigateur. Les manifestes de streaming adaptatif (.mpd, .m3u8) sont
# volontairement absents : ils ne sont pas lisibles sans bibliotheque dediee.
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".ico", ".bmp")
VIDEO_EXT = (".mp4", ".webm")
MEDIA_HOSTS = ("steamstatic.com", "akamaihd.net", "steamcdn-a.akamaihd.net")


def media_kind(url):
    """'image', 'video', ou None si l'URL ne pointe pas un media affichable."""
    if not url or not url.startswith("https://"):
        return None
    if not any(host in url.split("/")[2] for host in MEDIA_HOSTS):
        return None
    path = url.split("?")[0].lower()
    if path.endswith(IMAGE_EXT):
        return "image"
    if path.endswith(VIDEO_EXT):
        return "video"
    return None


def is_media(url):
    return media_kind(url) is not None

# Mot-cle rencontre dans un evenement -> categorie affichee par le tracker.
# L'ordre fixe la categorie principale (badge et couleur) quand plusieurs
# s'appliquent : le technique prime sur l'editorial, et un changement visuel
# prime sur une retouche de fiche store.
#
# Les mots-cles sont cherches en MOT ENTIER : en sous-chaine, "name" matchait
# jusque dans les URL de trailers et classait des lots d'images en "store".
CATEGORY_RULES = [
    ("build", ("buildid", "timebuildupdated")),
    ("branch", ("branch", "branches", "privatebranches")),
    ("depot", ("depot", "depots", "manifest", "manifests")),
    ("assets", (
        "assets", "screenshots", "trailers", "header_image", "small_capsule",
        "library_capsule", "library_assets", "library_assets_full", "capsule",
        "movie", "logo", "icon", "clienticon",
    )),
    ("store", (
        "store genres", "user tags", "store description", "store release date",
        "supported languages", "name", "associations", "store asset",
        "has a playtest", "price", "franchise", "publisher", "developer",
        "store categories",
    )),
]


class PanelParser(HTMLParser):
    """Reconstruit l'arbre des <li> d'un panneau d'historique SteamDB.

    Le markup imbrique des <ul class="app-history"> dans les <li> pour grouper
    (Depots > branche public > champs). On preserve cette hierarchie : le
    tracker la rend telle quelle, comme SteamDB.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"children": []}
        self.stack = [self.root]        # <li> ouverts, du plus externe au plus interne
        self.fmt = []                   # balises de formatage ouvertes (del/ins/i/span muted)
        self.hrefs = []                 # pile parallele : URL du <a> englobant, si media

    # -- helpers ----------------------------------------------------------
    def _current(self):
        return self.stack[-1]

    def _push_text(self, text, kind=None):
        node = self._current()
        if node is self.root:
            return
        if kind is None:
            kind = self._active_kind()
        href = self._active_href()
        segs = node["seg"]
        # Fusionne avec le segment precedent s'il est du meme type ET pointe le
        # meme media : evite un emiettement en dizaines de fragments pour une
        # seule phrase, sans coller deux liens differents l'un a l'autre.
        if segs and segs[-1]["t"] == kind and segs[-1].get("href") == href:
            segs[-1]["v"] += text
        else:
            seg = {"t": kind, "v": text}
            if href:
                seg["href"] = href
            segs.append(seg)

    def _active_kind(self):
        for kind in reversed(self.fmt):
            if kind:
                return kind
        return "text"

    def _active_href(self):
        for href in reversed(self.hrefs):
            if href:
                return href
        return None

    # -- HTMLParser -------------------------------------------------------
    def _open(self, kind, href=None):
        """Ouvre une balise de formatage. Les deux piles avancent ensemble."""
        self.fmt.append(kind)
        self.hrefs.append(href)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        cls = attrs.get("class", "")

        if tag == "li":
            op = "none"
            for candidate in ("added", "modified", "removed"):
                if f"diff-{candidate}" in cls:
                    op = candidate
            node = {"op": op, "seg": [], "children": []}
            self._current().setdefault("children", []).append(node)
            self.stack.append(node)
            return

        if tag in SEGMENT_TAGS:
            self._open(SEGMENT_TAGS[tag])
            return

        if tag == "i":
            # <i class="history-icon"> est une puce decorative, sans texte.
            # <i class="muted"> porte les tailles lisibles ("(4.53 GiB)").
            if "history-icon" in cls:
                self._open("skip")
            elif "muted" in cls:
                self._open("muted")
            else:
                self._open("field")
            return

        if tag == "a":
            href = attrs.get("href", "")
            # Seuls les liens vers un media sont conserves : ceux vers SteamDB
            # (changelist, depot, patchnotes) n'ont rien a previsualiser et
            # alourdiraient le JSON pour rien.
            media = href if is_media(href) else None
            # Les liens "?" vers la FAQ sont du bruit dans un flux condense.
            if "/faq/" in href:
                self._open("skip")
            elif "del" in cls:
                self._open("del", media)
            elif "ins" in cls:
                self._open("ins", media)
            else:
                self._open(None, media)
            return

        if tag == "span":
            if "muted" in cls:
                self._open("muted")
            elif "branch-name" in cls:
                self._open("branch")
            else:
                self._open(None)
            return

        if tag in ("svg", "path", "template"):
            self._open("skip")

    def handle_endtag(self, tag):
        if tag == "li":
            if len(self.stack) > 1:
                self.stack.pop()
            return
        if tag in ("del", "ins", "i", "a", "span", "svg", "path", "template"):
            if self.fmt:
                self.fmt.pop()
                self.hrefs.pop()

    def handle_data(self, data):
        if self._active_kind() == "skip":
            return
        text = re.sub(r"\s+", " ", data)
        if not text.strip():
            # On garde l'espace simple qui separe deux segments, pas les
            # retours a la ligne du markup.
            if text == " " and self._current().get("seg"):
                self._push_text(" ")
            return
        self._push_text(text)


def clean_tree(nodes):
    """Supprime les segments vides et les <li> qui ne portent plus rien."""
    out = []
    for node in nodes:
        node["children"] = clean_tree(node.get("children", []))
        segs = []
        for seg in node.get("seg", []):
            if seg["t"] == "skip":
                continue
            seg["v"] = re.sub(r"\s+", " ", seg["v"])
            # SteamDB emet les tailles et deltas dans des <i> tantot muted,
            # tantot nus : "(4.53 GiB)", "(+23.22 KiB)". Ce ne sont pas des
            # noms de champ, on les reclasse pour ne pas les mettre en avant.
            if seg["t"] == "field" and seg["v"].lstrip().startswith("("):
                seg["t"] = "muted"
            if seg["t"] == "field":
                seg["v"] = seg["v"].lstrip()
            if seg["v"].strip():
                # Le type de media est resolu ici, une fois, plutot qu'a chaque
                # rendu cote navigateur.
                if seg.get("href"):
                    seg["media"] = media_kind(seg["href"])
                segs.append(seg)
        # Rogne les espaces en bord de ligne.
        if segs:
            segs[0]["v"] = segs[0]["v"].lstrip()
            segs[-1]["v"] = segs[-1]["v"].rstrip()
        node["seg"] = segs
        if segs or node["children"]:
            out.append(node)
    return out


def flatten_text(nodes):
    """Texte brut de l'arbre, pour categoriser et resumer."""
    parts = []
    for node in nodes:
        parts.extend(seg["v"] for seg in node.get("seg", []))
        parts.append(flatten_text(node.get("children", [])))
    return " ".join(parts)


def has_media(nodes):
    """Vrai si l'arbre porte au moins une image ou video."""
    for node in nodes:
        if any(seg.get("href") for seg in node.get("seg", [])):
            return True
        if has_media(node.get("children", [])):
            return True
    return False


def categorize(nodes, text):
    """Toutes les categories applicables, la principale en premier.

    Un changelist Steam touche souvent plusieurs sections a la fois : n'en
    retenir qu'une rendait les filtres menteurs, un lot de capsules classe
    "store" restant introuvable sous "assets".
    """
    lowered = text.lower()
    found = []
    for name, keywords in CATEGORY_RULES:
        if any(re.search(rf"\b{re.escape(kw)}\b", lowered) for kw in keywords):
            found.append(name)

    # La presence reelle d'un media est un signal plus sur qu'un mot-cle : les
    # icones, par exemple, ne declenchent aucune regle textuelle.
    if "assets" not in found and has_media(nodes):
        found.append("assets")

    return found or ["meta"]


def find_buildid(nodes):
    """Recupere le buildid pousse, s'il y en a un dans l'arbre.

    C'est l'information la plus utile d'un patch : elle merite le titre, plutot
    qu'un generique 'Changed Depots'.
    """
    for node in nodes:
        segs = node.get("seg", [])
        for index, seg in enumerate(segs):
            if seg["t"] == "field" and seg["v"].strip().rstrip(":") == "buildid":
                for later in segs[index + 1:]:
                    if later["t"] == "ins":
                        return later["v"].strip()
        found = find_buildid(node.get("children", []))
        if found:
            return found
    return None


def summarize(nodes, text):
    """Titre court de l'evenement, façon 'Changed Depots, User Tags'."""
    buildid = find_buildid(nodes)
    if buildid:
        return f"Build {buildid}"

    labels = []
    for node in nodes:
        for seg in node.get("seg", []):
            if seg["t"] == "field":
                label = seg["v"].strip().rstrip(":")
                if label and label not in labels and not label.startswith("("):
                    labels.append(label)
                break
    labels = [lab for lab in labels if lab != "ChangeNumber"]
    if not labels:
        return "Changenumber only"
    head = ", ".join(labels[:3])
    if len(labels) > 3:
        head += f" +{len(labels) - 3}"
    verb = "Changed"
    if "Added" in text and "Changed" not in text:
        verb = "Added"
    elif "Removed" in text and "Changed" not in text:
        verb = "Removed"
    return f"{verb} {head}"


def parse_panels(html_text, appid):
    start = html_text.find('<div class="history-container">')
    if start < 0:
        sys.exit("Balise history-container introuvable : ce n'est pas une page History SteamDB.")

    chunks = re.split(r'(?=<div class="panel panel-history)', html_text[start:])
    events = []

    for chunk in chunks:
        match = re.match(r'<div class="panel panel-history[^"]*" data-changeid="([^"]+)"', chunk)
        if not match:
            continue
        changeid = match.group(1)

        date = re.search(r'<relative-time[^>]*datetime="([^"]+)"', chunk)
        if not date:
            continue
        date = date.group(1)

        parser = PanelParser()
        parser.feed(chunk)
        tree = clean_tree(parser.root["children"])

        # Le changenumber est un fait technique repete a chaque panneau ; on le
        # sort de l'arbre pour ne pas polluer le rendu de chaque evenement.
        changes = [n for n in tree if not flatten_text([n]).strip().startswith("ChangeNumber")]

        text = flatten_text(changes)
        types = categorize(changes, text) if changes else ["changenumber"]

        events.append({
            "id": f"change:{changeid}",
            # type : categorie principale, celle du badge et de la couleur.
            # types : toutes les categories applicables, sur lesquelles filtre
            # la page, pour qu'un changement mixte reste trouvable sous chacune.
            "type": types[0],
            "types": types,
            "changeid": changeid,
            "title": summarize(changes, text) if changes else "Changenumber only",
            "url": f"https://steamdb.info/app/{appid}/history/?changeid={changeid}",
            "source": "steamdb-history",
            "date": date,
            "changes": changes,
        })

    return events


def merge_panels(events):
    """Regroupe les panneaux d'un meme changelist survenus au meme instant.

    SteamDB decoupe un changelist en plusieurs panneaux, un par section
    modifiee : un meme changeid peut porter separement "Added User File System"
    et "Build 18767038". Les garder distincts obligerait a une cle d'unicite
    bancale, et les ecraser perdait de vraies builds.

    On les reunit donc en un evenement, ce qui correspond exactement a ce que
    produit le collecteur : un changelist, tous ses groupes de modifications.
    """
    merged = {}
    order = []
    for event in events:
        key = (event["changeid"], event["date"])
        if key not in merged:
            merged[key] = event
            order.append(key)
            continue

        target = merged[key]
        target["changes"] = (target.get("changes") or []) + (event.get("changes") or [])
        for kind in event["types"]:
            if kind not in target["types"]:
                target["types"].append(kind)

        # Le titre le plus informatif l'emporte : une build nomme le changement
        # mieux que la section qui l'accompagne.
        if event["title"].startswith("Build "):
            target["title"] = event["title"]
        elif not target["title"].startswith("Build "):
            if event["title"] not in ("Changenumber only", target["title"]):
                base = "" if target["title"] == "Changenumber only" else target["title"] + ", "
                target["title"] = base + event["title"].replace("Changed ", "", 1)

    for key in order:
        event = merged[key]
        # "changenumber" ne vaut que pour un panneau reellement vide : il ne
        # doit pas subsister a cote de vraies categories apres fusion.
        if len(event["types"]) > 1 and "changenumber" in event["types"]:
            event["types"].remove("changenumber")
        event["type"] = event["types"][0]
    return [merged[k] for k in order]


def import_history(conn, appid, html_path):
    """Parse un export SteamDB et enregistre les evenements manquants.

    Renvoie (importes, ignores). Les doublons sont ecartes par la contrainte
    UNIQUE (appid, change_number, source) : reimporter le meme fichier, ou un
    export plus recent qui recouvre l'ancien, n'ajoute que les nouveautes.
    """
    from . import db

    html_text = Path(html_path).read_text(encoding="utf-8", errors="replace")
    events = merge_panels(parse_panels(html_text, appid))

    imported = skipped = 0
    for event in events:
        buildid = find_buildid(event["changes"])
        # Le changeid de SteamDB n'est pas toujours numerique ("U:80867795"
        # pour les changements detectes hors PICS) : on le conserve tel quel,
        # c'est lui qui porte l'unicite.
        ok = db.add_change(conn, appid, {
            "change_number": event["changeid"],
            "kind": event["type"],
            "types": event["types"],
            "title": event["title"],
            "buildid": buildid,
            "occurred_at": event["date"],
            "payload": event["changes"],
            "source": "import",
        })
        if ok:
            imported += 1
        else:
            skipped += 1
    return imported, skipped
