// Code commun aux deux pages : appels API, rendu des differences, apercu des
// assets. Aucun framework, aucune dependance : la page est servie telle quelle.

const API = "";                 // meme origine que l'interface

const KINDS = {
    build:  "Builds",
    depot:  "Depots",
    branch: "Branches",
    store:  "Store",
    assets: "Assets",
    news:   "News",
    meta:   "Other",
};

async function api(path) {
    const resp = await fetch(API + path, { cache: "no-store" });
    if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${resp.status}`);
    }
    return resp.json();
}

function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    // textContent partout : le contenu vient de Steam, jamais d'innerHTML.
    if (text !== undefined) node.textContent = text;
    return node;
}

// ----- Dates -----
const UNITS = [
    [31536000, "year"], [2592000, "month"], [86400, "day"],
    [3600, "hour"], [60, "minute"],
];

function ago(iso) {
    if (!iso) return "-";
    const secs = (Date.now() - new Date(iso).getTime()) / 1000;
    if (secs < 60) return "just now";
    for (const [size, label] of UNITS) {
        if (secs >= size) {
            const n = Math.floor(secs / size);
            return `${n} ${label}${n > 1 ? "s" : ""} ago`;
        }
    }
    return "just now";
}

function stamp(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleString("en-GB", {
        day: "2-digit", month: "short", year: "numeric",
        hour: "2-digit", minute: "2-digit",
    });
}

// ----- Rendu d'un evenement -----
function panel(event, opts = {}) {
    const box = el("article", "panel");

    const head = el("div", "panel-head");
    head.append(el("span", "tag " + event.kind, (KINDS[event.kind] || event.kind).toUpperCase()));
    head.append(el("span", "panel-title", event.title));
    if (opts.appName) {
        const link = el("a", "changenum", opts.appName);
        link.href = `app.html?appid=${event.appid}`;
        head.append(link);
    }
    if (event.change_number) head.append(el("span", "changenum", "#" + event.change_number));

    const time = el("span", "panel-time", ago(event.occurred_at));
    time.title = stamp(event.occurred_at);
    head.append(time);
    box.append(head);

    const body = el("div", "panel-body");
    if (event.source === "news") {
        body.append(newsBody(event.changes || {}));
    } else if (event.changes && event.changes.length) {
        body.append(tree(event.changes));
    } else {
        body.append(el("span", "s-muted", "no detail"));
    }
    box.append(body);
    return box;
}

function newsBody(payload) {
    const wrap = el("div");
    const text = el("div", "news-body", payload.body || "");
    wrap.append(text);

    const actions = el("div", "news-actions");
    if ((payload.body || "").length > 320) {
        text.classList.add("clip");
        const btn = el("button", "more", "Read more");
        btn.addEventListener("click", () => {
            const clipped = text.classList.toggle("clip");
            btn.textContent = clipped ? "Read more" : "Show less";
        });
        actions.append(btn);
    }
    if (payload.url) {
        const link = el("a", "more", "Open on Steam");
        link.href = payload.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        actions.append(link);
    }
    if (actions.childElementCount) wrap.append(actions);
    return wrap;
}

function tree(nodes) {
    const list = el("ul", "diff");
    nodes.forEach(node => {
        const item = el("li", node.op || "none");
        const line = el("span", "line");
        (node.seg || []).forEach(seg => {
            const cls = {
                del: "s-del", ins: "s-ins", field: "s-field", muted: "s-muted",
            }[seg.t] || "s-text";
            line.append(seg.href ? mediaLink(seg, cls) : el("span", cls, seg.v));
        });
        item.append(line);
        if (node.children && node.children.length) item.append(tree(node.children));
        list.append(item);
    });
    return list;
}

// ----- Assets : apercu au survol, telechargement au clic -----
function mediaLink(seg, cls) {
    const link = el("a", `${cls} media ${seg.media || ""}`, seg.v);
    link.href = seg.href;
    link.dataset.media = seg.media || "image";
    link.title = "Hover to preview, click to download";
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
}

let hover = null;
let hoverTimer = null;

function showPreview(link) {
    hidePreview();
    hover = el("div", "preview");
    const cap = el("span", "cap", "Loading...");

    const ready = (w, h) => {
        if (!hover) return;
        cap.textContent = (w && h ? `${w}x${h} - ` : "") + "click to download";
        place(link);
    };
    const failed = () => {
        if (!hover) return;
        hover.classList.add("err");
        cap.textContent = "Preview unavailable";
    };

    // Les ecouteurs sont poses AVANT src : une ressource deja en cache se
    // charge de façon synchrone et l'evenement partirait avant l'ecoute.
    let node;
    if (link.dataset.media === "video") {
        node = document.createElement("video");
        node.autoplay = node.loop = node.muted = node.playsInline = true;
        node.addEventListener("loadeddata", () => ready(node.videoWidth, node.videoHeight));
        node.addEventListener("error", failed);
        node.src = link.href;
        if (node.readyState >= 2) ready(node.videoWidth, node.videoHeight);
    } else {
        node = document.createElement("img");
        node.alt = "";
        node.addEventListener("load", () => ready(node.naturalWidth, node.naturalHeight));
        node.addEventListener("error", failed);
        node.src = link.href;
        if (node.complete) {
            node.naturalWidth ? ready(node.naturalWidth, node.naturalHeight) : failed();
        }
    }

    hover.append(node, cap);
    document.body.append(hover);
    place(link);
}

function place(link) {
    if (!hover) return;
    const r = link.getBoundingClientRect();
    const box = hover.getBoundingClientRect();
    const margin = 10;
    let left = Math.max(margin, Math.min(r.left, window.innerWidth - box.width - margin));
    let top = r.top - box.height - 8;
    if (top < margin) top = r.bottom + 8;
    hover.style.left = left + "px";
    hover.style.top = top + "px";
}

function hidePreview() {
    clearTimeout(hoverTimer);
    if (hover) { hover.remove(); hover = null; }
}

// Ecouteurs delegues : le flux compte des centaines de liens.
function bindMedia(root) {
    root.addEventListener("mouseover", ev => {
        const link = ev.target.closest("a.media");
        if (!link) return;
        clearTimeout(hoverTimer);
        hoverTimer = setTimeout(() => showPreview(link), 170);
    });
    root.addEventListener("mouseout", ev => {
        const link = ev.target.closest("a.media");
        if (link && !(ev.relatedTarget && link.contains(ev.relatedTarget))) hidePreview();
    });
    window.addEventListener("scroll", hidePreview, { passive: true });

    // L'attribut download est ignore en cross-origin : le navigateur navigue
    // au lieu d'enregistrer. On passe donc par fetch + blob, ce que les CDN
    // Steam autorisent (access-control-allow-origin: *).
    root.addEventListener("click", async ev => {
        const link = ev.target.closest("a.media");
        if (!link) return;
        ev.preventDefault();
        hidePreview();
        const name = decodeURIComponent(link.href.split("/").pop().split("?")[0]) || "asset";
        link.classList.add("busy");
        try {
            const resp = await fetch(link.href, { mode: "cors" });
            if (!resp.ok) throw new Error("HTTP " + resp.status);
            const url = URL.createObjectURL(await resp.blob());
            const a = document.createElement("a");
            a.href = url;
            a.download = name;
            document.body.append(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 10000);
        } catch {
            // Asset supprime, hors ligne : ouvrir l'original reste plus utile
            // que de ne rien faire.
            window.open(link.href, "_blank", "noopener,noreferrer");
        } finally {
            link.classList.remove("busy");
        }
    });
}

// ----- Graphique de frequentation -----
// SVG construit a la main : une bibliotheque de graphes pour une seule courbe
// couterait plus de poids que toute la page.
function playerChart(series) {
    const box = el("div", "chart");
    if (!series.length) {
        box.append(el("p", "empty",
            "No player data yet. Steam does not publish past figures, "
            + "so the curve starts when tracking began."));
        return box;
    }

    const W = 900, H = 220, PAD = { t: 12, r: 12, b: 22, l: 52 };
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("preserveAspectRatio", "none");

    const add = (tag, attrs, cls) => {
        const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
        for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
        if (cls) n.setAttribute("class", cls);
        svg.append(n);
        return n;
    };

    const values = series.map(p => p.players);
    const max = Math.max(...values), min = Math.min(...values);
    // Une courbe plate au milieu se lit mieux qu'une courbe collee au bord.
    const top = max === min ? max * 1.1 + 1 : max + (max - min) * 0.15;
    const bottom = max === min ? Math.max(0, max * 0.9 - 1) : Math.max(0, min - (max - min) * 0.15);

    const x = i => PAD.l + (i / Math.max(1, series.length - 1)) * (W - PAD.l - PAD.r);
    const y = v => PAD.t + (1 - (v - bottom) / (top - bottom || 1)) * (H - PAD.t - PAD.b);

    for (let i = 0; i <= 4; i++) {
        const v = bottom + (top - bottom) * (i / 4);
        const yy = y(v);
        add("line", { x1: PAD.l, y1: yy, x2: W - PAD.r, y2: yy }, "grid");
        const t = add("text", { x: PAD.l - 8, y: yy + 3, "text-anchor": "end" }, "lbl");
        t.textContent = compact(Math.round(v));
    }

    const pts = series.map((p, i) => `${x(i)},${y(p.players)}`).join(" ");
    add("polygon", {
        points: `${PAD.l},${y(bottom)} ${pts} ${x(series.length - 1)},${y(bottom)}`,
    }, "area");
    add("polyline", { points: pts }, "curve");
    add("circle", { cx: x(series.length - 1), cy: y(values[values.length - 1]), r: 3 }, "dot");

    const first = add("text", { x: PAD.l, y: H - 6 }, "lbl");
    first.textContent = stamp(series[0].t).split(",")[0];
    const last = add("text", { x: W - PAD.r, y: H - 6, "text-anchor": "end" }, "lbl");
    last.textContent = stamp(series[series.length - 1].t).split(",")[0];

    box.append(svg);
    return box;
}

function compact(n) {
    if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "k";
    return String(n);
}

function statRow(pairs) {
    const row = el("div", "statrow");
    pairs.forEach(([label, value]) => {
        const box = el("div");
        box.append(el("div", "stat-v", value));
        box.append(el("div", "stat-l", label));
        row.append(box);
    });
    return row;
}

function bytes(n) {
    if (!n) return "-";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let i = 0, v = Number(n);
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v >= 100 || i === 0 ? 0 : 2)} ${units[i]}`;
}

