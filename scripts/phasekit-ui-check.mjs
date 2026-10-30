#!/usr/bin/env node
// phasekit ui-check (v0.23.0): the deterministic half of the UI standard
// (foundry-meta designs/DESIGN-ui-design-review.md, ROADMAP O15).
//
// For every page in the project's docs/project/ui-pages.json and every viewport
// it lists, in the runner image's pinned browser:
//   1. serves the project's static root on loopback and opens the page with
//      motion, time and randomness frozen (reduced motion, no animations or
//      transitions, a fixed Date, a seeded Math.random, service workers blocked);
//   2. runs the page's setup steps (data only: storage, click, press, waitFor, eval);
//   3. runs axe-core (WCAG 2.x A/AA): an impact `serious`/`critical` violation is RED;
//   4. measures tap targets: smaller than tap.block (24 px) is RED, smaller than
//      tap.advise (44 px) advisory;
//   5. lists the fonts the page actually rendered with: one that is neither the
//      image's reference family nor declared in `fonts` is RED;
//   6. screenshots the page and compares it with tests/ui-baselines/<id>@<viewport>.png:
//      a difference past the threshold is RED unless this is `--update` and the
//      phase declared the page under `Visual changes:` (then the baseline is
//      rewritten). A missing baseline is advisory; `--update` captures it.
// A page that fails to load or whose setup fails is RED; so is a run past
// PHASEKIT_UI_CHECK_TIMEOUT (default 120 s).
//
// Evidence goes to artifacts/logs/ui-check/<stamp>/ (an ignored path: the gate is
// read-only over the tracked tree): report.json, every screenshot and every diff.
// The report records the sha256 of every file the browser fetched.
//
// Exit: 0 GREEN (or not configured: no ui-pages.json), 1 RED, 2 usage or
// configuration error, 3 the tools are missing (it runs in the runner image:
// `phasekit verify --in-runner`, or a session/runner container).
//
// Usage (normally through the CLI: `phasekit ui-check …`, which resolves the
// project and, for --update --phase P, reads the phase's declaration):
//   node phasekit-ui-check.mjs [--project DIR] [--json] [--update] [--declared-json JSON]
//   node phasekit-ui-check.mjs init [--project DIR]   seed the three companion files (never overwrites)
//
// No dependencies of its own: playwright-core and axe-core come from the image's
// pinned prefix (PHASEKIT_UI_PREFIX, default /opt/phasekit-ui). The PNG codec
// below is pure (zlib only) and is exported for the tests.

import { execFileSync } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import zlib from "node:zlib";

export const CONFIG_PATH = "docs/project/ui-pages.json";
export const BASELINE_DIR = "tests/ui-baselines";
export const EVIDENCE_DIR = "artifacts/logs/ui-check";
export const VIEWPORTS = {
  phone: { width: 390, height: 844 },
  desktop: { width: 1440, height: 900 },
};
export const REFERENCE_FONTS = ["DejaVu Sans", "DejaVu Sans Mono"];
export const DEFAULTS = {
  root: ".",
  threshold: { channel: 16, share: 0.001 },
  tap: { block: 24, advise: 44 },
  fixedTime: "2026-01-01T12:00:00Z",
  seed: 1,
};
const BLOCKING_IMPACTS = new Set(["serious", "critical"]);
const MAX_FONT_NODES = 400;
const MAX_WAIT_MS = 5000;
const SETUP_OPS = new Set(["storage", "click", "press", "waitFor", "eval", "wait"]);

