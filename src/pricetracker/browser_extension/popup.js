// Popup: find the flyer on the current tab and send it to the local Price tracker app.
const ext = globalThis.browser ?? globalThis.chrome;
const DEFAULTS = { appUrl: "http://127.0.0.1:8000", token: "" };
const SLICE_HEIGHT = 4000; // px per whole-page screenshot slice (each is uploaded on its own)
const JPEG_QUALITY = 90;
const MAX_URL = 4000;
const QUICK_TIMEOUT = 8000; // ms, for status checks
const SEND_TIMEOUT = 300000; // ms: the app may be downloading a 40 MB PDF
// runtime.getBrowserInfo exists only in Firefox. (tabs.captureTab is Firefox-only too, but Firefox hides
// it from add-ons without access to all sites, which this one doesn't ask for.)
const isFirefox = typeof globalThis.browser?.runtime?.getBrowserInfo === "function";
const $ = (id) => document.getElementById(id);

let tab = null;
let scan = null;

class AppError extends Error {
  constructor(kind, message, status = 0) {
    super(message);
    this.kind = kind;
    this.status = status;
  }
}

// ---- settings & app calls ------------------------------------------------------------------------

async function getSettings() {
  const stored = await ext.storage.local.get(["appUrl", "token", "draft"]);
  return { ...DEFAULTS, ...stored };
}

async function call(path, { method = "GET", json, form, timeout = 60000 } = {}) {
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
    response = await fetch(appUrl.replace(/\/+$/, "") + path, { method, headers, body, signal: AbortSignal.timeout(timeout) });
  } catch (e) {
    if (e.name === "TimeoutError") throw new AppError("timeout", "The app took too long to answer. Check the Flyers page in the app.");
    throw new AppError(
      "unreachable",
      `Can't reach the app at ${appUrl}. Is “pricetracker serve” running? (Use http://127.0.0.1:PORT or http://localhost:PORT.)`,
    );
  }
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) throw new AppError("token", "The app needs the token: copy it from the app's “Browser add-on” page into Settings below.");
  if (!response.ok) {
    const detail = typeof data.detail === "string" ? data.detail : data.detail ? JSON.stringify(data.detail) : `HTTP ${response.status}`;
    throw new AppError("http", detail, response.status);
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

function pageInfo() {
  return {
    page_url: (tab.url || "").slice(0, 2000),
    user_agent: scan ? scan.userAgent : navigator.userAgent,
  };
}

function chosenStore() {
  const store = $("store").value.trim();
  if (!store) {
    $("store").focus();
    throw new AppError("input", "Choose the store first.");
  }
  return store;
}

async function ensureDraft() {
  const existing = await currentDraft();
  if (existing) return existing;
  const created = await call("/api/flyers", {
    method: "POST",
    timeout: QUICK_TIMEOUT,
    json: { store: chosenStore(), title: $("title").value.trim() || null, page_url: pageInfo().page_url },
  });
  const draft = { id: created.id, store: created.store, files: created.files, url: created.url };
  await saveDraft(draft);
  return draft;
}

function draftNote(draft) {
  // Pages always go to the flyer in progress, whose store was set when it was started.
  const store = $("store").value.trim();
  return store && store !== draft.store ? ` (added to the ${draft.store} flyer in progress)` : "";
}

async function sendInOneGo(content) {
  // A PDF, a single file or a page's text is a whole flyer: one request creates it and starts the
  // reading, so closing the popup meanwhile can't leave half a flyer behind. Any draft is untouched.
  const done = await call("/api/flyers/send", {
    method: "POST",
    timeout: SEND_TIMEOUT,
    json: { store: chosenStore(), title: $("title").value.trim() || null, ...pageInfo(), ...content },
  });
  const parts = done.chunks > 1 ? ` (${done.chunks} parts)` : "";
  showResult(`Sent${parts}. Claude is reading it now; prices appear in the app in a minute or two.`, { link: done.url });
}

async function refreshDraft() {
  const draft = await currentDraft();
  if (!draft) return renderDraft(null);
  try {
    const status = await call(`/api/flyers/${draft.id}`, { timeout: QUICK_TIMEOUT });
    if (!status.collecting) return saveDraft(null); // finished or read from the app meanwhile
    await saveDraft({ ...draft, files: status.files });
  } catch (e) {
    // Forget it only when the app says it no longer exists; any other error may be passing.
    if (e.kind === "http" && e.status === 404 && /No such flyer/.test(e.message)) await saveDraft(null);
    else throw e;
  }
}

async function finishDraft() {
  const draft = await currentDraft();
  if (!draft) return;
  let done;
  try {
    done = await call(`/api/flyers/${draft.id}/finish`, { method: "POST" });
  } catch (e) {
    if (e.kind === "http" && /already sent|no longer accepting|No such flyer/.test(e.message)) {
      await saveDraft(null); // it was read from the app meanwhile
      return showResult("This flyer was already sent from the app.", { link: draft.url });
    }
    throw e;
  }
  await saveDraft(null);
  showResult(`Sent ${done.files} file(s). Claude is reading the flyer now; prices appear in the app in a minute or two.`, { link: done.url });
}

// ---- actions -------------------------------------------------------------------------------------

async function addImageUrls(urls) {
  const draft = await ensureDraft();
  const result = await call(`/api/flyers/${draft.id}/urls`, {
    method: "POST",
    timeout: SEND_TIMEOUT,
    json: { urls: urls.filter((u) => u.length <= MAX_URL), ...pageInfo() },
  });
  await saveDraft({ ...draft, files: result.files });
  if (!result.added && !result.replaced) {
    const why = result.skipped.length ? result.skipped[0].reason : "nothing usable";
    throw new AppError("http", `Nothing could be added (${why}). Try a screenshot instead.`);
  }
  const sharper = result.replaced ? ` Replaced ${result.replaced} with a sharper copy.` : "";
  const skipped = result.skipped_count ? ` Skipped ${result.skipped_count} (small, duplicate or unreadable).` : "";
  showResult(`Added ${result.added} page image(s)${draftNote(draft)}.${sharper}${skipped} Add more pages, or press “Read now”.`);
}

function dataUrlToBlob(dataUrl) {
  const [head, data] = dataUrl.split(",", 2);
  const mime = (head.match(/data:([^;]+)/) || [])[1] || "image/png";
  const bytes = atob(data);
  const buffer = new Uint8Array(bytes.length);
  for (let i = 0; i < bytes.length; i++) buffer[i] = bytes.charCodeAt(i);
  return new Blob([buffer], { type: mime });
}

async function addScreenshots(dataUrls) {
  // One upload per screenshot, so one oversized slice can't lose the others.
  let draft = await ensureDraft();
  let added = 0;
  for (const dataUrl of dataUrls) {
    const form = new FormData();
    form.append("files", dataUrlToBlob(dataUrl), `screenshot-${String(draft.files + 1).padStart(3, "0")}.jpg`);
    const status = await call(`/api/flyers/${draft.id}/pages`, { method: "POST", form, timeout: SEND_TIMEOUT });
    draft = { ...draft, files: status.files };
    await saveDraft(draft);
    added++;
  }
  showResult(`Added ${added} screenshot(s)${draftNote(draft)}, ${draft.files} file(s) in total. Show the next page and add it, or press “Read now”.`);
}

async function captureVisible() {
  return [await ext.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: JPEG_QUALITY })];
}

