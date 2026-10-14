// Sorter review page. Plain JavaScript, no external files: works offline.
"use strict";

const $ = (id) => document.getElementById(id);
const state = { day: null, scans: [], filter: "check", editing: null };

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false) {
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
  }
  return node;
}

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = response.statusText;
    try { message = (await response.json()).error || message; } catch (e) { /* not JSON */ }
    throw new Error(message);
  }
  return response.json();
}

function toast(text) {
  const box = $("toast");
  box.textContent = text;
  box.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => box.classList.remove("show"), 1800);
}

function money(value, currency) {
  const n = parseFloat(value);
  if (isNaN(n) || n === 0) return "";       // 0: Scryfall has no price
  return (currency === "EUR" ? "€" : "$") + n.toFixed(2);
}

// --- loading -----------------------------------------------------------------

async function loadDays() {
  const days = await api("/api/days");
  const select = $("day");
  select.replaceChildren(...days.map((d) =>
    el("option", { value: d.day }, d.day + (d.needs_check ? "  (" + d.needs_check + " to check)" : ""))));

  if (!days.length) {
    showEmpty("No scans yet. Shadow is waiting.");
    return;
  }
  const remembered = sessionStorageGet("day");
  state.day = days.some((d) => d.day === remembered) ? remembered : days[0].day;
  select.value = state.day;
  await loadScans();
}

async function loadScans() {
  const data = await api("/api/scans?day=" + encodeURIComponent(state.day));
  state.scans = data.scans;
  renderStats(data.stats);
  const option = $("day").selectedOptions[0];
  if (option) option.textContent = state.day + (data.stats.needs_check ? "  (" + data.stats.needs_check + " to check)" : "");
  render();
  $("export").href = "/api/export?day=" + encodeURIComponent(state.day);
}

function sessionStorageGet(key) {
  try { return sessionStorage.getItem(key); } catch (e) { return null; }
}
function sessionStorageSet(key, value) {
  try { sessionStorage.setItem(key, value); } catch (e) { /* private mode */ }
}

// --- rendering ---------------------------------------------------------------

function renderStats(s) {
  const stat = (value, label, alert) => el("div", { class: "stat" + (alert ? " alert" : "") }, el("b", {}, value), el("span", {}, label));
  $("stats").replaceChildren(
    stat(s.scans, "scans"),
    stat(s.scans ? Math.round((100 * s.found) / s.scans) + "%" : "–", "recognised"),
    stat(s.needs_check, "to check", s.needs_check > 0),
    stat(s.confirmed, "confirmed"),
    stat(s.corrected, "corrected"),
    stat(money(s.value, s.currency) || "–", "value")
  );
}

function visible() {
  return state.scans.filter((s) => {
    if (state.filter === "check") return s.needs_check && !s.label;
    if (state.filter === "reviewed") return !!s.label;
    return true;
  });
}

function render() {
  const all = state.scans;
  $("count-check").textContent = all.filter((s) => s.needs_check && !s.label).length;
  $("count-all").textContent = all.length;
  $("count-reviewed").textContent = all.filter((s) => s.label).length;

  const scans = visible();
  $("grid").replaceChildren(...scans.map(card));

  if (scans.length) {
    $("empty").hidden = true;
  } else if (state.filter === "check" && all.length) {
    showEmpty("Nothing left to check. Shadow approves.");
  } else {
    showEmpty("Nothing here.");
  }
}

function showEmpty(text) {
  $("empty").textContent = text;
  $("empty").hidden = false;
}

function card(scan) {
  const label = scan.label;
  const shown = label && label.status === "corrected" ? label : scan;
  const title = label && label.status === "not_a_card" ? "Not a card"
    : shown.title || "Not recognised";

  const badges = [];
  if (label) {
    badges.push(el("span", { class: "badge " + (label.status === "confirmed" ? "ok" : "fix") },
      label.status === "confirmed" ? "confirmed" : label.status === "corrected" ? "corrected" : "not a card"));
  } else {
    for (const reason of scan.reasons) badges.push(el("span", { class: "badge" }, reason));
  }
  if (scan.resorted) badges.push(el("span", { class: "badge ok", title: "Pulled back out of the manual-check bin and sorted with your verdict" }, "re-sorted"));
  if (scan.border && scan.border !== "black") badges.push(el("span", { class: "badge ok" }, scan.border + " border"));

  const image = scan.has_image
    ? el("img", { src: imageUrl(scan, true), alt: title, loading: "lazy", onclick: () => window.open(imageUrl(scan, false), "_blank") })
    : el("img", { alt: "No photo" });

  const actions = label
    ? [el("button", { class: "undo", onclick: () => setLabel(scan, "clear") }, "Undo")]
    : [
        scan.status === "found" ? el("button", { class: "yes", onclick: () => setLabel(scan, "confirmed", scan.scryfall_id) }, "✓ Correct") : null,
        el("button", { class: "edit", onclick: () => openFix(scan) }, "✎ Fix"),
      ];

  return el("article", { class: "scan" + (scan.needs_check && !label ? " check" : "") + (label ? " done" : "") },
    image,
    el("div", { class: "body" },
      el("h3", {}, title),
      shown.set ? el("div", { class: "meta" },
        shown.set + " · #" + shown.num + " ",
        money(scan.price) && !(label && label.status !== "confirmed") ? el("span", { class: "price" }, money(scan.price)) : null,
        el("span", {}, " · " + scan.time)) : el("div", { class: "meta" }, scan.time),
      el("div", { class: "badges" }, badges)),
    el("div", { class: "actions" }, actions));
}