// --- PNG codec (8-bit RGB/RGBA, non-interlaced: what Chromium writes) -------

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32(buf) {
  let c = 0xffffffff;
  for (const b of buf) c = CRC_TABLE[(c ^ b) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

const PNG_SIG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

export function decodePng(buf) {
  if (!Buffer.isBuffer(buf) || buf.length < 8 || !buf.subarray(0, 8).equals(PNG_SIG)) {
    throw new Error("not a PNG");
  }
  let off = 8, width = 0, height = 0, colorType = -1;
  const idat = [];
  while (off + 8 <= buf.length) {
    const len = buf.readUInt32BE(off);
    const type = buf.toString("latin1", off + 4, off + 8);
    const data = buf.subarray(off + 8, off + 8 + len);
    if (type === "IHDR") {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      const depth = data[8];
      colorType = data[9];
      const interlace = data[12];
      if (depth !== 8 || (colorType !== 2 && colorType !== 6) || interlace !== 0) {
        throw new Error(`unsupported PNG (depth ${depth}, colour type ${colorType}, interlace ${interlace})`);
      }
    } else if (type === "IDAT") {
      idat.push(data);
    } else if (type === "IEND") {
      break;
    }
    off += 12 + len;
  }
  if (!width || !height) throw new Error("PNG has no IHDR");
  const bpp = colorType === 6 ? 4 : 3;
  const raw = zlib.inflateSync(Buffer.concat(idat));
  const stride = width * bpp;
  if (raw.length < height * (stride + 1)) throw new Error("PNG data is truncated");
  const out = Buffer.alloc(width * height * 4);
  let prev = Buffer.alloc(stride);
  for (let y = 0; y < height; y++) {
    const ft = raw[y * (stride + 1)];
    const line = Buffer.from(raw.subarray(y * (stride + 1) + 1, (y + 1) * (stride + 1)));
    for (let i = 0; i < stride; i++) {
      const a = i >= bpp ? line[i - bpp] : 0;
      const b = prev[i];
      const c = i >= bpp ? prev[i - bpp] : 0;
      let v;
      switch (ft) {
        case 0: v = 0; break;
        case 1: v = a; break;
        case 2: v = b; break;
        case 3: v = (a + b) >> 1; break;
        case 4: {
          const p = a + b - c, pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
          v = pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
          break;
        }
        default: throw new Error(`bad PNG filter ${ft}`);
      }
      line[i] = (line[i] + v) & 0xff;
    }
    for (let x = 0; x < width; x++) {
      const o = (y * width + x) * 4;
      out[o] = line[x * bpp];
      out[o + 1] = line[x * bpp + 1];
      out[o + 2] = line[x * bpp + 2];
      out[o + 3] = bpp === 4 ? line[x * bpp + 3] : 255;
    }
    prev = line;
  }
  return { width, height, data: out };
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const td = Buffer.concat([Buffer.from(type, "latin1"), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(td));
  return Buffer.concat([len, td, crc]);
}

export function encodePng({ width, height, data }) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; ihdr[9] = 6; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
  const raw = Buffer.alloc(height * (width * 4 + 1));
  for (let y = 0; y < height; y++) {
    data.copy(raw, y * (width * 4 + 1) + 1, y * width * 4, (y + 1) * width * 4);
  }
  return Buffer.concat([PNG_SIG, chunk("IHDR", ihdr), chunk("IDAT", zlib.deflateSync(raw)), chunk("IEND", Buffer.alloc(0))]);
}

// Compare two decoded images. A pixel changed when any channel differs by more
// than `channel`; the result is the share of changed pixels (1 when the sizes
// differ) and a diff image (changed pixels red over a faded copy of `b`).
export function diffImages(a, b, channel = DEFAULTS.threshold.channel) {
  if (a.width !== b.width || a.height !== b.height) {
    return { changed: a.width * a.height || 1, share: 1, sizeChanged: true, image: null };
  }
  const n = a.width * a.height;
  const img = Buffer.alloc(n * 4);
  let changed = 0;
  for (let i = 0; i < n; i++) {
    const o = i * 4;
    let hit = false;
    for (let k = 0; k < 4; k++) {
      if (Math.abs(a.data[o + k] - b.data[o + k]) > channel) { hit = true; break; }
    }
    if (hit) {
      changed++;
      img[o] = 255; img[o + 1] = 0; img[o + 2] = 0; img[o + 3] = 255;
    } else {
      const g = (b.data[o] * 0.3 + b.data[o + 1] * 0.59 + b.data[o + 2] * 0.11) | 0;
      const f = 255 - ((255 - g) >> 2);
      img[o] = f; img[o + 1] = f; img[o + 2] = f; img[o + 3] = 255;
    }
  }
  return { changed, share: changed / n, sizeChanged: false, image: { width: a.width, height: a.height, data: img } };
}

// --- configuration -----------------------------------------------------------

// The declared viewports of a page: `phone`, `desktop`, or either with `-dark`
// (the page under prefers-color-scheme: dark).
export function parseViewport(name) {
  const m = /^(phone|desktop)(-dark)?$/.exec(String(name));
  if (!m) return null;
  return { name: String(name), ...VIEWPORTS[m[1]], colorScheme: m[2] ? "dark" : "light" };
}

export function loadConfig(raw) {
  const errors = [];
  let cfg;
  try {
    cfg = JSON.parse(raw);
  } catch (e) {
    return { errors: [`${CONFIG_PATH} is not JSON: ${e.message}`] };
  }
  if (!cfg || typeof cfg !== "object" || Array.isArray(cfg)) return { errors: [`${CONFIG_PATH} must be an object`] };
  const out = {
    root: typeof cfg.root === "string" ? cfg.root : DEFAULTS.root,
    fonts: Array.isArray(cfg.fonts) ? cfg.fonts.map(String) : [],
    ui_paths: Array.isArray(cfg.ui_paths) ? cfg.ui_paths.map(String) : [],
    threshold: { ...DEFAULTS.threshold, ...(cfg.threshold || {}) },
    tap: { ...DEFAULTS.tap, ...(cfg.tap || {}) },
    fixedTime: typeof cfg.fixedTime === "string" ? cfg.fixedTime : DEFAULTS.fixedTime,
    seed: Number.isInteger(cfg.seed) ? cfg.seed : DEFAULTS.seed,
    pages: [],
  };
  if (path.isAbsolute(out.root) || out.root.split(/[\\/]/).includes("..")) errors.push("`root` must be a path inside the project");
  // A project may tighten the thresholds, never loosen them: a looser one would
  // let an undeclared visual change through without a `Visual changes:` line.
  for (const [k, v] of Object.entries(out.threshold)) {
    if (!(k in DEFAULTS.threshold)) errors.push(`threshold.${k} is not a known threshold`);
    else if (typeof v !== "number" || !(v >= 0) || v > DEFAULTS.threshold[k]) {
      errors.push(`threshold.${k} must be a number from 0 to ${DEFAULTS.threshold[k]} (a project may tighten it, not loosen it)`);
    }
  }
  // Never looser than the standard: a project may raise the blocking minimum, not lower it.
  if (!(out.tap.block >= DEFAULTS.tap.block)) errors.push(`tap.block may not be below ${DEFAULTS.tap.block}`);
  if (!(out.tap.advise >= out.tap.block)) errors.push("tap.advise must be >= tap.block");
  if (!Array.isArray(cfg.pages) || cfg.pages.length === 0) errors.push("`pages` must be a non-empty list");
  const ids = new Set();
  for (const [i, p] of (Array.isArray(cfg.pages) ? cfg.pages : []).entries()) {
    const where = `pages[${i}]`;
    if (!p || typeof p.id !== "string" || !/^[A-Za-z0-9_.-]+$/.test(p.id)) { errors.push(`${where}.id must match [A-Za-z0-9_.-]+`); continue; }
    if (ids.has(p.id)) errors.push(`${where}.id '${p.id}' is a duplicate`);
    ids.add(p.id);
    if (typeof p.path !== "string" || !p.path.startsWith("/")) errors.push(`${where}.path must start with /`);
    const vps = Array.isArray(p.viewports) && p.viewports.length ? p.viewports : ["phone", "desktop"];
    const parsed = vps.map(parseViewport);
    if (parsed.some((v) => !v)) errors.push(`${where}.viewports: each is phone, desktop, phone-dark or desktop-dark`);
    const setup = Array.isArray(p.setup) ? p.setup : [];
    for (const [j, s] of setup.entries()) {
      const ops = s && typeof s === "object" ? Object.keys(s) : [];
      if (ops.length !== 1 || !SETUP_OPS.has(ops[0])) {
        errors.push(`${where}.setup[${j}] must be one of {${[...SETUP_OPS].join("|")}: …}`);
      } else if (ops[0] === "wait" && !(Number.isFinite(s.wait) && s.wait >= 0 && s.wait <= MAX_WAIT_MS)) {
        errors.push(`${where}.setup[${j}].wait must be 0..${MAX_WAIT_MS} ms`);
      }
    }
    out.pages.push({ id: p.id, path: p.path, setup, viewports: parsed.filter(Boolean) });
  }
  return errors.length ? { errors } : { config: out };
}

// Which baselines a `--update` may rewrite: every viewport of a declared page
// id, or the one viewport of a declared `id@viewport`.
export const DECLARED_RE = /^[A-Za-z0-9_.-]+(@(phone|desktop)(-dark)?)?$/;

export function isDeclared(declared, id, viewport) {
  return declared.includes(id) || declared.includes(`${id}@${viewport}`);
}

// --- the static server -------------------------------------------------------

const TYPES = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".mjs": "text/javascript",
  ".css": "text/css", ".json": "application/json", ".webmanifest": "application/manifest+json",
  ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
  ".gif": "image/gif", ".webp": "image/webp", ".ico": "image/x-icon", ".woff2": "font/woff2",
  ".woff": "font/woff", ".ttf": "font/ttf", ".txt": "text/plain; charset=utf-8", ".wasm": "application/wasm",
};

function serve(rootDir, fetched) {
  const root = fs.realpathSync(rootDir);
  const server = http.createServer((req, res) => {
    let rel;
    try {
      rel = decodeURIComponent(new URL(req.url, "http://x").pathname);
    } catch {
      res.writeHead(400).end();
      return;
    }
    let file = path.resolve(root, "." + rel);
    if (file !== root && !file.startsWith(root + path.sep)) { res.writeHead(403).end(); return; }
    try {
      if (fs.statSync(file).isDirectory()) file = path.join(file, "index.html");
      const real = fs.realpathSync(file);
      if (real !== root && !real.startsWith(root + path.sep)) { res.writeHead(403).end(); return; }
      const body = fs.readFileSync(real);
      fetched.set(path.relative(root, real), crypto.createHash("sha256").update(body).digest("hex"));
      res.writeHead(200, { "content-type": TYPES[path.extname(real).toLowerCase()] || "application/octet-stream",
        "cache-control": "no-store" });
      res.end(body);
    } catch {
      res.writeHead(404).end();
    }
  });
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve(server));
  });
}