async function imageSize(dataUrl) {
  const img = new Image();
  img.src = dataUrl;
  await img.decode();
  return { width: img.naturalWidth, height: img.naturalHeight };
}

async function captureFullPage() {
  // Firefox's captureVisibleTab accepts a `rect` beyond the visible area, with the permission the
  // toolbar click grants. The result is scaled by the page zoom, so check its shape, not its size.
  const { width, height } = scan.page;
  const shots = [];
  for (let y = 0; y < height; y += SLICE_HEIGHT) {
    const rect = { x: 0, y, width, height: Math.min(SLICE_HEIGHT, height - y) };
    const shot = await ext.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: JPEG_QUALITY, rect, scale: 1 });
    const size = await imageSize(shot);
    const expectedRatio = rect.height / rect.width;
    if (size.height / size.width < expectedRatio * 0.9) {
      // Only the visible area came back: this Firefox ignores `rect`.
      if (!shots.length) throw new AppError("input", "This Firefox could only capture the visible area: use “Add visible area” page by page.");
      break;
    }
    shots.push(shot);
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
    button.addEventListener("click", () => busy(button, () => sendInOneGo({ urls: [pdf.url] })));
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

  $("shot-full").hidden = !(isFirefox && scan.page.height > 0);
  $("scan-warning").hidden = !scan.timingBufferWasFull;

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
    const stores = await call("/api/stores", { timeout: QUICK_TIMEOUT });
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
  $("store").value = guessStore(tab.url || "", tab.title || "");

  const scanning = ext.scripting.executeScript({ target: { tabId: tab.id }, func: scanFlyerPage });
  loadStores().then(() => refreshDraft()).catch(() => {});
  try {
    const [injected] = await scanning;
    scan = injected.result;
    renderScan();
  } catch (e) {
    // Built-in PDF viewer, browser pages and other places where scripts can't run.
    scan = { pdfs: [], images: [], frames: [], text: "", priceCount: 0, timingBufferWasFull: false, page: { width: 0, height: 0 }, userAgent: navigator.userAgent };
    const isHttp = /^https?:/i.test(tab.url || "");
    $("scan-summary").textContent = isHttp
      ? "Can't look inside this tab (it may be a PDF open in Firefox's viewer). You can send its address: the app downloads it."
      : "Open a store's flyer page, then click the add-on again.";
    $("send-tab-url").hidden = !isHttp;
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
$("send-tab-url").addEventListener("click", () => busy($("send-tab-url"), () => sendInOneGo({ urls: [tab.url] })));
$("send-text").addEventListener("click", () => busy($("send-text"), () => sendInOneGo({ text: scan.text })));
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
    if (draft) await call(`/api/flyers/${draft.id}`, { method: "DELETE", timeout: QUICK_TIMEOUT }).catch(() => {});
    await saveDraft(null);
    showResult("Discarded.");
  }),
);

main().catch((e) => showResult(e.message || String(e), { error: true }));
