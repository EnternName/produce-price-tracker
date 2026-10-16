// Runs inside the flyer page (injected with scripting.executeScript) and reports what could be sent:
// PDF links, large images (flyer viewers load one image per page), embedded viewer frames and the
// page's text. Must be self-contained: it is serialized and executed in the page.
function scanFlyerPage() {
  const MIN_SIDE = 400; // px; smaller images are logos, thumbnails or zoom tiles
  const MAX_IMAGES = 300;
  const MAX_URL = 4000;
  const PDF_RE = /\.pdf($|[?#])/i;
  const IMAGE_RE = /\.(jpe?g|png|webp|avif)($|[?#])/i;
  const DOWNLOAD_WORDS = /\bpdf\b|descarg|descarreg|download/i;
  // Lidl's viewer fetches a JSON description of the leaflet that names its PDF; the app follows it.
  const LEAFLET_JSON_RE = /leaflets\.schwarz\/v\d+\/flyer/i;
  const PRICE_RE = /\d+[.,']\d{2}\s?€|€\s?\d+[.,']\d{2}/g;
  const MAX_TEXT = 400000;
  const DEFAULT_TIMING_BUFFER = 250;

  const safeDecode = (s) => {
    try {
      return decodeURIComponent(s);
    } catch (e) {
      return s; // malformed %-escape, e.g. "50%-descuento"
    }
  };
  const abs = (u) => {
    if (!u) return null;
    try {
      const url = new URL(u, document.baseURI).href;
      return /^https?:/i.test(url) && url.length <= MAX_URL ? url : null;
    } catch (e) {
      return null;
    }
  };
  const fileName = (u) => safeDecode(u.split(/[?#]/)[0].split("/").pop() || u);

  const pdfs = new Map();
  const images = new Map();
  const frames = [];
  let order = 0;

  const addImage = (u, width, height, how) => {
    const url = abs(u);
    if (!url) return;
    const prev = images.get(url);
    if (prev) {
      if (width * height > prev.width * prev.height) Object.assign(prev, { width, height });
      return;
    }
    images.set(url, { url, width: width || 0, height: height || 0, how, order: order++ });
  };

  // srcset as the HTML spec splits it: candidates are separated by a comma followed by whitespace,
  // so CDN URLs with commas in them (".../w_600,q_auto/p1.jpg 600w") stay whole.
  const srcsetCandidates = (srcset) =>
    (srcset || "")
      .trim()
      .split(/,\s+/)
      .map((part) => {
        const [u, size = ""] = part.trim().split(/\s+/);
        const w = size.endsWith("w") ? parseInt(size, 10) : size.endsWith("x") ? parseFloat(size) * 1000 : 1;
        return { url: u.replace(/,+$/, ""), w: Number.isFinite(w) ? w : 1 };
      })
      .filter((c) => c.url);
  const largestFromSrcset = (srcset) => {
    const candidates = srcsetCandidates(srcset).sort((a, b) => b.w - a.w);
    return candidates.length ? candidates[0].url : null;
  };

  // 1. Links to PDFs, and "Descargar PDF" / "Descarregar fulletó" buttons whose link may not end in .pdf
  for (const a of document.querySelectorAll("a[href]")) {
    const url = abs(a.getAttribute("href"));
    if (!url || url.split("#")[0] === location.href.split("#")[0]) continue;
    const text = (a.textContent || a.getAttribute("aria-label") || a.title || "").replace(/\s+/g, " ").trim();
    const hints = `${text} ${typeof a.className === "string" ? a.className : ""} ${a.getAttribute("download") !== null ? "download" : ""}`;
    if (PDF_RE.test(url) || DOWNLOAD_WORDS.test(hints)) {
      if (!pdfs.has(url)) pdfs.set(url, { url, label: text.slice(0, 80) || fileName(url), sure: PDF_RE.test(url) });
    }
  }

  // 2. Embedded PDFs and viewer iframes
  for (const el of document.querySelectorAll("embed[src], object[data], iframe[src]")) {
    const url = abs(el.getAttribute("src") || el.getAttribute("data"));
    if (!url) continue;
    if (PDF_RE.test(url)) {
      if (!pdfs.has(url)) pdfs.set(url, { url, label: "Embedded PDF: " + fileName(url), sure: true });
    } else if (el.tagName === "IFRAME") {
      const r = el.getBoundingClientRect();
      if (r.width >= 400 && r.height >= 300) frames.push({ url, width: Math.round(r.width), height: Math.round(r.height) });
    }
  }

  // 3. Images in the page (the largest srcset candidate, and the one the browser actually picked)
  for (const img of document.images) {
    const best = largestFromSrcset(img.getAttribute("srcset"));
    if (best) addImage(best, 0, 0, "srcset");
    addImage(img.currentSrc || img.src, img.naturalWidth, img.naturalHeight, "img");
  }
  for (const source of document.querySelectorAll("picture source[srcset]")) {
    addImage(largestFromSrcset(source.getAttribute("srcset")), 0, 0, "picture");
  }

  // 4. CSS background images on large elements (some viewers draw pages this way)
  let checked = 0;
  for (const el of document.querySelectorAll("div, section, figure, li, a, span")) {
    if (checked++ > 4000) break;
    const r = el.getBoundingClientRect();
    if (r.width < 300 || r.height < 300) continue;
    const bg = getComputedStyle(el).backgroundImage;
    for (const m of bg.matchAll(/url\(["']?([^"')]+)["']?\)/g)) addImage(m[1], 0, 0, "css");
  }

  // 5. Everything the page downloaded, including images and PDFs fetched by viewer scripts.
  // Only files with an image extension count: tracking pixels usually have none.
  const entries = performance.getEntriesByType("resource");
  for (const entry of entries) {
    const url = entry.name;
    if (PDF_RE.test(url)) {
      if (!pdfs.has(url)) pdfs.set(url, { url, label: fileName(url), sure: true });
    } else if (LEAFLET_JSON_RE.test(url)) {
      if (!pdfs.has(url)) pdfs.set(url, { url, label: "PDF of the leaflet open in this viewer", sure: true });
    } else if (IMAGE_RE.test(url)) {
      // Same-origin entries report their size; tiny downloads are icons, not pages.
      const tiny = entry.encodedBodySize > 0 && entry.encodedBodySize < 25000;
      if (!tiny) addImage(url, 0, 0, "network");
    }
  }
  // The browser keeps only 250 of these by default; once full, later page images go unrecorded.
  // Raising the limit now means pages viewed from here on are all kept for the next scan.
  const timingBufferWasFull = entries.length >= DEFAULT_TIMING_BUFFER;
  try {
    performance.setResourceTimingBufferSize(20000);
  } catch (e) {
    /* not available */
  }

  const pageImages = [...images.values()]
    .filter((i) => !(i.width && i.height && Math.min(i.width, i.height) < MIN_SIDE))
    .sort((a, b) => a.order - b.order)
    .slice(0, MAX_IMAGES);

  // 6. Visible text, for store pages that list products with prices (offers pages, product grids).
  // Cut on whole characters and repair broken emoji halves so the text is valid Unicode.
  let text = (document.body ? document.body.innerText : "").slice(0, MAX_TEXT);
  text = typeof text.toWellFormed === "function"
    ? text.toWellFormed()
    : text.replace(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/g, "�");
  const priceCount = (text.match(PRICE_RE) || []).length;

  const doc = document.documentElement;
  const pdfList = [...pdfs.values()].sort((a, b) => Number(b.sure) - Number(a.sure));
  return {
    text,
    priceCount,
    title: document.title,
    url: location.href,
    pdfs: pdfList,
    images: pageImages,
    frames,
    timingBufferWasFull,
    page: {
      width: Math.max(doc.scrollWidth, document.body ? document.body.scrollWidth : 0),
      height: Math.max(doc.scrollHeight, document.body ? document.body.scrollHeight : 0),
      viewportWidth: window.innerWidth,
      viewportHeight: window.innerHeight,
    },
    userAgent: navigator.userAgent,
  };
}
