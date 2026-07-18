"""Collecteur : suit le flux PICS de Steam et enregistre les changements.

Fonctionnement, calque sur celui de SteamDB :
  - un client Steam en login ANONYME (aucun compte requis) ;
  - abonnement au flux des changelists, qui annonce en continu quels apps
    viennent d'etre modifies ;
  - pour les apps suivis, on recharge l'appinfo et on le compare au dernier
    snapshot connu ; le diff devient un evenement.

Interroger chaque jeu en boucle serait impossible a l'echelle de Steam : c'est
le flux qui nous dit quoi recharger, et lui seul.
"""

import gevent.monkey
gevent.monkey.patch_all()

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone

from steam.client import SteamClient

from . import db, diff, news, probes

log = logging.getLogger("collector")

# Steam refuse les gros lots : on recharge l'appinfo par paquets.
BATCH = 50
# Filet si le flux se tait : on repasse quand meme sur les apps suivis.
IDLE_SWEEP_S = 1800
# Frequentation : assez frequent pour dessiner une courbe, assez espace pour ne
# pas marteler l'API ni gonfler la base.
PLAYERS_EVERY_S = 600
# Fiche store et prix : ils bougent rarement, inutile d'y revenir souvent.
DETAILS_EVERY_S = 21600


class Collector:
    def __init__(self, conn):
        self.conn = conn
        self.client = SteamClient()
        self.running = True
        self.last_players = 0.0
        self.last_details = 0.0

    # -- Steam ------------------------------------------------------------
    def connect(self):
        # Idempotent : bootstrap() peut avoir deja ouvert la session avant que
        # run() ne prenne la main, et Steam refuse un second login.
        if self.client.logged_on:
            return
        log.info("connexion a Steam (login anonyme)")
        result = self.client.anonymous_login()
        if result != 1:
            raise RuntimeError(f"login anonyme refuse : {result}")
        log.info("connecte, steam_id=%s", self.client.steam_id)

    def fetch_appinfo(self, appids):
        """Recharge l'appinfo d'une liste d'apps, par lots."""
        out = {}
        appids = list(appids)
        for i in range(0, len(appids), BATCH):
            chunk = appids[i:i + BATCH]
            try:
                info = self.client.get_product_info(apps=chunk, timeout=60)
            except Exception as exc:                      # noqa: BLE001
                log.warning("appinfo %s : %s", chunk, exc)
                continue
            out.update(info.get("apps", {}) or {})
        return out

    # -- Traitement -------------------------------------------------------
    def process(self, appids, reason=""):
        """Diffe les apps donnes contre leur snapshot et enregistre le resultat."""
        tracked = db.tracked_ids(self.conn)
        targets = [a for a in appids if a in tracked]
        if not targets:
            return 0

        log.info("%d app(s) a examiner %s", len(targets), reason)
        recorded = 0

        for appid, info in self.fetch_appinfo(targets).items():
            appid = int(appid)
            name = (info.get("common") or {}).get("name", "")
            previous = db.get_snapshot(self.conn, appid)

            event = diff.diff(appid, previous, info)
            if event:
                event["occurred_at"] = datetime.now(timezone.utc).isoformat()
                if db.add_change(self.conn, appid, event):
                    recorded += 1
                    log.info("  [%s] %s : %s", appid, event["kind"], event["title"])

            db.put_snapshot(self.conn, appid, info, info.get("_change_number"))
            db.touch_app(self.conn, appid,
                         change_number=info.get("_change_number"),
                         name=name,
                         missing_token=bool(info.get("_missing_token")))
        return recorded

    def bootstrap(self, appid):
        """Premier enregistrement d'un jeu : etat courant + annonces.

        Aucun evenement de diff n'est cree ici. Sans snapshot precedent, tout
        l'appinfo paraitrait neuf et le jeu s'ajouterait avec un faux "tout a
        change" -- c'est son point de depart, pas un changement.
        """
        info = self.fetch_appinfo([appid]).get(appid) or self.fetch_appinfo([appid]).get(str(appid))
        if not info:
            return None

        name = (info.get("common") or {}).get("name", "")
        db.put_snapshot(self.conn, appid, info, info.get("_change_number"))
        db.touch_app(self.conn, appid,
                     change_number=info.get("_change_number"),
                     name=name,
                     missing_token=bool(info.get("_missing_token")))

        added = news.backfill(self.conn, appid)
        return {"name": name, "news": added,
                "missing_token": bool(info.get("_missing_token"))}

    def run_probes(self, force=False):
        """Releve frequentation et prix, chacun a son rythme.

        Ces mesures ne se rattrapent pas : personne ne republie le nombre de
        joueurs d'hier. Un releve manque est perdu, d'ou leur place dans la
        boucle plutot que dans une tache separee qu'on oublierait de lancer.
        """
        now = time.time()
        apps = list(db.tracked_ids(self.conn))
        if not apps:
            return

        if force or now - self.last_players >= PLAYERS_EVERY_S:
            measured = 0
            for appid in apps:
                if probes.sample_players(self.conn, appid) is not None:
                    measured += 1
            self.last_players = now
            if measured:
                log.info("frequentation relevee pour %d/%d app(s)", measured, len(apps))
            probes.prune_players(self.conn)

        if force or now - self.last_details >= DETAILS_EVERY_S:
            for appid in apps:
                probes.sample_details(self.conn, appid)
            self.last_details = now
            log.info("fiches store rafraichies (%d app(s))", len(apps))

    def bootstrap_pending(self):
        """Complete les jeux suivis qui n'ont pas encore de snapshot."""
        rows = self.conn.execute(
            """SELECT a.appid FROM apps a
               LEFT JOIN snapshots s ON s.appid = a.appid
               WHERE s.appid IS NULL"""
        ).fetchall()
        for row in rows:
            appid = row["appid"]
            log.info("initialisation de l'app %s (ajoutee via l'API)", appid)
            try:
                result = self.bootstrap(appid)
            except Exception as exc:                      # noqa: BLE001
                log.warning("  echec, nouvel essai au prochain tour : %s", exc)
                continue
            if result:
                log.info("  %s : etat enregistre, %d annonce(s)",
                         result["name"] or appid, result["news"])
            else:
                log.warning("  appinfo introuvable pour %s", appid)

    # -- Boucle -----------------------------------------------------------
    def run(self):
        self.connect()

        last = db.get_state(self.conn, "change_number")
        if last is None:
            # Premier demarrage : on part du present, sans tenter de rejouer un
            # passe que Steam ne conserve de toute façon pas.
            current = self.client.get_changes_since(0, True, False)
            last = current.current_change_number
            db.set_state(self.conn, "change_number", last)
            log.info("demarrage au changenumber %s", last)
        last = int(last)

        idle = seen_apps = seen_lists = 0
        self.bootstrap_pending()
        # Un premier releve immediat : sans lui, un redemarrage laisse un trou
        # de dix minutes dans la courbe.
        self.run_probes(force=True)

        while self.running:
            # Un jeu ajoute par l'API arrive sans snapshot : l'API ne joint pas
            # Steam elle-meme. On le complete ici, dans le seul processus qui a
            # le droit de le faire.
            self.bootstrap_pending()
            self.run_probes()

            try:
                resp = self.client.get_changes_since(last, True, False)
            except Exception as exc:                      # noqa: BLE001
                log.warning("flux interrompu (%s), reconnexion", exc)
                self.client.sleep(15)
                self.reconnect()
                continue

            if resp and resp.current_change_number > last:
                appids = [a.appid for a in resp.app_changes]
                if resp.force_full_app_update:
                    # Notre curseur est trop vieux pour Steam : on repasse sur
                    # tous les jeux suivis plutot que de manquer des changements.
                    log.info("full update demande par Steam")
                    appids = list(db.tracked_ids(self.conn))

                # Sans cette trace, un collecteur qui tourne mais ne croise
                # aucun jeu suivi est indiscernable d'un collecteur en panne.
                hits = len(set(appids) & db.tracked_ids(self.conn))
                seen_apps += len(appids)
                seen_lists += 1
                log.info("changelist %s : %d apps, %d suivi(s)%s",
                         resp.current_change_number, len(appids), hits,
                         "" if hits else " -- rien a faire")

                self.process(appids, f"(changelist {resp.current_change_number})")
                last = resp.current_change_number
                db.set_state(self.conn, "change_number", last)
                idle = 0
            else:
                idle += 5
                # Bilan periodique : le service doit prouver qu'il vit meme
                # quand aucun jeu suivi ne bouge.
                if idle % 300 == 0:
                    log.info("actif : %d changelists, %d apps vus, curseur %s",
                             seen_lists, seen_apps, last)

            if idle >= IDLE_SWEEP_S:
                self.process(db.tracked_ids(self.conn), "(balayage periodique)")
                idle = 0

            self.client.sleep(5)

    def reconnect(self):
        try:
            self.client.logout()
        except Exception:                                 # noqa: BLE001
            pass
        self.connect()

    def stop(self, *_):
        log.info("arret demande")
        self.running = False


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    conn = db.connect(args.db)
    collector = Collector(conn)
    signal.signal(signal.SIGINT, collector.stop)
    signal.signal(signal.SIGTERM, collector.stop)

    try:
        collector.run()
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
