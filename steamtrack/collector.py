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
# Annonces : PICS ne les annonce PAS. Une news publiee sur Steam ne produit
# aucun changelist, donc rien dans la boucle principale ne la ferait apparaitre
# -- jusqu'ici elles n'etaient lues qu'une fois, a l'ajout du jeu. D'ou ce
# sondage dedie. Cinq minutes : ISteamNews est un endpoint public et leger, et
# c'est le delai maximum entre la publication d'un patch note et son affichage.
NEWS_EVERY_S = 300
# Profondeur du sondage. Large marge : il faudrait vingt annonces entre deux
# passages pour en manquer une.
NEWS_POLL_COUNT = 20

# Apps sous surveillance rapprochee : le flux PICS reste la source principale,
# mais on repasse aussi sur elles regulierement. Utile a l'approche d'une
# sortie, quand la section depots peut s'ouvrir d'un coup sans qu'un changelist
# visible ne l'annonce.
WATCH_CLOSELY = {2467880}          # Fading Echo, sortie le 21 juillet 2026
WATCH_EVERY_S = 120


class Collector:
    def __init__(self, conn):
        self.conn = conn
        self.client = SteamClient()
        self.running = True
        self.last_players = 0.0
        self.last_details = 0.0
        self.last_watch = 0.0
        self.last_news = 0.0

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

    def poll_news(self, force=False):
        """Detecte les annonces parues depuis le dernier passage.

        Separe de run_probes a dessein : une frequentation manquee est perdue,
        une annonce manquee ne l'est pas -- ISteamNews la rendra encore au
        prochain tour. Les deux n'ont donc ni la meme urgence ni le meme
        traitement en cas d'echec, et les melanger ferait croire l'inverse.

        Un jeu sans snapshot est ignore : il n'a pas encore ete initialise, et
        bootstrap_pending fera de toute facon son backfill complet.
        """
        now = time.time()
        if not force and now - self.last_news < NEWS_EVERY_S:
            return
        self.last_news = now

        for appid in db.tracked_ids(self.conn):
            try:
                added = news.poll(self.conn, appid, count=NEWS_POLL_COUNT)
            except Exception as exc:                      # noqa: BLE001
                # Une annonce ratee se rattrape au tour suivant : rien ici ne
                # justifie d'interrompre la boucle du collecteur.
                log.warning("annonces %s : %s", appid, exc)
                continue
            if added:
                log.info("%d annonce(s) pour l'app %s", added, appid)

    def watch_closely(self):
        """Repasse frequemment sur les apps a surveiller de pres.

        Le flux PICS annonce les changelists, mais un app qui passe de "jeton
        requis" a "depots publics" ne declenche pas forcement un changelist
        visible pour nous. Un balayage regulier garantit qu'on ne rate pas ce
        basculement le jour d'une sortie.
        """
        now = time.time()
        if now - self.last_watch < WATCH_EVERY_S:
            return
        self.last_watch = now
        watched = [a for a in WATCH_CLOSELY if a in db.tracked_ids(self.conn)]
        if watched:
            self.process(watched, "(surveillance rapprochee)")

    def index_packages(self, packageids):
        """Retient les packages qui contiennent un jeu suivi.

        PICS ne relie pas un jeu a ses packages : c'est le package qui liste ses
        apps. On ne peut donc pas demander "les packages de ce jeu" -- il faut
        les avoir vus. On profite du flux, qui annonce les packages modifies, et
        on garde ceux qui nous concernent. L'index se remplit avec le temps ; il
        ne sera jamais complet des le premier jour, contrairement a celui de
        SteamDB qui a parcouru Steam entier.
        """
        if not packageids:
            return 0
        tracked = db.tracked_ids(self.conn)
        kept = 0
        for i in range(0, len(packageids), BATCH):
            chunk = packageids[i:i + BATCH]
            try:
                info = self.client.get_product_info(packages=chunk, timeout=60)
            except Exception as exc:                      # noqa: BLE001
                log.debug("packages %s : %s", chunk, exc)
                continue
            for pid, data in (info.get("packages") or {}).items():
                appids = [int(a) for a in (data.get("appids") or {}).values()]
                if not (set(appids) & tracked):
                    continue
                db.put_package(self.conn, int(pid), data, appids)
                kept += 1
        if kept:
            log.info("  %d package(s) retenu(s)", kept)
        return kept

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
            self.poll_news()
            self.watch_closely()

            try:
                resp = self.client.get_changes_since(last, True, True)
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
                # Les packages changent en meme temps que les apps : c'est la
                # seule occasion de les voir passer.
                self.index_packages([p.packageid for p in resp.package_changes])

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

    conn = db.init(args.db)
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
