"""Reconstitution des depots a partir de l'historique importe.

Steam ne publie pas la section `depots` des apps a jeton -- typiquement les jeux
non encore sortis. L'historique importe de SteamDB, lui, contient ces
changements : la creation des depots, chaque changement de buildid, chaque
variation de taille.

En rejouant ces evenements dans l'ordre, on retrouve l'etat courant. C'est le
principe d'un journal d'evenements : l'etat n'est que la somme de ce qui s'est
produit.

Ce qui est reconstitue est marque comme tel. Un depot deduit de l'historique et
un depot lu en direct dans PICS n'ont pas la meme fraicheur, et l'interface doit
pouvoir le dire.
"""

import json
import re

# "Depot 2467881 download", "Depot 2467881 config/oslist"
DEPOT_FIELD = re.compile(r"^Depot\s+(\d+)\s+(.+)$")
# Une date lisible plutot qu'un horodatage : SteamDB affiche "8 July 2026 - 10:33:43 UTC"
DATE_LIKE = re.compile(r"^\d{1,2}\s+\w+\s+\d{4}")


def replay(conn, appid):
    """Rejoue les evenements importes et rend l'etat final, champ par champ."""
    rows = conn.execute(
        """SELECT payload FROM changes
           WHERE appid = ? AND source = 'import'
           ORDER BY occurred_at""",
        (appid,),
    ).fetchall()

    state = {}

    def walk(nodes, path):
        for node in nodes:
            segs = node.get("seg", [])
            label = "".join(s["v"] for s in segs).strip()
            field = next((s["v"].strip().rstrip(":")
                          for s in segs if s["t"] == "field"), None)
            # ins porte la valeur apres changement ; une ligne sans ins est une
            # suppression, qu'on traduit par un retrait de la cle.
            ins = [s["v"].strip() for s in segs if s["t"] == "ins"]
            removed = node.get("op") == "removed"

            if node.get("children"):
                group = re.sub(r"^(Changed|Added|Removed)\s+", "", label).strip()
                walk(node["children"], path + [group])
            elif field:
                key = "/".join(path + [field])
                if removed:
                    state.pop(key, None)
                elif ins:
                    state[key] = ins[-1]

    for row in rows:
        walk(json.loads(row["payload"]), [])
    return state


def depots_from_history(conn, appid):
    """Rend la section depots reconstituee, au format de /v1/apps/{id}/depots.

    Renvoie None s'il n'y a rien a reconstituer -- aucun historique importe, ou
    un historique qui ne parle pas de depots.
    """
    state = replay(conn, appid)
    if not state:
        return None

    depots = {}
    branches = {}

    for key, value in state.items():
        parts = key.split("/")
        if parts[0] != "Depots":
            continue

        # Un champ situe sous "<nom> branch" appartient a cette branche.
        branch = None
        rest = parts[1:]
        if rest and rest[0].endswith(" branch"):
            branch = rest[0][: -len(" branch")].strip()
            rest = rest[1:]
        if not rest:
            continue

        field = "/".join(rest)
        match = DEPOT_FIELD.match(field)

        if match:
            depot_id, attr = int(match.group(1)), match.group(2).strip()
            entry = depots.setdefault(depot_id, {"depot": depot_id, "config": {}})
            if attr.startswith("config/"):
                entry["config"][attr.split("/", 1)[1]] = value
            elif attr in ("size", "download"):
                # Ces valeurs ne concernent qu'une branche donnee.
                slot = branches.setdefault(branch or "public", {"name": branch or "public"})
                slot.setdefault("depots", {}).setdefault(depot_id, {})[attr] = _int(value)
            elif attr == "gid":
                slot = branches.setdefault(branch or "public", {"name": branch or "public"})
                slot.setdefault("depots", {}).setdefault(depot_id, {})["manifest"] = value
            else:
                entry[attr] = value
        elif branch:
            slot = branches.setdefault(branch, {"name": branch})
            slot[field] = value

    if not depots and not branches:
        return None

    # La taille d'un depot est celle de la branche publique : c'est ce que
    # SteamDB affiche dans sa colonne, et ce qu'on attend en lisant la liste.
    public = branches.get("public", {}).get("depots", {})
    for depot_id, entry in depots.items():
        sizes = public.get(depot_id, {})
        entry["size"] = sizes.get("size")
        entry["download"] = sizes.get("download")
        entry["manifest"] = sizes.get("manifest")
        entry["shared"] = entry.get("sharedinstall") == "1"
        entry["depotfromapp"] = entry.get("depotfromapp")

    return {
        "depots": [depots[k] for k in sorted(depots)],
        "branches": [
            {
                "name": b.get("name"),
                "buildid": b.get("buildid"),
                "description": b.get("description"),
                "updated": b.get("timeupdated"),
                "protected": b.get("pwdrequired") == "1",
            }
            for b in branches.values()
        ],
    }


def _int(value):
    try:
        return int(str(value).replace(" ", ""))
    except (TypeError, ValueError):
        return None