// --- the in-page probes ------------------------------------------------------

const FREEZE_STYLE = "*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important;scroll-behavior:auto!important}";

function initScript({ seed, storage, style }) {
  // Runs before any page script: a seeded Math.random, the given storage, and
  // the freeze style as soon as there is a document.
  let s = seed >>> 0;
  Math.random = () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  // crypto's random sources too, from the same seeded stream.
  if (globalThis.crypto) {
    crypto.getRandomValues = (arr) => {
      const bytes = new Uint8Array(arr.buffer, arr.byteOffset, arr.byteLength);
      for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
      return arr;
    };
    crypto.randomUUID = () => "10000000-1000-4000-8000-100000000000".replace(/[018]/g, (c) =>
      (c ^ (Math.floor(Math.random() * 256) & (15 >> (c / 4)))).toString(16));
  }
  try {
    for (const [k, v] of Object.entries(storage)) localStorage.setItem(k, v);
  } catch { /* storage unavailable: setup steps that need it will show it */ }
  const add = () => {
    const el = document.createElement("style");
    el.setAttribute("data-phasekit-ui-check", "");
    el.textContent = style;
    (document.head || document.documentElement).appendChild(el);
  };
  if (document.documentElement) add();
  else document.addEventListener("DOMContentLoaded", add, { once: true });
}

