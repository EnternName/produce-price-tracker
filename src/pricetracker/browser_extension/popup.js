// Popup: find the flyer on the current tab and send it to the local Price tracker app.
const ext = globalThis.browser ?? globalThis.chrome;
const DEFAULTS = { appUrl: "http://127.0.0.1:8000", token: "" };
const MAX_CAPTURE_HEIGHT = 12000; // px per full-page screenshot slice
const $ = (id) => document.getElementById(id);

let tab = null;
let scan = null;

class AppError extends Error {
  constructor(kind, message) {
    super(message);
    this.kind = kind;
  }
}

// ---- settings & app calls ------------------------------------------------------------------------

async function getSettings() {
  const stored = await ext.storage.local.get(["appUrl", "token", "draft"]);
  return { ...DEFAULTS, ...stored };
}

async function call(path, { method = "GET", json, form } = {}) {
  const { appUrl, token } = await getSettings();
  const headers = { "X-Pricetracker-Token": token || "" };
  let body;
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  } else if (form) {
    body = form;
  }
  let response;
  try {
    response = await fetch(appUrl.replace(/\/+$/, "") + path, { method, headers, body });
  } catch (e) {
    throw new AppError("unreachable", `Can't reach the app at ${appUrl}. Is “pricetracker serve” running?`);
  }
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) throw new AppError("token", "The app needs the token: copy it from the app's “Browser add-on” page into Settings below.");
  if (!response.ok) {
    const detail = typeof data.detail === "string" ? data.detail : data.detail ? JSON.stringify(data.detail) : `HTTP ${response.status}`;
    throw new AppError("http", detail);
  }
  return data;
}

// ---- UI helpers ----------------------------------------------------------------------------------

function setStatus(text, kind) {
  const el = $("status");
  el.textContent = text;
  el.className = "status" + (kind ? " " + kind : "");
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of children) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return node;
}

function showResult(message, { error = false, link } = {}) {
  const box = $("result");
  box.hidden = false;
  box.className = "result" + (error ? " error" : "");
  box.replaceChildren(el("div", {}, message));
  if (link) {
    box.append(el("div", { class: "row" }, el("button", { class: "secondary", onclick: () => ext.tabs.create({ url: link }) }, "Open in Price tracker")));
  }
}

async function busy(button, work) {
  const buttons = [...document.querySelectorAll("button")];
  buttons.forEach((b) => (b.disabled = true));
  const label = button ? button.textContent : "";
  if (button) button.textContent = "Working…";
  try {
    await work();
  } catch (e) {
    showResult(e.message || String(e), { error: true });
    if (e.kind === "token" || e.kind === "unreachable") $("settings").open = true;
  } finally {
    buttons.forEach((b) => (b.disabled = false));
    if (button) button.textContent = label;
  }
}

// ---- drafts (a flyer being assembled from several sends) -----------------------------------------

async function currentDraft() {
  const { draft } = await getSettings();
  return draft || null;
}

async function saveDraft(draft) {
  if (draft) await ext.storage.local.set({ draft });
  else await ext.storage.local.remove("draft");
  renderDraft(draft);
}

function renderDraft(draft) {
  $("draft").hidden = !draft;
  if (!draft) return;
  $("draft-count").textContent = draft.files;
  $("draft-store").textContent = draft.store;
}

async function createFlyer() {
  const store = $("store").value.trim();
  if (!store) {
    $("store").focus();
    throw new AppError("input", "Choose the store first.");
  }
  const created = await call("/api/flyers", {
    method: "POST",
    json: { store, title: $("title").value.trim() || null, page_url: tab.url },
  });
  return { id: created.id, store: created.store, files: created.files, url: created.url };
}

async function ensureDraft() {
  const existing = await currentDraft();
  if (existing) return existing;
  const draft = await createFlyer();
  await saveDraft(draft);
  return draft;
}

async function sendCompleteFile(url) {
  // A PDF (or a single file) is a whole flyer: send it on its own, leaving any draft untouched.
  const flyer = await createFlyer();
  const result = await call(`/api/flyers/${flyer.id}/urls`, {
    method: "POST",
    json: { urls: [url], page_url: tab.url, user_agent: scan ? scan.userAgent : navigator.userAgent },
  });
  if (!result.added) {
    await call(`/api/flyers/${flyer.id}`, { method: "DELETE" }).catch(() => {});
    const why = result.skipped.length ? result.skipped[0].reason : "nothing usable";
    throw new AppError("http", `The app could not use that file (${why}). Try a screenshot instead.`);
  }
  const done = await call(`/api/flyers/${flyer.id}/finish`, { method: "POST" });
  showResult(`Sent. Claude is reading the flyer now; prices appear in the app in a minute or two.`, { link: done.url });
}