function imageUrl(scan, thumb) {
  return "/api/image/" + state.day + "/" + scan.uuid + (thumb ? "?size=thumb" : "");
}

// --- labelling ---------------------------------------------------------------

async function setLabel(scan, status, cardId) {
  try {
    await api("/api/label", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ day: state.day, uuid: scan.uuid, status: status, card_id: cardId || null }),
    });
    toast(status === "clear" ? "Undone" : status === "confirmed" ? "Confirmed" : "Saved");
    await loadScans();
  } catch (error) {
    toast("Could not save: " + error.message);
  }
}

// --- fix dialog -------------------------------------------------------------

async function openFix(scan) {
  state.editing = scan;
  $("fix-photo").src = scan.has_image ? imageUrl(scan, false) : "";
  $("fix-why").textContent = scan.reasons.join(" · ") + (scan.drop_seen ? " — second photo: " + scan.drop_seen : "");
  $("search").value = "";
  $("results").replaceChildren();
  $("printings").replaceChildren();
  $("fix-title").textContent = scan.title ? "Which printing of " + scan.title + "?" : "Search for the card";
  $("fix").showModal();

  if (scan.scryfall_id) await showPrintings(scan.scryfall_id, scan.scryfall_id);
}

async function showPrintings(cardId, currentId) {
  const printings = await api("/api/printings?card_id=" + encodeURIComponent(cardId));
  if (printings.length) $("fix-title").textContent = "Which printing of " + printings[0].name + "?";
  // What the sorter recognised goes first
  printings.sort((a, b) => (b.id === currentId) - (a.id === currentId));

  $("printings").replaceChildren(...printings.map((p) => el("li", {
      class: p.id === currentId ? "current" : "",
      title: p.id === currentId ? "What the sorter recognised" : "",
      onclick: () => choose(p.id),
    },
    p.has_closeup ? el("img", { src: "/api/closeup/" + p.id, alt: "Set symbol of " + p.set_code, loading: "lazy" })
                  : el("span", { class: "noimg" }),
    el("span", { class: "what" },
      el("b", {}, p.set_code + " #" + p.collector_number + "  " + (p.set_name || ""),
        p.id === currentId ? el("span", { class: "tag" }, "recognised") : null),
      el("small", {}, [p.rarity, p.frame && p.frame + " frame", p.border_color && p.border_color + " border",
                       (p.released_at || "").slice(0, 4), p.lang !== "en" ? p.lang : null,
                       p.usd ? "$" + Number(p.usd).toFixed(2) : null].filter(Boolean).join(" · "))))));
}

async function choose(cardId) {
  const scan = state.editing;
  $("fix").close();
  await setLabel(scan, cardId === scan.scryfall_id ? "confirmed" : "corrected", cardId);
}

let searchTimer;
$("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(async () => {
    const query = $("search").value.trim();
    if (query.length < 2) { $("results").replaceChildren(); return; }
    const results = await api("/api/search?q=" + encodeURIComponent(query));
    $("results").replaceChildren(...results.map((r) =>
      el("li", { onclick: () => { $("results").replaceChildren(); showPrintings(r.id, state.editing.scryfall_id); } },
        r.name, el("small", {}, r.printings + " printing" + (r.printings === 1 ? "" : "s")))));
  }, 200);
});

$("not-card").addEventListener("click", async () => {
  const scan = state.editing;
  $("fix").close();
  await setLabel(scan, "not_a_card");
});

// --- the sorter's state ---------------------------------------------------------

let lastStatus = null;
let lastReload = 0;

async function pollStatus() {
  try {
    const status = await api("/api/status");
    const banner = $("banner");
    // Reload the scans when the sorter pauses, starts or finishes re-sorting,
    // and every 15 s while it re-sorts: not on every card
    const key = (status.waiting_review ? "waiting" : "") + (status.resorting ? "resorting" : "");

    if (status.waiting_review) {
      const w = status.waiting_review;
      banner.className = "banner";
      banner.replaceChildren(
        el("b", {}, "Sorter paused since " + w.since + ": "),
        w.cards + " card" + (w.cards === 1 ? "" : "s") + " in the manual-check bin. Review them below, then press ",
        el("b", {}, "Resume"), " on the touchscreen to sort them (or Stop to finish).");
      banner.hidden = false;
    } else if (status.resorting) {
      banner.className = "banner busy";
      banner.replaceChildren(el("b", {}, "Re-sorting the manual-check bin: "),
        status.resorting.left + " card" + (status.resorting.left === 1 ? "" : "s") + " left.");
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }

    const changed = lastStatus !== null && key !== lastStatus;
    const due = status.resorting && Date.now() - lastReload > 15000;
    if ((changed || due) && state.day) {
      lastReload = Date.now();
      await loadScans();
    }
    lastStatus = key;
  } catch (error) {
    /* the page keeps working without the banner */
  }
}

// --- wiring -------------------------------------------------------------------

$("day").addEventListener("change", async (event) => {
  state.day = event.target.value;
  sessionStorageSet("day", state.day);
  await loadScans();
});

for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    state.filter = tab.dataset.filter;
    render();
  });
}

loadDays().catch((error) => showEmpty("Could not load scans: " + error.message));
pollStatus();
setInterval(pollStatus, 5000);
