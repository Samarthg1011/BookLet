/* BooKlet — Gallery frontend
   Ratings + reading list live in localStorage; the engine re-ranks on change. */

(function () {
  "use strict";

  const LS_R = "bk_ratings", LS_L = "bk_list";
  let ratings = load(LS_R, {});
  let list = load(LS_L, []);
  let INIT = null;
  let arch = { shelf: null, era: null };

  const $ = (id) => document.getElementById(id);
  function load(k, fb) {
    try { return JSON.parse(localStorage.getItem(k)) ?? fb; } catch { return fb; }
  }
  function save() {
    localStorage.setItem(LS_R, JSON.stringify(ratings));
    localStorage.setItem(LS_L, JSON.stringify(list));
    $("rateCount").textContent = Object.keys(ratings).length;
    $("listCount").textContent = list.length;
    $("statRatings").textContent = Object.keys(ratings).length;
  }

  // ---------- swatch art ----------
  const SHELF_HUE = {
    "Fantasy": 262, "Romance": 340, "Mystery & Crime": 250, "Thriller": 230,
    "Science Fiction": 195, "Classics": 35, "Non-fiction": 175, "History": 25,
    "Young Adult": 320, "Horror": 356, "Biography & Memoir": 200, "Philosophy": 45,
    "Poetry": 300, "Comics & Graphic": 15, "Children": 95, "Humor": 55, Other: 210,
  };
  function swatchCSS(b) {
    const shelf = (b.shelves && b.shelves[0]) || "Other";
    const hue = SHELF_HUE[shelf] ?? 210;
    const h2 = (hue + 20 + (b.id % 30)) % 360;
    // muted, paper-like tints so cover-less cards sit quietly beside real covers
    const s = 20 + (b.id % 12), l1 = 76 + (b.id % 7), l2 = 52 + (b.id % 8);
    return `background:linear-gradient(${155 + (b.id % 30)}deg,` +
           `hsl(${hue} ${s}% ${l1}%),hsl(${h2} ${s + 8}% ${l2}%) 80%)`;
  }
  const swatchLabel = (b) =>
    `${(b.author || "").split(" ").slice(-1)[0]} · ${b.year ?? "—"}`;

  // real covers: Goodreads CDN first, Open Library by ISBN as fallback,
  // and the shelf-tinted gradient stays underneath if both fail
  function coverURLs(b) {
    const urls = [];
    if (b.img) {
      // goodreads sizes its files ...<id>m/<n>.jpg — 'l' is the 300px edition
      const large = b.img.replace(/(\/books\/\d+)m\//, "$1l/");
      if (large !== b.img) urls.push(large);
      urls.push(b.img);
    }
    if (b.isbn) urls.push(`https://covers.openlibrary.org/b/isbn/${b.isbn}-L.jpg?default=false`);
    return urls;
  }
  function attachCover(host, b) {
    const urls = coverURLs(b);
    if (!urls.length || host.querySelector("img.cover")) return;
    const img = document.createElement("img");
    img.className = "cover";
    img.alt = "";
    img.decoding = "async";
    img.referrerPolicy = "no-referrer";
    let i = 0, retried = false;
    const nextOrDrop = () => {
      // a burst of 30+ cover requests can get throttled — give the current
      // source one more go before giving up on it
      if (!retried) {
        retried = true;
        setTimeout(() => { img.src = urls[i] + (urls[i].includes("?") ? "&" : "?") + "r=1"; }, 600);
        return;
      }
      retried = false;
      i += 1;
      if (i < urls.length) { img.src = urls[i]; return; }
      img.remove();
      host.classList.remove("has-img");
    };
    // must live in the DOM before src is set — a detached lazy image never loads
    img.onload = () => {
      if (img.naturalWidth < 2) { nextOrDrop(); return; }
      host.classList.add("has-img");
    };
    img.onerror = nextOrDrop;
    host.prepend(img);
    img.src = urls[0];
  }

  // ---------- api ----------
  async function api(path, body) {
    const opt = body
      ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
      : undefined;
    const res = await fetch(path, opt);
    if (!res.ok) throw new Error(`${path} -> ${res.status}`);
    return res.json();
  }
  const recommend = (extra = {}) => api("/api/recommend", { ratings, n: 24, ...extra });

  let toastT = null;
  function toast(msg) {
    const t = $("toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toastT); toastT = setTimeout(() => (t.hidden = true), 2200);
  }

  // ---------- widgets ----------
  function starRow(id, size) {
    const wrap = document.createElement("div");
    wrap.className = "stars" + (size === "lg" ? " lg" : "");
    const current = ratings[id] || 0;
    for (let s = 1; s <= 5; s++) {
      const b = document.createElement("button");
      b.className = "st" + (s <= current ? " on" : "");
      b.textContent = "★";
      b.setAttribute("aria-label", `${s} star${s > 1 ? "s" : ""}`);
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        rate(id, s);
        wrap.querySelectorAll(".st").forEach((el, i) => el.classList.toggle("on", i < s));
      });
      wrap.appendChild(b);
    }
    return wrap;
  }

  function listBtn(b) {
    const inList = list.some((w) => w.id === b.id);
    const btn = document.createElement("button");
    btn.className = "icon-btn" + (inList ? " on" : "");
    btn.textContent = inList ? "✓" : "＋";
    btn.setAttribute("aria-label", inList ? "Remove from reading list" : "Add to reading list");
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleList(b);
    });
    return btn;
  }

  function bookCard(b, i, { numbered = true, why = true } = {}) {
    const el = document.createElement("article");
    el.className = "film";

    if (numbered) {
      const no = document.createElement("span");
      no.className = "no";
      no.textContent = `SELECTION ${String(i + 1).padStart(2, "0")}`;
      el.appendChild(no);
    }
    const sw = document.createElement("button");
    sw.className = "swatch";
    sw.style.cssText = swatchCSS(b);
    sw.innerHTML = "<span></span>";
    sw.querySelector("span").textContent = swatchLabel(b);
    sw.addEventListener("click", () => openBook(b));
    attachCover(sw, b);
    el.appendChild(sw);

    const h = document.createElement("h3");
    h.textContent = b.title;
    h.addEventListener("click", () => openBook(b));
    el.appendChild(h);

    const meta = document.createElement("p");
    meta.className = "meta";
    meta.textContent = `${b.author}${b.shelves?.length ? " · " + b.shelves[0] : ""}`;
    el.appendChild(meta);

    if (why && b.reason) {
      const w = document.createElement("p");
      w.className = "why";
      w.textContent = b.reason.replace(/★/g, " of 5 stars");
      el.appendChild(w);
    }

    const row = document.createElement("div");
    row.className = "score-row";
    const score = document.createElement("span");
    score.className = "score";
    if (b.match) {
      score.innerHTML = `<span class="ring" style="--v:${b.match}"><b>${b.match}</b></span>MATCH`;
    } else {
      score.innerHTML = `<span>★ ${b.avg} · ${abbr(b.n)} readers</span>`;
    }
    const actions = document.createElement("div");
    actions.className = "row-actions";
    actions.appendChild(starRow(b.id));
    actions.appendChild(listBtn(b));
    row.append(score, actions);
    el.appendChild(row);
    return el;
  }
  const abbr = (n) =>
    n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "K" : n;

  // ---------- actions ----------
  function toggleList(b) {
    const i = list.findIndex((w) => w.id === b.id);
    if (i >= 0) { list.splice(i, 1); toast(`Removed “${b.title}”`); }
    else {
      list.push({ id: b.id, title: b.title, author: b.author, year: b.year, shelves: b.shelves, avg: b.avg, n: b.n, img: b.img, isbn: b.isbn });
      toast(`“${b.title}” set aside`);
    }
    save();
    renderList();
    renderSelections();
  }
  function rate(id, stars) {
    ratings[id] = stars;
    save();
    toast(`Noted — ${stars} of 5`);
    scheduleRefresh();
  }
  function unrate(id) {
    delete ratings[id];
    save();
    scheduleRefresh();
    renderProfile();
  }
  let refreshT = null;
  function scheduleRefresh() {
    clearTimeout(refreshT);
    refreshT = setTimeout(() => { renderSelections(); renderArchive(); renderProfile(); }, 350);
  }

  // ---------- SELECTIONS ----------
  async function renderSelections() {
    const { results, cold } = await recommend({ n: 12 });
    $("heroKick").textContent = cold
      ? "TONIGHT'S PROGRAMME — THE HOUSE SELECTION"
      : `TONIGHT'S PROGRAMME — TUNED BY ${Object.keys(ratings).length} OF YOUR RATINGS`;
    const grid = $("selectionGrid");
    grid.innerHTML = "";
    results.forEach((b, i) => grid.appendChild(bookCard(b, i)));
  }

  // ---------- ARCHIVE ----------
  async function renderArchive() {
    const { results } = await recommend({ n: 36, shelf: arch.shelf, era: arch.era });
    const grid = $("archiveGrid");
    grid.innerHTML = "";
    results.forEach((b, i) => grid.appendChild(bookCard(b, i, { numbered: false, why: false })));
  }
  const ERA_LABEL = { classics: "Classics · pre-1970", "20th": "20th Century", contemporary: "Contemporary · 2000+" };
  function chipRow(elId, values, key, labeler) {
    const row = $(elId);
    row.innerHTML = "";
    const all = document.createElement("button");
    all.className = "chip" + (arch[key] === null ? " on" : "");
    all.textContent = key === "shelf" ? "All shelves" : "Every era";
    all.onclick = () => { arch[key] = null; renderChips(); renderArchive(); };
    row.appendChild(all);
    values.forEach((v) => {
      const c = document.createElement("button");
      c.className = "chip" + (arch[key] === v ? " on" : "");
      c.textContent = labeler ? labeler(v) : v;
      c.onclick = () => { arch[key] = arch[key] === v ? null : v; renderChips(); renderArchive(); };
      row.appendChild(c);
    });
  }
  function renderChips() {
    chipRow("shelfChips", INIT.shelves, "shelf");
    chipRow("eraChips", INIT.eras, "era", (e) => ERA_LABEL[e] || e);
  }

  // ---------- READING LIST ----------
  function renderList() {
    const grid = $("listGrid");
    grid.innerHTML = "";
    list.forEach((b, i) => grid.appendChild(bookCard(b, i, { numbered: false, why: false })));
    $("listEmpty").hidden = list.length > 0;
  }

  // ---------- TASTE PROFILE ----------
  function renderProfile() {
    const ids = Object.keys(ratings);
    $("ratingsEmpty").hidden = ids.length > 0;
    $("ratingsTotal").textContent = ids.length ? `— ${ids.length} verdicts` : "";

    const lookup = new Map();
    (INIT?.onboarding || []).concat(INIT?.popular || []).forEach((b) => lookup.set(b.id, b));
    list.forEach((b) => lookup.set(b.id, b));

    // shelf affinity from rated books (4★+ count double)
    const aff = {};
    ids.forEach((id) => {
      const b = lookup.get(+id);
      if (!b || !b.shelves) return;
      const w = ratings[id] >= 4 ? 2 : 1;
      b.shelves.forEach((s) => (aff[s] = (aff[s] || 0) + w));
    });
    const affHost = $("affinity");
    affHost.innerHTML = "";
    const entries = Object.entries(aff).sort((a, b) => b[1] - a[1]).slice(0, 6);
    const max = entries[0]?.[1] || 1;
    entries.forEach(([shelf, v]) => {
      const d = document.createElement("div");
      d.className = "aff";
      d.innerHTML = `<span></span><div class="track"><div class="fill"></div></div>`;
      d.querySelector("span").textContent = shelf;
      d.querySelector(".fill").style.width = Math.round((v / max) * 100) + "%";
      affHost.appendChild(d);
    });

    const listHost = $("ratingsList");
    listHost.innerHTML = "";
    ids.forEach((id) => {
      const b = lookup.get(+id) || { id: +id, title: `Book #${id}`, author: "", shelves: [] };
      const row = document.createElement("div");
      row.className = "rate-row";
      const chip = document.createElement("div");
      chip.className = "rate-chip";
      chip.style.cssText = swatchCSS(b);
      attachCover(chip, b);
      const t = document.createElement("div");
      t.className = "t";
      t.innerHTML = "<b></b><small></small>";
      t.querySelector("b").textContent = b.title;
      t.querySelector("small").textContent = `${b.author}${b.year ? " · " + b.year : ""}`;
      const rm = document.createElement("button");
      rm.className = "rm";
      rm.textContent = "withdraw";
      rm.onclick = () => unrate(id);
      row.append(chip, t, starRow(+id), rm);
      listHost.appendChild(row);
    });
  }
  $("clearRatings").addEventListener("click", () => {
    if (!Object.keys(ratings).length) return;
    ratings = {};
    save();
    toast("All verdicts withdrawn");
    scheduleRefresh();
  });

  // ---------- BOOK MODAL ----------
  async function openBook(b) {
    const bs = $("bmSwatch");
    bs.style.cssText = swatchCSS(b);
    const oldImg = bs.querySelector("img.cover");
    if (oldImg) oldImg.remove();
    attachCover(bs, b);
    $("bmSwatchLabel").textContent = swatchLabel(b);
    $("bmTitle").textContent = b.title;
    $("bmMeta").textContent =
      `${b.author} · ${b.year ?? "—"} · ★${b.avg} from ${abbr(b.n ?? 0)} readers` +
      (b.shelves?.length ? ` · ${b.shelves.join(", ")}` : "");
    $("bmReason").textContent =
      b.reason?.replace(/★/g, " of 5 stars") ||
      (b.tags || []).join(" · ") || "In the BooKlet archive";
    const starsHost = $("bmStars");
    starsHost.innerHTML = "";
    starsHost.appendChild(starRow(b.id, "lg"));
    const l = $("bmList");
    const inList = list.some((x) => x.id === b.id);
    l.textContent = inList ? "✓ On reading list" : "＋ Reading list";
    l.onclick = () => { toggleList(b); openBook(b); };
    $("bookModal").hidden = false;

    const sim = await api(`/api/similar/${b.id}`);
    const host = $("bmSimilar");
    host.innerHTML = "";
    sim.results.slice(0, 10).forEach((s) =>
      host.appendChild(bookCard(s, 0, { numbered: false, why: false })));
  }

  // ---------- ONBOARDING ----------
  function openOnboarding() {
    const grid = $("onboardGrid");
    grid.innerHTML = "";
    INIT.onboarding.forEach((b) => {
      const el = document.createElement("div");
      el.className = "ob";
      const sw = document.createElement("div");
      sw.className = "swatch";
      sw.style.cssText = swatchCSS(b);
      sw.innerHTML = "<span></span>";
      sw.querySelector("span").textContent = swatchLabel(b);
      attachCover(sw, b);
      const t = document.createElement("b");
      t.textContent = b.title;
      const a = document.createElement("small");
      a.textContent = b.author;
      el.append(sw, t, a, starRow(b.id));
      grid.appendChild(el);
    });
    updateOnboardCount();
    $("onboardModal").hidden = false;
  }
  function updateOnboardCount() {
    const n = Object.keys(ratings).length;
    $("onboardCount").textContent = `${n} RATED`;
    $("onboardDone").disabled = n < 3;
  }
  $("onboardDone").addEventListener("click", () => {
    $("onboardModal").hidden = true;
    switchView("selections");
    renderSelections();
  });
  $("coldStart").addEventListener("click", openOnboarding);
  new MutationObserver(updateOnboardCount).observe($("onboardGrid"), { subtree: true, attributes: true });

  // ---------- SEARCH ----------
  const sInput = $("searchInput"), sResults = $("searchResults");
  let searchT = null;
  sInput.addEventListener("input", () => {
    clearTimeout(searchT);
    const q = sInput.value.trim();
    if (q.length < 2) { sResults.hidden = true; return; }
    searchT = setTimeout(async () => {
      const { results } = await api(`/api/search?q=${encodeURIComponent(q)}`);
      sResults.innerHTML = "";
      results.slice(0, 9).forEach((b) => {
        const btn = document.createElement("button");
        btn.className = "sr-item";
        const chip = document.createElement("span");
        chip.className = "sr-chip";
        chip.style.cssText = swatchCSS(b);
        attachCover(chip, b);
        const t = document.createElement("span");
        t.innerHTML = "<b></b><small></small>";
        t.querySelector("b").textContent = b.title;
        t.querySelector("small").textContent = `${b.author} · ${b.year ?? "—"} · ★${b.avg}`;
        btn.append(chip, t);
        btn.onclick = () => { sResults.hidden = true; sInput.value = ""; openBook(b); };
        sResults.appendChild(btn);
      });
      sResults.hidden = results.length === 0;
    }, 220);
  });
  document.addEventListener("click", (e) => {
    if (!$("searchBox").contains(e.target)) sResults.hidden = true;
  });

  // ---------- NAV / MODALS ----------
  function switchView(name) {
    document.querySelectorAll(".view").forEach((v) => (v.hidden = v.id !== `view-${name}`));
    document.querySelectorAll(".navlink").forEach((b) =>
      b.classList.toggle("active", b.dataset.nav === name));
    if (name === "archive") renderArchive();
    if (name === "list") renderList();
    if (name === "profile") renderProfile();
    window.scrollTo({ top: 0 });
  }
  document.querySelectorAll("[data-nav]").forEach((b) =>
    b.addEventListener("click", (e) => { e.preventDefault(); switchView(b.dataset.nav); }));
  document.querySelectorAll(".modal").forEach((m) => {
    m.addEventListener("click", (e) => {
      if (e.target === m || e.target.closest("[data-close]")) m.hidden = true;
    });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") document.querySelectorAll(".modal").forEach((m) => (m.hidden = true));
  });

  // ---------- boot ----------
  (async function boot() {
    const params = new URLSearchParams(location.search);
    // ?demo=1 seeds a sample taste profile (handy for demos & screenshots)
    if (params.has("demo") && !Object.keys(ratings).length) {
      // spread across dystopia, sci-fi, thriller, non-fiction and a classic
      ratings = { 13: 5, 126: 5, 141: 4, 2205: 5, 10: 3 };
      save();
    }
    INIT = await api("/api/init");
    $("statBooks").textContent = INIT.meta.n_books.toLocaleString();
    $("statCats").textContent = INIT.shelves.length;
    $("statSignals").textContent = (INIT.meta.n_ratings / 1e6).toFixed(1) + "M";
    $("footMeta").textContent =
      `ENGINE ITEM-ITEM CF + TF-IDF CONTENT · ${INIT.meta.dataset.toUpperCase()} · ` +
      `${INIT.meta.n_users.toLocaleString()} READERS SAMPLED`;
    const sub = $("archiveSub");
    if (sub) {
      sub.textContent =
        `${INIT.meta.n_books.toLocaleString()} volumes across ${INIT.shelves.length} ` +
        `categories, ranked by your taste. Filter by category or era.`;
    }
    save();
    renderChips();
    await renderSelections();
    // ?view=archive|profile|list deep-links straight to a section,
    // optionally filtered: &category=Technology%20%26%20Coding
    const view = params.get("view");
    const cat = params.get("category");
    if (cat && INIT.shelves.includes(cat)) { arch.shelf = cat; renderChips(); }
    if (view && document.getElementById(`view-${view}`)) {
      switchView(view);
      return;
    }
    if (!Object.keys(ratings).length) openOnboarding();
  })().catch((err) => {
    console.error(err);
    toast("The curator is unreachable — is the server running?");
  });
})();