// ----- Rafraichissement automatique -----
// Le collecteur detecte un changement en quelques secondes ; sans cela, une
// page laissee ouverte resterait pourtant figee jusqu'au rechargement manuel.
function autoRefresh({ every = 300000, reload, label = "" } = {}) {
    const wrap = el("div", "auto");
    const status = el("span", "", "");
    const btn = el("button", "refresh", "Refresh");
    wrap.append(status, btn);

    let nextAt = Date.now() + every;
    let busy = false;

    async function run(manual) {
        if (busy) return;
        busy = true;
        btn.disabled = true;
        const before = status.textContent;
        status.textContent = "Refreshing...";
        try {
            const fresh = await reload();
            status.classList.toggle("fresh", !!fresh);
            if (fresh) {
                status.textContent = `${fresh} new`;
                setTimeout(() => status.classList.remove("fresh"), 8000);
            }
        } catch {
            // Le flux affiche reste en place : mieux vaut des donnees un peu
            // vieilles qu'une page videe par une coupure reseau.
            status.textContent = "Refresh failed";
            status.classList.remove("fresh");
        } finally {
            busy = false;
            btn.disabled = false;
            nextAt = Date.now() + every;
        }
    }

    btn.addEventListener("click", () => run(true));
    setInterval(() => {
        if (busy) return;
        if (Date.now() >= nextAt) { run(false); return; }
        if (status.classList.contains("fresh")) return;
        const left = Math.max(0, Math.round((nextAt - Date.now()) / 1000));
        status.textContent = `${label}next refresh in ${Math.floor(left / 60)}:`
            + String(left % 60).padStart(2, "0");
    }, 1000);

    return wrap;
}