async function refreshDraft() {
  const draft = await currentDraft();
  if (!draft) return renderDraft(null);
  try {
    const status = await call(`/api/flyers/${draft.id}`);
    if (!status.collecting) return saveDraft(null); // finished or read from the app meanwhile
    await saveDraft({ ...draft, files: status.files });
  } catch (e) {
    if (e.kind === "http") await saveDraft(null); // deleted in the app
    else throw e;
  }
}

async function finishDraft() {
  const draft = await currentDraft();
  if (!draft) return;
  const done = await call(`/api/flyers/${draft.id}/finish`, { method: "POST" });
  await saveDraft(null);
  showResult(`Sent ${done.files} file(s). Claude is reading the flyer now; prices appear in the app in a minute or two.`, { link: done.url });
}

// ---- actions -------------------------------------------------------------------------------------

async function addImageUrls(urls) {
  const draft = await ensureDraft();
  const result = await call(`/api/flyers/${draft.id}/urls`, {
    method: "POST",
    json: { urls, page_url: tab.url, user_agent: scan ? scan.userAgent : navigator.userAgent },
  });
  await saveDraft({ ...draft, files: result.files });
  if (!result.added) {
    const why = result.skipped.length ? result.skipped[0].reason : "nothing usable";
    throw new AppError("http", `Nothing could be added (${why}). Try a screenshot instead.`);
  }
  const skipped = result.skipped_count ? ` Skipped ${result.skipped_count} (small, duplicate or unreadable).` : "";
  showResult(`Added ${result.added} page image(s).${skipped} Add more pages, or press “Read now”.`);
}

async function sendPageText() {
  const flyer = await createFlyer();
  const result = await call(`/api/flyers/${flyer.id}/text`, { method: "POST", json: { text: scan.text, page_url: tab.url } });
  const done = await call(`/api/flyers/${flyer.id}/finish`, { method: "POST" });
  const parts = result.chunks > 1 ? ` in ${result.chunks} parts` : "";
  showResult(`Sent the page text${parts}. Claude is reading the offers now.`, { link: done.url });
}

async function dataUrlToBlob(dataUrl) {
  return (await fetch(dataUrl)).blob();
}

async function addScreenshots(dataUrls) {
  const draft = await ensureDraft();
  const form = new FormData();
  let n = draft.files;
  for (const dataUrl of dataUrls) {
    form.append("files", await dataUrlToBlob(dataUrl), `screenshot-${String(++n).padStart(3, "0")}.png`);
  }
  const status = await call(`/api/flyers/${draft.id}/pages`, { method: "POST", form });
  await saveDraft({ ...draft, files: status.files });
  showResult(`Added ${dataUrls.length} screenshot(s) (${status.files} in total). Show the next page and add it, or press “Read now”.`);
}

async function captureVisible() {
  return [await ext.tabs.captureVisibleTab(tab.windowId, { format: "png" })];
}

async function captureFullPage() {
  // Firefox only: tabs.captureTab with a rect can capture beyond the visible area.
  const { width, height } = scan.page;
  const shots = [];
  for (let y = 0; y < height; y += MAX_CAPTURE_HEIGHT) {
    const rect = { x: 0, y, width, height: Math.min(MAX_CAPTURE_HEIGHT, height - y) };
    shots.push(await ext.tabs.captureTab(tab.id, { format: "png", rect, scale: 1 }));
  }
  return shots;
}

// ---- rendering the scan --------------------------------------------------------------------------

function renderScan() {
  const pdfList = $("pdf-list");
  const frameList = $("frame-list");
  pdfList.replaceChildren();
  frameList.replaceChildren();
  const parts = [];

  if (scan.pdfs.length) parts.push(`${scan.pdfs.length} PDF${scan.pdfs.length > 1 ? "s" : ""}`);
  for (const pdf of scan.pdfs.slice(0, 8)) {
    const button = el("button", pdf.sure ? {} : { class: "secondary", title: "This looks like a download button; the app checks that it really is a PDF" }, "Send PDF");
    button.addEventListener("click", () => busy(button, () => sendCompleteFile(pdf.url)));
    pdfList.append(el("li", {}, el("span", { class: "label", title: pdf.url }, pdf.label), button));
  }

  $("images").hidden = !scan.images.length;
  if (scan.images.length) {
    const sized = scan.images.filter((i) => i.width);
    const largest = sized.sort((a, b) => b.width * b.height - a.width * a.height)[0];
    parts.push(`${scan.images.length} possible page image${scan.images.length > 1 ? "s" : ""}` + (largest ? ` (largest ${largest.width}×${largest.height})` : ""));
    const thumbs = $("thumbs");
    thumbs.replaceChildren(...scan.images.slice(0, 8).map((i) => el("img", { src: i.url, alt: "", loading: "lazy", referrerpolicy: "no-referrer" })));
    $("send-images").textContent = `Add ${scan.images.length} page image${scan.images.length > 1 ? "s" : ""}`;
  }

  for (const frame of scan.frames.slice(0, 4)) {
    const button = el("button", { class: "secondary" }, "Open viewer");
    button.addEventListener("click", () => ext.tabs.create({ url: frame.url }));
    frameList.append(el("li", {}, el("span", { class: "label", title: frame.url }, `Embedded viewer (${new URL(frame.url).hostname}) – open it and click the add-on there`), button));
  }

  $("text-section").hidden = scan.priceCount < 5;
  if (scan.priceCount >= 5) {
    parts.push(`${scan.priceCount} prices in the page text`);
    $("text-summary").textContent = `${scan.priceCount} prices in ${scan.text.length.toLocaleString()} characters of text.`;
  }

  $("scan-summary").textContent = parts.length
    ? `Found ${parts.join(" and ")}.`
    : scan.frames.length
      ? "The flyer seems to be inside an embedded viewer."
      : "No PDF or page images found. Use screenshots below.";
}

