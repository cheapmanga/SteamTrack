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
