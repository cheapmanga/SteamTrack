"""Compare deux appinfo PICS et en tire des evenements lisibles.

La logique vient du tracker Fading Echo, ou elle a ete eprouvee sur 795
changements reels ; elle est ici generalisee a n'importe quel appid.

Le format de sortie est un arbre de segments types (text, field, del, ins,
muted), que l'interface rend directement et que l'API expose tel quel.
"""

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".ico", ".bmp")
VIDEO_EXT = (".mp4", ".webm")

# Chemins qui bougent sans rien dire d'utile.
NOISE_PATHS = ("common/community_hub_visible",)

# Metadonnees de transport : le changenumber sert de detecteur, le sha et la
# taille sont derives du reste et feraient double emploi dans un diff.
TRANSPORT_KEYS = ("_change_number", "_sha", "_size", "_missing_token", "public_only")

# Fragment de chemin -> categorie. L'ordre fixe la categorie principale ;
# un evenement peut en porter plusieurs.
CATEGORY_RULES = [
    ("build", ("buildid", "timebuildupdated")),
    ("branch", ("branches", "privatebranches")),
    ("depot", ("depots", "manifests")),
    ("assets", (
        "library_assets", "header_image", "small_capsule", "library_capsule",
        "movie", "screenshots", "logo", "store_asset", "icon", "trailer",
        "capsule",
    )),
    ("store", (
        "common/name", "store_tags", "genres", "associations", "release",
        "languages", "price", "supported", "playtest", "franchise", "category",
    )),
]


def asset_url(appid, value):
    """('url', 'image'|'video') si la valeur designe un asset, sinon (None, None).

    PICS stocke les assets en chemin relatif ; le CDN les sert sous cette
    racine, ce qui permet de les previsualiser et de les telecharger.
    """
    if not isinstance(value, str) or "/" not in value or len(value) > 300:
        return None, None
    lowered = value.split("?")[0].lower()
    if lowered.endswith(IMAGE_EXT):
        kind = "image"
    elif lowered.endswith(VIDEO_EXT):
        kind = "video"
    else:
        return None, None
    if value.startswith("https://"):
        return value, kind
    if value.startswith("http://"):
        return None, None
    root = f"https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{appid}/"
    return root + value.lstrip("/"), kind


def flatten(obj, prefix=""):
    """Aplati l'appinfo imbrique en {chemin: valeur}."""
    flat = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            flat.update(flatten(value, f"{prefix}/{key}" if prefix else str(key)))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            flat.update(flatten(value, f"{prefix}/{index}"))
    else:
        flat[prefix] = obj
    return flat


def pretty_path(path):
    parts = path.split("/")
    return " > ".join(parts[:-1]) if len(parts) > 1 else ""


def categorize(paths, has_media=False):
    joined = " ".join(paths).lower()
    found = [name for name, keywords in CATEGORY_RULES
             if any(kw in joined for kw in keywords)]
    # Un asset reellement present prime sur le nom du chemin : les icones, par
    # exemple, ne declenchent aucun mot-cle.
    if has_media and "assets" not in found:
        found.append("assets")
    return found or ["meta"]


def value_seg(appid, kind, value):
    seg = {"t": kind, "v": str(value)}
    url, media = asset_url(appid, value)
    if url:
        seg["href"] = url
        seg["media"] = media
    return seg


def diff(appid, old, new):
    """Rend un evenement, ou None si rien de visible n'a change.

    Renvoie aussi le cas 'opaque' : le changenumber a bouge sans qu'aucun champ
    public ne change, ce qui arrive quand la section depots n'est pas exposee
    (jeu non sorti). On enregistre le fait plutot que de le perdre.
    """
    old_flat, new_flat = flatten(old or {}), flatten(new)
    new_change = new_flat.get("_change_number")
    old_change = old_flat.get("_change_number")

    for key in TRANSPORT_KEYS:
        old_flat.pop(key, None)
        new_flat.pop(key, None)

    groups, touched, buildid, media = {}, [], None, False

    for path in sorted(set(old_flat) | set(new_flat)):
        if any(noise in path for noise in NOISE_PATHS):
            continue
        before, after = old_flat.get(path), new_flat.get(path)
        if before == after:
            continue

        if before is None:
            op, verb = "added", "Added"
        elif after is None:
            op, verb = "removed", "Removed"
        else:
            op, verb = "modified", "Changed"

        touched.append(path)
        field = path.split("/")[-1]
        # Seule la branche public merite le titre : une build sur une branche de
        # test ne doit pas passer pour la version jouable du jour.
        if field == "buildid" and after is not None and "/public/" in f"/{path}/":
            buildid = str(after)

        seg = [{"t": "text", "v": f"{verb} "}, {"t": "field", "v": f"{field}:"}]
        if before is not None:
            s = value_seg(appid, "del", before)
            media = media or "href" in s
            seg.append(s)
        if before is not None and after is not None:
            seg.append({"t": "text", "v": " > "})
        if after is not None:
            s = value_seg(appid, "ins", after)
            media = media or "href" in s
            seg.append(s)

        groups.setdefault(pretty_path(path), []).append(
            {"op": op, "seg": seg, "children": []}
        )

    if not touched:
        if new_change and new_change != old_change and old is not None:
            return _opaque(old_change, new_change)
        return None

    changes = []
    for group, nodes in sorted(groups.items()):
        if group:
            changes.append({
                "op": "none",
                "seg": [{"t": "text", "v": "Changed "}, {"t": "field", "v": group}],
                "children": nodes,
            })
        else:
            changes.extend(nodes)

    types = categorize(touched, media)
    return {
        "kind": types[0],
        "types": types,
        "title": f"Build {buildid}" if buildid else _summarize(touched),
        "buildid": buildid,
        "change_number": new_change,
        "payload": changes,
        "source": "pics",
    }


def _summarize(paths):
    fields = []
    for path in paths:
        field = path.split("/")[-1]
        if field.isdigit() and "/" in path:
            field = path.split("/")[-2]
        if field not in fields:
            fields.append(field)
    head = ", ".join(fields[:3])
    if len(fields) > 3:
        head += f" +{len(fields) - 3}"
    return f"Changed {head}"


def _opaque(old_change, new_change):
    """Changelist publie sans rien de visible dans l'appinfo public.

    Steam n'expose pas la section depots des apps a jeton (typiquement les jeux
    non encore sortis) : on constate qu'une build existe sans pouvoir en lire le
    numero, plutot que de perdre l'evenement.
    """
    return {
        "kind": "build",
        "types": ["build"],
        "title": "Build pushed (content not public)",
        "buildid": None,
        "change_number": new_change,
        "opaque": True,
        "source": "pics",
        "payload": [{
            "op": "modified",
            "seg": [
                {"t": "text", "v": "Changed "},
                {"t": "field", "v": "ChangeNumber:"},
                {"t": "del", "v": str(old_change or "?")},
                {"t": "text", "v": " > "},
                {"t": "ins", "v": str(new_change)},
            ],
            "children": [{
                "op": "none",
                "seg": [{
                    "t": "muted",
                    "v": "No change in the public appinfo: this app requires a "
                         "token, so depots and buildid are not exposed.",
                }],
                "children": [],
            }],
        }],
    }