// ---- start ---------------------------------------------------------------------------------------

async function targetTab() {
  const id = new URLSearchParams(location.search).get("tab"); // lets the automated tests pick a tab
  if (id) return ext.tabs.get(Number(id));
  const [active] = await ext.tabs.query({ active: true, currentWindow: true });
  return active;
}

async function loadStores() {
  try {
    const stores = await call("/api/stores");
    $("store-list").replaceChildren(...stores.map((s) => el("option", { value: s.name })));
    setStatus("Connected", "ok");
    $("setup-hint").hidden = true;
  } catch (e) {
    setStatus(e.kind === "token" ? "Token needed" : "App not reachable", "bad");
    const hint = $("setup-hint");
    hint.hidden = false;
    hint.textContent = e.message;
    $("settings").open = true;
  }
}

async function main() {
  const settings = await getSettings();
  $("app-url").value = settings.appUrl;
  $("token").value = settings.token;
  renderDraft(settings.draft || null);

  tab = await targetTab();
  $("title").value = (tab.title || "").slice(0, 120);
  $("store").value = settings.draft ? settings.draft.store : guessStore(tab.url || "", tab.title || "");
  $("shot-full").hidden = typeof ext.tabs.captureTab !== "function";

  await loadStores();
  refreshDraft().catch(() => {});

  try {
    const [injected] = await ext.scripting.executeScript({ target: { tabId: tab.id }, func: scanFlyerPage });
    scan = injected.result;
    renderScan();
  } catch (e) {
    // Built-in PDF viewer, browser pages and other places where scripts can't run.
    scan = { pdfs: [], images: [], frames: [], text: "", priceCount: 0, page: { width: 0, height: 0 }, userAgent: navigator.userAgent };
    const isHttp = /^https?:/i.test(tab.url || "");
    $("scan-summary").textContent = isHttp
      ? "Can't look inside this tab (it may be a PDF open in Firefox's viewer). You can send its address: the app downloads it."
      : "Open a store's flyer page, then click the add-on again.";
    $("send-tab-url").hidden = !isHttp;
    $("shot-full").hidden = true;
  }
}

// ---- wiring --------------------------------------------------------------------------------------

$("save-settings").addEventListener("click", () =>
  busy($("save-settings"), async () => {
    await ext.storage.local.set({ appUrl: $("app-url").value.trim() || DEFAULTS.appUrl, token: $("token").value.trim() });
    await loadStores();
    showResult("Settings saved.");
  }),
);
$("send-images").addEventListener("click", () => busy($("send-images"), () => addImageUrls(scan.images.map((i) => i.url))));
$("send-tab-url").addEventListener("click", () => busy($("send-tab-url"), () => sendCompleteFile(tab.url)));
$("send-text").addEventListener("click", () => busy($("send-text"), sendPageText));
$("shot-visible").addEventListener("click", () => busy($("shot-visible"), async () => addScreenshots(await captureVisible())));
$("shot-full").addEventListener("click", () => busy($("shot-full"), async () => addScreenshots(await captureFullPage())));
$("draft-finish").addEventListener("click", () => busy($("draft-finish"), finishDraft));
$("draft-open").addEventListener("click", async () => {
  const draft = await currentDraft();
  if (draft) ext.tabs.create({ url: draft.url });
});
$("draft-discard").addEventListener("click", () =>
  busy($("draft-discard"), async () => {
    const draft = await currentDraft();
    if (draft) await call(`/api/flyers/${draft.id}`, { method: "DELETE" }).catch(() => {});
    await saveDraft(null);
    showResult("Discarded.");
  }),
);

main().catch((e) => showResult(e.message || String(e), { error: true }));
