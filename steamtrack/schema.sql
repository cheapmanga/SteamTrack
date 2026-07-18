-- Schema de steamtrack.
--
-- SQLite plutot que PostgreSQL : le service suit une liste de jeux, pas les
-- 250 000 apps de Steam. A cette echelle SQLite tient largement, sans process
-- serveur ni compilation native -- ce qui compte sur une VM a 1 Go de RAM.

PRAGMA journal_mode = WAL;      -- le collecteur ecrit pendant que l'API lit
PRAGMA foreign_keys = ON;

-- Jeux suivis. Retirer une ligne efface tout l'historique associe (ON DELETE
-- CASCADE) : c'est la suppression "avec tout son historique" voulue.
CREATE TABLE IF NOT EXISTS apps (
    appid           INTEGER PRIMARY KEY,
    name            TEXT    NOT NULL DEFAULT '',
    added_at        TEXT    NOT NULL,
    last_change     INTEGER,            -- dernier changenumber traite
    last_checked_at TEXT,
    missing_token   INTEGER NOT NULL DEFAULT 0
);

-- Dernier appinfo connu, base du prochain diff. Un seul par app : l'historique
-- vit dans changes, pas ici, pour ne pas stocker N fois le meme gros blob.
CREATE TABLE IF NOT EXISTS snapshots (
    appid         INTEGER PRIMARY KEY REFERENCES apps(appid) ON DELETE CASCADE,
    change_number INTEGER,
    data          TEXT NOT NULL,        -- appinfo JSON
    updated_at    TEXT NOT NULL
);

-- Un evenement = un changelist affectant un app suivi, ou une annonce Steam.
-- payload porte l'arbre de diff au format deja utilise par le tracker.
CREATE TABLE IF NOT EXISTS changes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    appid         INTEGER NOT NULL REFERENCES apps(appid) ON DELETE CASCADE,
    change_number INTEGER,
    kind          TEXT    NOT NULL,     -- build | depot | branch | store | assets | news | meta
    types         TEXT    NOT NULL,     -- JSON : toutes les categories applicables
    title         TEXT    NOT NULL,
    buildid       TEXT,
    occurred_at   TEXT    NOT NULL,
    payload       TEXT    NOT NULL,     -- JSON : arbre de diff ou corps d'annonce
    source        TEXT    NOT NULL,     -- pics | news | import
    -- Un meme changelist ne doit etre enregistre qu'une fois par app, meme si
    -- le collecteur redemarre ou rejoue une fenetre.
    --
    -- La date fait partie de la cle : SteamDB publie plusieurs panneaux sous un
    -- meme changeid, parfois a des dates differentes. Sans elle, l'import
    -- ecrasait des evenements distincts -- dont une build.
    UNIQUE (appid, change_number, source, occurred_at)
);

CREATE INDEX IF NOT EXISTS idx_changes_app_time ON changes (appid, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_changes_time     ON changes (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_changes_kind     ON changes (kind, occurred_at DESC);

-- Cles d'API. quota_per_hour NULL = illimite (les tiennes, et les invites).
-- is_admin autorise en plus l'ajout et la suppression de jeux : ces endpoints
-- modifient ce que le service collecte, ils ne peuvent pas etre ouverts a tous.
CREATE TABLE IF NOT EXISTS api_keys (
    key            TEXT PRIMARY KEY,
    label          TEXT NOT NULL,
    quota_per_hour INTEGER,
    is_admin       INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    revoked_at     TEXT
);

-- Consommation par cle et par heure, pour appliquer le quota sans table de logs
-- qui grossirait sans fin.
CREATE TABLE IF NOT EXISTS api_usage (
    key     TEXT NOT NULL REFERENCES api_keys(key) ON DELETE CASCADE,
    hour    TEXT NOT NULL,              -- 'YYYY-MM-DDTHH'
    hits    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key, hour)
);

-- Etat du collecteur : le changenumber global d'ou reprendre apres un arret.
CREATE TABLE IF NOT EXISTS state (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