function measureTapTargets(min) {
  const sel = "a[href],button,input:not([type=hidden]),select,textarea,summary,[role=button],[role=link]," +
    "[role=checkbox],[role=radio],[role=switch],[role=tab],[role=menuitem],[role=option],[role=slider],[tabindex]:not([tabindex='-1'])";
  const out = [];
  for (const el of document.querySelectorAll(sel)) {
    const cs = getComputedStyle(el);
    if (cs.visibility === "hidden" || cs.display === "none" || el.closest("[hidden],[inert],[aria-hidden=true]")) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    // WCAG 2.5.8 "inline" exception: a link inside a sentence, i.e. an inline
    // link whose parent carries running text of its own (a non-blank text node
    // beside it), not a link that merely shares a container with other elements.
    if (el.tagName === "A" && cs.display === "inline" && el.parentElement &&
        [...el.parentElement.childNodes].some((n) => n.nodeType === 3 && /[\p{L}\p{N}]/u.test(n.nodeValue))) continue;
    if (Math.min(r.width, r.height) < min) {
      const name = (el.getAttribute("aria-label") || el.textContent || el.getAttribute("title") || "").trim().slice(0, 60);
      out.push({ tag: el.tagName.toLowerCase(), id: el.id || null, name, width: Math.round(r.width * 10) / 10, height: Math.round(r.height * 10) / 10 });
    }
  }
  return out;
}

async function renderedFonts(cdp) {
  await cdp.send("DOM.enable");
  await cdp.send("CSS.enable");
  const { root } = await cdp.send("DOM.getDocument", { depth: -1, pierce: true });
  const targets = [];
  const walk = (n) => {
    if (targets.length >= MAX_FONT_NODES) return;
    if (n.nodeType === 1 && !["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE"].includes(n.nodeName) &&
        (n.children || []).some((c) => c.nodeType === 3 && c.nodeValue.trim())) targets.push(n.nodeId);
    for (const c of n.children || []) walk(c);
    for (const c of n.shadowRoots || []) walk(c);
    if (n.contentDocument) walk(n.contentDocument);
  };
  walk(root);
  const fams = new Map();
  let failed = 0;
  for (const nodeId of targets) {
    try {
      const { fonts } = await cdp.send("CSS.getPlatformFontsForNode", { nodeId });
      for (const f of fonts || []) {
        const e = fams.get(f.familyName) || { family: f.familyName, custom: !!f.isCustomFont, glyphs: 0 };
        e.glyphs += f.glyphCount || 0;
        fams.set(f.familyName, e);
      }
    } catch {
      failed++; // a node gone since the walk
    }
  }
  return { fonts: [...fams.values()], partial: targets.length >= MAX_FONT_NODES, failed };
}

// --- the run -----------------------------------------------------------------

function stamp() {
  return new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z") + "-" + process.pid;
}

