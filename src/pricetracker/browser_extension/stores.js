// Guess the store from the site being viewed. Names match the stores in the Price tracker app.
const STORE_HOSTS = [
  ["mercadona.es", "Mercadona"],
  ["lidl.es", "Lidl"],
  ["leaflets.schwarz", "Lidl"],
  ["aldi.es", "Aldi"],
  ["carrefour.es", "Carrefour"],
  ["dia.es", "Dia"],
  ["alcampo.es", "Alcampo"],
  ["eroski.es", "Eroski"],
  ["consum.es", "Consum"],
  ["bonpreuesclat.cat", "Bonpreu / Esclat"],
  ["bonpreu.cat", "Bonpreu / Esclat"],
  ["esclat.cat", "Bonpreu / Esclat"],
  ["condis.es", "Condis"],
  ["condisline.com", "Condis"],
  ["caprabo.com", "Caprabo"],
  ["capraboacasa.com", "Caprabo"],
  ["bonarea.com", "bonÀrea"],
  ["bonarea-online.com", "bonÀrea"],
  ["ahorramas.com", "Ahorramás"],
  ["elcorteingles.es", "Hipercor / El Corte Inglés"],
  ["hipercor.es", "Hipercor / El Corte Inglés"],
  ["plusfresc.cat", "Plusfresc"],
  ["spar.es", "Spar"],
  ["sorli.com", "Sorli"],
  ["sorliclic.com", "Sorli"],
];

// On flyer aggregators (Tiendeo...) the store only appears in the page title or address.
const STORE_WORDS = [
  [/mercadona/i, "Mercadona"], [/\blidl\b/i, "Lidl"], [/\baldi\b/i, "Aldi"], [/carrefour/i, "Carrefour"],
  [/\bdia\b(?!\w)/i, "Dia"], [/alcampo/i, "Alcampo"], [/eroski/i, "Eroski"], [/\bconsum\b/i, "Consum"],
  [/bonpreu|esclat/i, "Bonpreu / Esclat"], [/condis/i, "Condis"], [/caprabo/i, "Caprabo"],
  [/bon\s?[àa]rea/i, "bonÀrea"], [/ahorram[áa]s/i, "Ahorramás"], [/hipercor|corte ingl[ée]s/i, "Hipercor / El Corte Inglés"],
  [/plusfresc/i, "Plusfresc"], [/\bspar\b/i, "Spar"], [/\bsorli\b/i, "Sorli"],
];

function guessStore(url, title) {
  let host = "";
  try {
    host = new URL(url).hostname.toLowerCase();
  } catch (e) {
    return "";
  }
  for (const [domain, name] of STORE_HOSTS) {
    if (host === domain || host.endsWith("." + domain)) return name;
  }
  let path = new URL(url).pathname;
  try {
    path = decodeURIComponent(path);
  } catch (e) {
    /* malformed %-escape: use the path as it is */
  }
  const text = `${title || ""} ${path.replace(/[-_/]+/g, " ")}`;
  for (const [pattern, name] of STORE_WORDS) {
    if (pattern.test(text)) return name;
  }
  return "";
}