// Is `rel` (project-relative) in the project's last commit? (No git, no
// commit, or not tracked: no.)
function trackedInHead(project, rel) {
  try {
    execFileSync("git", ["-C", project, "cat-file", "-e", `HEAD:./${rel}`], { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

function sha256(buf) {
  return crypto.createHash("sha256").update(buf).digest("hex");
}

function loadTools(prefix) {
  const req = createRequire(path.join(prefix, "noop.js"));
  let playwright, axePath;
  try {
    playwright = req("playwright-core");
    axePath = req.resolve("axe-core/axe.min.js");
  } catch {
    return null;
  }
  return { playwright, axeSource: fs.readFileSync(axePath, "utf8"),
    versions: { "playwright-core": req("playwright-core/package.json").version, "axe-core": req("axe-core/package.json").version } };
}

export async function run(opts) {
  const project = path.resolve(opts.project);
  const cfgFile = path.join(project, CONFIG_PATH);
  if (!fs.existsSync(cfgFile)) {
    return { code: 0, report: { status: "not-configured", note: `no ${CONFIG_PATH}: nothing to check` } };
  }
  const { config, errors } = loadConfig(fs.readFileSync(cfgFile, "utf8"));
  if (errors) return { code: 2, report: { status: "config-error", errors } };
  const rootDir = path.join(project, config.root);
  if (!fs.existsSync(rootDir)) return { code: 2, report: { status: "config-error", errors: [`root '${config.root}' does not exist`] } };
  const realProject = fs.realpathSync(project), realRoot = fs.realpathSync(rootDir);
  if (realRoot !== realProject && !realRoot.startsWith(realProject + path.sep)) {
    return { code: 2, report: { status: "config-error", errors: [`root '${config.root}' resolves outside the project`] } };
  }

  // A declaration names configured pages only: a word that happens to be a
  // valid id but is no page (or a viewport the page does not list) is refused.
  const unknown = (opts.declared || []).filter((d) => {
    const [id, vpName] = d.split("@");
    const pg = config.pages.find((p) => p.id === id);
    return !pg || (vpName && !pg.viewports.some((v) => v.name === vpName));
  });
  if (unknown.length) {
    return { code: 2, report: { status: "config-error",
      errors: [`the declaration names pages or viewports ${CONFIG_PATH} does not have: ${unknown.join(", ")}`] } };
  }
  const prefix = opts.prefix || process.env.PHASEKIT_UI_PREFIX || "/opt/phasekit-ui";
  // The image installs the browser under /home/node; a run under another HOME
  // (uid 0 with a throwaway HOME) must still find that pinned build. Set before
  // playwright-core is loaded: it resolves its browser registry at load time.
  if (!process.env.PLAYWRIGHT_BROWSERS_PATH && fs.existsSync("/home/node/.cache/ms-playwright")) {
    process.env.PLAYWRIGHT_BROWSERS_PATH = "/home/node/.cache/ms-playwright";
  }
  const tools = loadTools(prefix);
  if (!tools) {
    return { code: 3, report: { status: "tools-missing",
      note: `playwright-core and axe-core were not found under ${prefix}: ui-check runs in the runner image ` +
        "(phasekit verify --in-runner, or a session/runner container)" } };
  }
  const fontsConf = path.join(prefix, "fonts", "fonts.conf");

  const evidenceRoot = path.join(project, EVIDENCE_DIR);
  const outDir = path.join(evidenceRoot, opts.stamp || stamp());
  fs.mkdirSync(outDir, { recursive: true });
  pruneEvidence(evidenceRoot, outDir);
  const baseDir = path.join(project, BASELINE_DIR);
  const declared = opts.declared || [];
  const fetched = new Map();
  const report = { schema: 1, status: "red", update: !!opts.update, declared, versions: tools.versions,
    config: { root: config.root, threshold: config.threshold, tap: config.tap, fonts: config.fonts },
    pages: [], red: [], advisory: [], fetched: {}, blocked: [] };
  const red = (page, viewport, rule, detail) => report.red.push({ page, viewport, rule, detail });
  const advise = (page, viewport, rule, detail) => report.advisory.push({ page, viewport, rule, detail });
  const writeReport = () => {
    report.fetched = Object.fromEntries([...fetched.entries()].sort());
    report.evidence = path.relative(project, outDir);
    fs.writeFileSync(path.join(outDir, "report.json"), JSON.stringify(report, null, 2) + "\n");
  };

  let server, browser;
  try {
    server = await serve(rootDir, fetched);
    const origin = `http://127.0.0.1:${server.address().port}`;
    // channel "chromium": the image's pinned full Chromium in new headless mode
    // (the image installs no separate headless-shell build).
    browser = await tools.playwright.chromium.launch({
      headless: true, channel: "chromium",
      env: { ...process.env, ...(fs.existsSync(fontsConf) ? { FONTCONFIG_FILE: fontsConf } : {}) },
      args: ["--font-render-hinting=none", "--disable-lcd-text", "--force-color-profile=srgb"],
    });
    report.versions.browser = browser.version();
    for (const page of config.pages) {
      for (const vp of page.viewports) {
        const entry = { id: page.id, viewport: vp.name, ok: false };
        report.pages.push(entry);
        const before = report.red.length;
        try {
          await checkView({ browser, origin, page, vp, entry, config, tools, opts, declared, outDir, baseDir, red, advise, report });
        } catch (e) {
          red(page.id, vp.name, "error", String((e && e.message) || e).split("\n")[0]);
        }
        // A view that did not finish never passes quietly.
        if (!entry.ok && report.red.length === before) red(page.id, vp.name, "error", "the page view did not complete");
      }
    }
    report.status = report.red.length ? "red" : "green";
  } catch (e) {
    report.note = `ui-check failed: ${String((e && e.message) || e).split("\n")[0]}`;
    report.status = "red";
  } finally {
    if (browser) await browser.close().catch(() => {});
    if (server) server.close();
    writeReport();
  }
  return { code: report.status === "green" ? 0 : 1, report };
}

// Keep the newest EVIDENCE_KEEP evidence directories (the current one always).
const EVIDENCE_KEEP = 10;
function pruneEvidence(root, current) {
  try {
    const dirs = fs.readdirSync(root, { withFileTypes: true }).filter((d) => d.isDirectory())
      .map((d) => d.name).sort().reverse();
    for (const name of dirs.slice(EVIDENCE_KEEP)) {
      if (path.join(root, name) !== current) fs.rmSync(path.join(root, name), { recursive: true, force: true });
    }
  } catch { /* evidence housekeeping never fails a check */ }
}

// Advance the page's fake clock (installed per context) by `ms` virtual
// milliseconds, so timers, intervals and animation frames run the same way on
// every run.
const SETTLE_MS = 500;
const MAX_BLOCKED = 20;
const STEP_LIMIT_MS = 10000;

// Run `promise` while stepping the paused clock, so page code that waits on a
// timer can finish; at most STEP_LIMIT_MS of page time.
async function stepUntil(tab, promise, what) {
  let done = false, value, error;
  promise.then((v) => { done = true; value = v; }, (e) => { done = true; error = e; });
  for (let t = 0; !done && t < STEP_LIMIT_MS; t += 50) {
    await new Promise((r) => setImmediate(r));
    if (!done) await tab.clock.runFor(50);
  }
  await new Promise((r) => setImmediate(r));
  if (!done) throw new Error(`${what}: not finished after ${STEP_LIMIT_MS / 1000} s of page time`);
  if (error) throw error;
  return value;
}

async function checkView({ browser, origin, page, vp, entry, config, tools, opts, declared, outDir, baseDir, red, advise, report }) {
  const key = `${page.id}@${vp.name}`;
  const viewRedStart = report.red.length;
  const ctx = await browser.newContext({
    viewport: { width: vp.width, height: vp.height }, deviceScaleFactor: 1, colorScheme: vp.colorScheme,
    reducedMotion: "reduce", serviceWorkers: "block", locale: "en-US", timezoneId: "UTC", bypassCSP: true,
  });
  try {
    // Time is virtual: Date, timers and animation frames advance only when the
    // check runs the clock, and so do differences between performance.now
    // readings. performance.now's absolute origin still carries the real time
    // the page took to start (Playwright's fake clock replaces the whole
    // performance object), so a page must not display a raw performance.now.
    // Installed here, then PAUSED once the page exists (pausing a context with no
    // page hangs): an installed clock otherwise keeps flowing in real time.
    const t0 = new Date(config.fixedTime).getTime();
    await ctx.clock.install({ time: t0 });
    // Nothing leaves loopback: any other request is blocked and RED (it would make
    // the page depend on the network, and its bytes would be missing from the record).
    const blocked = new Set();
    const block = (url) => {
      if (blocked.has(url) || blocked.size >= MAX_BLOCKED) return;
      blocked.add(url);
      report.blocked.push(url);
      red(page.id, vp.name, "external-request", `blocked a request off loopback: ${url.slice(0, 200)}`);
    };
    await ctx.route("**/*", (route) => {
      const url = route.request().url();
      if (url.startsWith(origin + "/") || url === origin) return route.continue();
      block(url);
      return route.abort("blockedbyclient");
    });
    // WebSockets are not requests: route them too (loopback passes through).
    const wsOrigin = origin.replace(/^http/, "ws");
    await ctx.routeWebSocket(/.*/, (ws) => {
      if (ws.url().startsWith(wsOrigin + "/") || ws.url() === wsOrigin) { ws.connectToServer(); return; }
      block(ws.url());
      ws.close({ code: 1008, reason: "phasekit ui-check: off loopback" });
    });
    const storage = {};
    for (const s of page.setup) {
      if (s.storage) for (const [k, v] of Object.entries(s.storage)) storage[k] = typeof v === "string" ? v : JSON.stringify(v);
    }
    await ctx.addInitScript(initScript, { seed: config.seed, storage, style: FREEZE_STYLE });
    const tab = await ctx.newPage();
    await tab.clock.pauseAt(t0 + 1000);
    const pageErrors = [];
    tab.on("pageerror", (e) => pageErrors.push(String(e.message || e).split("\n")[0]));
    tab.on("requestfailed", (r) => {
      if (r.url().startsWith(origin) && !blocked.has(r.url())) {
        red(page.id, vp.name, "request-failed", `${(r.failure() && r.failure().errorText) || "failed"} for ${r.url().slice(origin.length)}`);
      }
    });
    tab.on("response", (r) => {
      if (r.url().startsWith(origin) && r.status() >= 400) {
        red(page.id, vp.name, "request-failed", `HTTP ${r.status()} for ${r.url().slice(origin.length)}`);
      }
    });
    const resp = await tab.goto(origin + page.path, { waitUntil: "load", timeout: 30000 });
    if (!resp || !resp.ok()) {
      if (!resp) red(page.id, vp.name, "page-load", `no response for ${page.path}`);
      return; // a 4xx/5xx document is already RED through the response listener
    }
    await tab.clock.runFor(SETTLE_MS);
    try {
      for (const s of page.setup) {
        if (s.click) await tab.click(s.click, { timeout: 10000 });
        else if (s.press) await tab.keyboard.press(s.press);
        else if (s.waitFor) {
          // Virtual time: advance the clock until the element is there (10 s of page time at most).
          let t = 0;
          while (!(await tab.$(s.waitFor)) && t < 10000) { await tab.clock.runFor(100); t += 100; }
          if (!(await tab.$(s.waitFor))) throw new Error(`waitFor '${s.waitFor}': not there after 10 s of page time`);
        }
        else if (s.eval) await stepUntil(tab, tab.evaluate(s.eval), `eval '${String(s.eval).slice(0, 40)}'`);
        if (s.wait !== undefined) await tab.clock.runFor(s.wait);
        else if (!s.storage) await tab.clock.runFor(SETTLE_MS);
      }
    } catch (e) {
      red(page.id, vp.name, "setup", String(e.message || e).split("\n")[0]);
      return;
    }
    try {
      await tab.waitForLoadState("networkidle", { timeout: 15000 });
    } catch {
      red(page.id, vp.name, "never-settled", "the page still had network requests in flight after 15 s");
      return;
    }
    await tab.evaluate(() => document.fonts.ready);
    await tab.clock.runFor(SETTLE_MS);
    const earlyErrors = pageErrors.length;
    for (const msg of pageErrors.slice(0, 5)) red(page.id, vp.name, "page-error", msg);

    // screenshot vs baseline: FIRST, at the fixed settle point (the probes below
    // step the clock for axe, so the page may move on after this)
    const shot = await tab.screenshot({ fullPage: true, animations: "disabled", caret: "hide" });
    let pendingWrite = null;
    fs.writeFileSync(path.join(outDir, `${key}.png`), shot);
    entry.screenshot = `${key}.png`;
    entry.screenshot_sha256 = sha256(shot);
    const baseFile = path.join(baseDir, `${key}.png`);
    const mayRewrite = opts.update && isDeclared(declared, page.id, vp.name);
    // Baselines are written only at the end, and only if the view stayed clean:
    // a view that goes RED never leaves a new baseline behind.
    const rewrite = (why) => {
      entry.baseline = "updated";
      pendingWrite = () => {
        fs.writeFileSync(path.join(outDir, `${key}.before.png`), fs.readFileSync(baseFile));
        fs.writeFileSync(baseFile, shot);
        advise(page.id, vp.name, "baseline", `declared visual change: baseline rewritten (${why})`);
      };
    };
    if (!fs.existsSync(baseFile) && !mayRewrite && trackedInHead(path.resolve(baseDir, "..", ".."), `${BASELINE_DIR}/${key}.png`)) {
      // A baseline the last commit has and the tree lost is a deleted check, not
      // a new page (a declared --update captures it again).
      entry.baseline = "deleted";
      red(page.id, vp.name, "baseline-deleted", `${BASELINE_DIR}/${key}.png is tracked in HEAD but missing from the tree`);
    } else if (!fs.existsSync(baseFile)) {
      if (opts.update) {
        entry.baseline = "captured";
        pendingWrite = () => {
          fs.mkdirSync(baseDir, { recursive: true });
          fs.writeFileSync(baseFile, shot);
          advise(page.id, vp.name, "baseline", "no baseline: captured");
        };
      } else {
        entry.baseline = "missing";
        advise(page.id, vp.name, "baseline", `no baseline (${BASELINE_DIR}/${key}.png): \`phasekit ui-check --update\` captures it`);
      }
    }
    let old = null;
    if (fs.existsSync(baseFile)) {
      try {
        old = decodePng(fs.readFileSync(baseFile));
      } catch (e) {
        if (mayRewrite) {
          rewrite(`the old baseline was unreadable: ${e.message}`);
        } else {
          entry.baseline = "unreadable";
          red(page.id, vp.name, "baseline-unreadable", `${BASELINE_DIR}/${key}.png: ${e.message} (declare the page and run --update to replace it)`);
        }
      }
    }
    if (old) {
      const d = diffImages(old, decodePng(shot), config.threshold.channel);
      entry.diff_share = d.share;
      if (d.image) {
        fs.writeFileSync(path.join(outDir, `${key}.diff.png`), encodePng(d.image));
        entry.diff = `${key}.diff.png`;
      }
      if (d.share <= config.threshold.share) {
        entry.baseline = "match";
      } else if (mayRewrite) {
        rewrite(`${(d.share * 100).toFixed(3)}% of pixels changed`);
      } else {
        entry.baseline = "differs";
        red(page.id, vp.name, "visual-diff",
          `${d.sizeChanged ? "the page size changed" : (d.share * 100).toFixed(3) + "% of pixels changed"} ` +
          `(threshold ${(config.threshold.share * 100).toFixed(3)}%): declare it under \`Visual changes:\` in the phase's ` +
          "docs/PHASES.md section and run `phasekit ui-check --update --phase P`");
      }
    }
    // axe
    await tab.addScriptTag({ content: tools.axeSource });
    // axe-core schedules its work on timers, so the paused clock is stepped
    // while it runs (at most 60 s of page time).
    await tab.evaluate(() => {
      window.__phasekitAxe = null;
      window.axe.run(document, {
        runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"] },
        resultTypes: ["violations", "incomplete"],
      }).then((r) => {
        const map = (v) => ({ id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.slice(0, 10).map((n) => n.target.join(" ")) });
        window.__phasekitAxe = { ok: r.violations.map(map), incomplete: r.incomplete.map(map) };
      },
      (e) => { window.__phasekitAxe = { error: String((e && e.message) || e) }; });
    });
    let axeResult = null;
    for (let t = 0; t <= 60000 && !(axeResult = await tab.evaluate(() => window.__phasekitAxe)); t += 50) {
      await tab.clock.runFor(50);
    }
    if (!axeResult || axeResult.error) throw new Error(`axe-core did not finish: ${axeResult ? axeResult.error : "60 s of page time"}`);
    const axe = axeResult.ok;
    entry.axe_incomplete = axeResult.incomplete;
    for (const v of axeResult.incomplete || []) {
      advise(page.id, vp.name, `axe-review:${v.id}`, `needs a human look: ${v.help} — ${v.nodes.join(", ")}`);
    }
    entry.axe = axe;
    for (const v of axe) {
      (BLOCKING_IMPACTS.has(v.impact) ? red : advise)(page.id, vp.name, `axe:${v.id}`, `${v.impact}: ${v.help} — ${v.nodes.join(", ")}`);
    }

    // tap targets
    const small = await tab.evaluate(measureTapTargets, config.tap.advise);
    entry.tap_targets = small;
    for (const t of small) {
      const what = `${t.tag}${t.id ? "#" + t.id : ""} "${t.name}" is ${t.width}×${t.height} px`;
      if (Math.min(t.width, t.height) < config.tap.block) red(page.id, vp.name, "tap-target", `${what} (minimum ${config.tap.block})`);
      else advise(page.id, vp.name, "tap-target", `${what} (recommended ${config.tap.advise})`);
    }

    // fonts
    const fontCheck = await renderedFonts(await ctx.newCDPSession(tab));
    entry.fonts = fontCheck.fonts;
    if (fontCheck.partial || fontCheck.failed) {
      advise(page.id, vp.name, "font-check-partial",
        `fonts read for ${fontCheck.partial ? "the first " + MAX_FONT_NODES + " text nodes only" : "every text node"}` +
        `${fontCheck.failed ? ", " + fontCheck.failed + " unreadable" : ""}`);
    }
    for (const f of fontCheck.fonts) {
      // A font the page loaded itself (@font-face) must be declared, whatever
      // family it reports; a system font must be the image's reference family
      // (or declared): anything else would make the baseline depend on what the
      // host happened to have.
      const ok = f.custom ? config.fonts.includes(f.family) : (REFERENCE_FONTS.includes(f.family) || config.fonts.includes(f.family));
      if (!ok) {
        red(page.id, vp.name, "font", `text rendered in ${f.custom ? "the page-loaded font" : "the system font"} '${f.family}', ` +
          `which is ${f.custom ? "not" : "neither the image's reference font nor"} declared in ${CONFIG_PATH} \`fonts\``);
      }
    }

    for (const msg of pageErrors.slice(Math.min(earlyErrors, 5), 10)) red(page.id, vp.name, "page-error", msg);
    if (pendingWrite && report.red.length === viewRedStart) pendingWrite();
    else if (pendingWrite) entry.baseline = `${entry.baseline} (not written: the view is RED)`;
    entry.ok = true;
  } finally {
    await ctx.close().catch(() => {});
  }
}

// --- `init`: seed the companion files ----------------------------------------

export const SEEDS = [
  ["templates/project-design-brief.template.md", "docs/project/DESIGN_BRIEF.md"],
  ["templates/project-design-system.template.md", "docs/project/DESIGN_SYSTEM.md"],
  ["templates/project-ui-pages.template.json", CONFIG_PATH],
];

export function init(project, engineRoot) {
  const lines = [];
  for (const [src, dst] of SEEDS) {
    const target = path.join(project, dst);
    if (fs.existsSync(target)) { lines.push(`kept: ${dst} (exists; never overwritten)`); continue; }
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.copyFileSync(path.join(engineRoot, src), target);
    lines.push(`seeded: ${dst}`);
  }
  return lines;
}

function summary(report) {
  if (report.status === "not-configured") return `ui-check: not configured — ${report.note}`;
  if (report.status === "config-error") return `ui-check: configuration error in ${CONFIG_PATH}:\n  ` + report.errors.join("\n  ");
  if (report.status === "tools-missing") return `ui-check: ${report.note}`;
  if (report.status === "timeout") return `ui-check: RED — ${report.note}`;
  const lines = [`ui-check: ${report.status.toUpperCase()} — ${report.pages.length} page view(s), ${report.red.length} red, ` +
    `${report.advisory.length} advisory; evidence ${report.evidence}/`];
  if (report.note) lines.push(`  RED ${report.note}`);
  for (const r of report.red) lines.push(`  RED ${r.page}@${r.viewport} ${r.rule}: ${r.detail}`);
  for (const a of report.advisory) lines.push(`  note ${a.page}@${a.viewport} ${a.rule}: ${typeof a.detail === "string" ? a.detail : JSON.stringify(a.detail)}`);
  return lines.join("\n");
}

async function main(argv) {
  const args = { project: process.cwd(), json: false, update: false, declared: [] };
  let verb = "check";
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "init" && verb === "check") verb = "init";
    else if (a === "--project") {
      if (i + 1 >= argv.length) { console.error("ui-check: --project needs a directory"); return 2; }
      args.project = argv[++i];
    }
    else if (a === "--json") args.json = true;
    else if (a === "--update") args.update = true;
    else if (a === "--declared-json") {
      // A JSON list of page ids, or the loop's `visual-changes` object (its `pages`).
      let d;
      try {
        if (i + 1 >= argv.length) throw new Error("missing");
        d = JSON.parse(argv[++i]);
      } catch {
        console.error("ui-check: --declared-json needs a JSON value");
        return 2;
      }
      const list = Array.isArray(d) ? d : d && typeof d === "object" && Array.isArray(d.pages) ? d.pages : null;
      if (!list || !list.every((x) => typeof x === "string" && DECLARED_RE.test(x))) {
        console.error("ui-check: --declared-json must be a list of page ids (id or id@viewport), or an object with one");
        return 2;
      }
      args.declared = list;
    } else {
      console.error(`ui-check: unknown argument ${a}`);
      return 2;
    }
  }
  if (verb === "init") {
    // The templates ship with an engine (pinned store, or a phasekit checkout),
    // not into a vendored project: fall back to the CLI's own install.
    const candidates = [path.resolve(path.dirname(fileURLToPath(import.meta.url)), ".."), process.env.PHASEKIT_SCAFFOLD_ROOT || ""];
    const engineRoot = candidates.find((c) => c && SEEDS.every(([src]) => fs.existsSync(path.join(c, src))));
    if (!engineRoot) {
      console.error("ui-check init: the templates were not found (run it with the installed phasekit CLI)");
      return 3;
    }
    for (const l of init(path.resolve(args.project), engineRoot)) console.log(`ui-check init: ${l}`);
    return 0;
  }
  const limit = Number(process.env.PHASEKIT_UI_CHECK_TIMEOUT || 120);
  if (!(limit > 0)) {
    console.error("ui-check: PHASEKIT_UI_CHECK_TIMEOUT must be a number of seconds > 0");
    return 2;
  }
  let timer;
  const timeout = new Promise((resolve) => {
    timer = setTimeout(() => resolve({ code: 1, report: { status: "timeout",
      note: `timed out after ${limit} s (PHASEKIT_UI_CHECK_TIMEOUT); no verdict, so RED` } }), limit * 1000);
  });
  const result = await Promise.race([run(args), timeout]).catch((e) => ({ code: 1,
    report: { status: "red", note: `ui-check failed: ${String(e && e.message || e).split("\n")[0]}`, red: [], advisory: [], pages: [] } }));
  clearTimeout(timer);
  if (args.json) console.log(JSON.stringify(result.report, null, 2));
  else console.log(result.report.note && result.report.status === "red" && !result.report.evidence ? `ui-check: RED — ${result.report.note}` : summary(result.report));
  return result.code;
}

if (process.argv[1] && import.meta.url === pathToFileURL(fs.realpathSync(process.argv[1])).href) {
  main(process.argv.slice(2)).then((code) => process.exit(code));
}
