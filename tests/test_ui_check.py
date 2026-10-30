"""phasekit ui-check (v0.23.0): the deterministic UI checks.

Three tiers, by what the host has:
  * PURE — the PNG codec, the diff, the config validator and the declaration
    match, run through node against the shipped module's own exports (any host
    with node; the runner image has node 20).
  * CLI — `phasekit ui-check` in BOTH layouts (a vendored project, and a pinned
    one with a read-only engine): not configured, configuration errors, missing
    tools, `--phase` without `--update`, `init` never overwriting, and the
    `Visual changes:` declaration read by the loop's own plan parser.
  * BROWSER — the fixture set, each page failing exactly one RED rule plus a
    clean page, determinism across two runs, and the baseline update rule. Runs
    only where the image's pinned tools exist (PHASEKIT_UI_PREFIX, default
    /opt/phasekit-ui): scripts/verify-in-container.sh runs this module.
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

REPO_ROOT = Path(__file__).resolve().parent.parent
UI_CHECK = REPO_ROOT / "scripts" / "phasekit-ui-check.mjs"
CLI = REPO_ROOT / "scripts" / "phasekit.sh"
LOOP = REPO_ROOT / "scripts" / "run-until-done.sh"
DOCKERFILE = REPO_ROOT / ".devcontainer" / "Dockerfile"
NODE = shutil.which("node")
PREFIX = os.environ.get("PHASEKIT_UI_PREFIX", "/opt/phasekit-ui")
HAVE_TOOLS = (Path(PREFIX) / "node_modules" / "playwright-core").is_dir() and \
    (Path(PREFIX) / "node_modules" / "axe-core").is_dir()


def node_eval(body):
    """Run `body` (an async JS function body with `m` = the module) and return its JSON result."""
    script = ("import(%s).then(async (m) => { const r = await (async () => { %s })(); "
              "process.stdout.write(JSON.stringify(r)); })" % (json.dumps(UI_CHECK.as_uri()), body))
    r = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise AssertionError(f"node failed: {r.stderr}")
    return json.loads(r.stdout)


@unittest.skipUnless(NODE, "node is not on PATH")
class Pure(unittest.TestCase):
    def test_png_round_trips_and_the_diff_counts_changed_pixels(self):
        r = node_eval("""
          const w = 7, h = 5, data = Buffer.alloc(w * h * 4);
          for (let i = 0; i < w * h; i++) { data[i*4] = i * 3; data[i*4+1] = 255 - i; data[i*4+2] = (i * 37) & 255; data[i*4+3] = 255; }
          const png = m.encodePng({ width: w, height: h, data });
          const back = m.decodePng(png);
          const same = m.diffImages(back, { width: w, height: h, data });
          const moved = Buffer.from(data); moved[0] = (moved[0] + 100) & 255; moved[5*4+1] = (moved[5*4+1] + 17) & 255;
          const two = m.diffImages({ width: w, height: h, data }, { width: w, height: h, data: moved }, 16);
          const size = m.diffImages({ width: w, height: h, data }, { width: w, height: h - 1, data: data.subarray(0, w * (h-1) * 4) });
          return { equal: back.data.equals(data), w: back.width, h: back.height, same: same.changed,
                   two: two.changed, share: two.share, size: [size.share, size.sizeChanged], diffPng: m.decodePng(m.encodePng(two.image)).width };
        """)
        self.assertTrue(r["equal"])
        self.assertEqual((r["w"], r["h"]), (7, 5))
        self.assertEqual(r["same"], 0)
        self.assertEqual(r["two"], 2)
        self.assertAlmostEqual(r["share"], 2 / 35)
        self.assertEqual(r["size"], [1, True])
        self.assertEqual(r["diffPng"], 7)

    def test_the_decoder_handles_every_filter_type_and_rgb(self):
        # Hand-built PNG: 3x5 RGB, rows filtered 0..4, so every unfilter path runs.
        r = node_eval("""
          const zlib = await import('node:zlib');
          const w = 3, h = 5, bpp = 3, stride = w * bpp;
          const px = []; for (let i = 0; i < w * h * 3; i++) px.push((i * 29 + 11) & 255);
          const raw = []; let prev = new Array(stride).fill(0);
          for (let y = 0; y < h; y++) {
            const line = px.slice(y * stride, (y + 1) * stride), ft = y; raw.push(ft);
            for (let i = 0; i < stride; i++) {
              const a = i >= bpp ? line[i - bpp] : 0, b = prev[i], c = i >= bpp ? prev[i - bpp] : 0;
              let p = 0;
              if (ft === 1) p = a; else if (ft === 2) p = b; else if (ft === 3) p = (a + b) >> 1;
              else if (ft === 4) { const q = a + b - c, pa = Math.abs(q - a), pb = Math.abs(q - b), pc = Math.abs(q - c); p = pa <= pb && pa <= pc ? a : pb <= pc ? b : c; }
              raw.push((line[i] - p) & 255);
            }
            prev = line;
          }
          const crcT = []; for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; crcT[n] = c >>> 0; }
          const crc = (b) => { let c = 0xffffffff; for (const x of b) c = crcT[(c ^ x) & 255] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0; };
          const chunk = (t, d) => { const l = Buffer.alloc(4); l.writeUInt32BE(d.length); const td = Buffer.concat([Buffer.from(t), d]); const c = Buffer.alloc(4); c.writeUInt32BE(crc(td)); return Buffer.concat([l, td, c]); };
          const ihdr = Buffer.alloc(13); ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4); ihdr[8] = 8; ihdr[9] = 2;
          const png = Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]), chunk('IHDR', ihdr), chunk('IDAT', zlib.deflateSync(Buffer.from(raw))), chunk('IEND', Buffer.alloc(0))]);
          const d = m.decodePng(png);
          const got = []; for (let i = 0; i < w * h; i++) got.push(d.data[i*4], d.data[i*4+1], d.data[i*4+2]);
          return { ok: JSON.stringify(got) === JSON.stringify(px), alpha: d.data[3] };
        """)
        self.assertTrue(r["ok"])
        self.assertEqual(r["alpha"], 255)

    def test_the_config_validator_holds_the_standard(self):
        r = node_eval("""
          const ok = m.loadConfig(JSON.stringify({ pages: [{ id: 'start', path: '/' }] }));
          const errs = (c) => (m.loadConfig(JSON.stringify(c)).errors || []).join(' | ');
          return {
            ok: ok.config.pages[0].viewports.map((v) => v.name), tap: ok.config.tap, thr: ok.config.threshold,
            noPages: errs({}),
            looseTap: errs({ tap: { block: 12 }, pages: [{ id: 'a', path: '/' }] }),
            badVp: errs({ pages: [{ id: 'a', path: '/', viewports: ['tablet'] }] }),
            badOp: errs({ pages: [{ id: 'a', path: '/', setup: [{ hover: 'x' }] }] }),
            longWait: errs({ pages: [{ id: 'a', path: '/', setup: [{ wait: 60000 }] }] }),
            looseShare: errs({ threshold: { share: 1 }, pages: [{ id: 'a', path: '/' }] }),
            looseChannel: errs({ threshold: { channel: 255 }, pages: [{ id: 'a', path: '/' }] }),
            tight: m.loadConfig(JSON.stringify({ threshold: { share: 0, channel: 0 }, pages: [{ id: 'a', path: '/' }] })).errors || null,
            dup: errs({ pages: [{ id: 'a', path: '/' }, { id: 'a', path: '/b' }] }),
            escape: errs({ root: '../x', pages: [{ id: 'a', path: '/' }] }),
            relPath: errs({ pages: [{ id: 'a', path: 'x.html' }] }),
            dark: m.loadConfig(JSON.stringify({ pages: [{ id: 'a', path: '/', viewports: ['phone-dark'] }] })).config.pages[0].viewports[0],
          };
        """)
        self.assertEqual(r["ok"], ["phone", "desktop"])
        self.assertEqual(r["tap"], {"block": 24, "advise": 44})
        self.assertEqual(r["thr"], {"channel": 16, "share": 0.001})
        self.assertIn("`pages` must be a non-empty list", r["noPages"])
        self.assertIn("tap.block may not be below 24", r["looseTap"])
        self.assertIn("viewports", r["badVp"])
        self.assertIn("setup[0] must be one of", r["badOp"])
        self.assertIn("wait must be", r["longWait"])
        self.assertIn("may tighten it, not loosen it", r["looseShare"])
        self.assertIn("may tighten it, not loosen it", r["looseChannel"])
        self.assertIsNone(r["tight"])
        self.assertIn("duplicate", r["dup"])
        self.assertIn("inside the project", r["escape"])
        self.assertIn("must start with /", r["relPath"])
        self.assertEqual((r["dark"]["width"], r["dark"]["colorScheme"]), (390, "dark"))

    def test_a_declaration_covers_a_page_or_one_viewport_of_it(self):
        r = node_eval("""
          const d = ['summary', 'stats@phone'];
          return [m.isDeclared(d, 'summary', 'desktop'), m.isDeclared(d, 'stats', 'phone'),
                  m.isDeclared(d, 'stats', 'desktop'), m.isDeclared(d, 'start', 'phone')];
        """)
        self.assertEqual(r, [True, True, False, False])


class Image(unittest.TestCase):
    """The tools reach the image pinned, never floating (the runner-image lesson)."""

    def test_the_ui_tools_and_font_are_pinned_and_asserted(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertRegex(text, r"ARG AXE_CORE_VERSION=\d+\.\d+\.\d+\n")
        self.assertRegex(text, r"ARG UI_FONT_SHA256=[0-9a-f]{64}\n")
        self.assertIn('"playwright-core@${PLAYWRIGHT_CORE_VERSION}" "axe-core@${AXE_CORE_VERSION}"', text)
        self.assertIn("axe-core/package.json').version\")\" = \"${AXE_CORE_VERSION}\"", text)
        self.assertIn("playwright-core/package.json').version\")\" = \"${PLAYWRIGHT_CORE_VERSION}\"", text)
        self.assertIn('sha256sum -c -', text)
        for fam in ("system-ui", "sans-serif", "monospace", "ui-monospace"):
            self.assertIn(f"<alias><family>{fam}</family>", text)


class _Project:
    """A throwaway git project in one layout. Vendored: the engine's scripts in
    its own tree. Pinned: only a pin, the engine a READ-ONLY copy elsewhere."""

    ENGINE_FILES = ("scripts/run-until-done.sh", "scripts/phasekit-ui-check.mjs", "scripts/phasekit.sh",
                    "templates/project-design-brief.template.md",
                    "templates/project-design-system.template.md", "templates/project-ui-pages.template.json")

    def __init__(self, layout):
        self.layout = layout
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-ui-"))
        self.root = self.tmp / "project"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        if layout == "vendored":
            self.engine = self.root
            for rel in ("scripts/run-until-done.sh", "scripts/phasekit-ui-check.mjs"):
                (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(REPO_ROOT / rel, self.root / rel)
        else:
            self.engine = self.tmp / "engine"
            for rel in self.ENGINE_FILES:
                (self.engine / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(REPO_ROOT / rel, self.engine / rel)
            for p in sorted(self.engine.rglob("*"), reverse=True):
                p.chmod(p.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
            self.engine.chmod(0o555)
            (self.root / ".phasekit-version").write_text("v0.23.0\n")

    def cli(self, *args, env=None):
        e = dict(os.environ)
        e.pop("PHASEKIT_ENGINE_DIR", None)
        if self.layout == "pinned":
            e["PHASEKIT_ENGINE_DIR"] = str(self.engine)
        e.update(env or {})
        cli = self.engine / "scripts" / "phasekit.sh" if self.layout == "pinned" else CLI
        return subprocess.run(["bash", str(cli), "ui-check", *args], cwd=self.root, env=e,
                              capture_output=True, text=True, timeout=600)

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def close(self):
        for p in [self.tmp, *self.tmp.rglob("*")]:
            try:
                p.chmod(p.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)


@unittest.skipUnless(NODE, "node is not on PATH")
class Cli(unittest.TestCase):
    def each(self):
        for layout in ("vendored", "pinned"):
            p = _Project(layout)
            self.addCleanup(p.close)
            yield p

    def test_not_configured_is_green_and_says_so(self):
        for p in self.each():
            r = p.cli()
            self.assertEqual(r.returncode, 0, (p.layout, r.stdout, r.stderr))
            self.assertIn("ui-check: not configured", r.stdout)

    def test_a_bad_config_is_exit_2_with_every_problem(self):
        for p in self.each():
            p.write("docs/project/ui-pages.json", json.dumps({"tap": {"block": 10}, "pages": [{"id": "a b", "path": "x"}]}))
            r = p.cli()
            self.assertEqual(r.returncode, 2, (p.layout, r.stdout, r.stderr))
            self.assertIn("tap.block may not be below 24", r.stdout)
            self.assertIn("pages[0].id", r.stdout)

    def test_missing_tools_is_exit_3_naming_the_runner_image(self):
        for p in self.each():
            p.write("docs/project/ui-pages.json", json.dumps({"pages": [{"id": "a", "path": "/"}]}))
            r = p.cli(env={"PHASEKIT_UI_PREFIX": str(p.tmp / "nowhere")})
            self.assertEqual(r.returncode, 3, (p.layout, r.stdout, r.stderr))
            self.assertIn("runs in the runner image", r.stdout)

    def test_phase_without_update_is_a_usage_error(self):
        for p in self.each():
            r = p.cli("--phase", "3")
            self.assertEqual(r.returncode, 2, (p.layout, r.stdout, r.stderr))

    def test_a_bad_timeout_is_a_usage_error(self):
        for p in self.each():
            p.write("docs/project/ui-pages.json", json.dumps({"pages": [{"id": "a", "path": "/"}]}))
            r = p.cli(env={"PHASEKIT_UI_CHECK_TIMEOUT": "soon"})
            self.assertEqual(r.returncode, 2, (p.layout, r.stdout, r.stderr))

    def test_init_seeds_the_three_files_and_never_overwrites(self):
        for p in self.each():
            p.write("docs/project/DESIGN_BRIEF.md", "# mine\n")
            r = p.cli("init")
            self.assertEqual(r.returncode, 0, (p.layout, r.stdout, r.stderr))
            self.assertEqual((p.root / "docs/project/DESIGN_BRIEF.md").read_text(), "# mine\n")
            self.assertIn("kept: docs/project/DESIGN_BRIEF.md", r.stdout)
            self.assertTrue((p.root / "docs/project/DESIGN_SYSTEM.md").is_file())
            cfg = json.loads((p.root / "docs/project/ui-pages.json").read_text())
            self.assertEqual(cfg["tap"], {"block": 24, "advise": 44})

    def test_the_seeded_ui_pages_is_a_valid_config(self):
        r = node_eval("""
          const fs = await import('node:fs');
          return m.loadConfig(fs.readFileSync(%s, 'utf8'));
        """ % json.dumps(str(REPO_ROOT / "templates" / "project-ui-pages.template.json")))
        self.assertNotIn("errors", r)

    def test_update_reads_the_phase_declaration_through_the_loop(self):
        # The CLI passes the loop parser's JSON to the check: prove the wiring by
        # pointing the tools at nowhere (exit 3 comes after the declaration was read)
        # and reading the declaration verb directly.
        for p in self.each():
            p.write("docs/PHASES.md", textwrap.dedent("""\
                # Phases
                ## Phase 7 — Redesign the summary
                Planned paths: src/**
                Visual changes: summary, `stats@phone`
                ```
                Visual changes: fenced
                ```
                ## Phase 8 — Nothing visual
                Visual changes: none
                """))
            e = dict(os.environ, PHASEKIT_PROJECT_DIR=str(p.root))
            out = {}
            for ph in ("7", "8", "9"):
                r = subprocess.run(["bash", str(p.engine / "scripts" / "run-until-done.sh"), "visual-changes", ph],
                                   cwd=p.root, env=e, capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 0, r.stderr)
                out[ph] = json.loads(r.stdout)
            self.assertEqual(out["7"]["pages"], ["summary", "stats@phone"])
            self.assertTrue(out["8"]["declared"])
            self.assertEqual(out["8"]["pages"], [])
            self.assertFalse(out["9"]["found"])
            p.write("docs/project/ui-pages.json", json.dumps({"pages": [
                {"id": "summary", "path": "/"}, {"id": "stats", "path": "/s"}]}))
            r = p.cli("--update", "--phase", "7", env={"PHASEKIT_UI_PREFIX": str(p.tmp / "nowhere")})
            self.assertEqual(r.returncode, 3, (p.layout, r.stdout, r.stderr))

    def test_prose_on_the_declaration_line_refuses_it_whole(self):
        # Review r1: "none; the summary and stats pages stay" once declared every word.
        for p in self.each():
            p.write("docs/PHASES.md", textwrap.dedent("""\
                ## Phase 3 — Copy only
                Visual changes: none; the summary and stats pages stay as they are
                ## Phase 4 — Header
                Visual changes: summary (only the header), not stats@phone
                """))
            p.write("docs/project/ui-pages.json", json.dumps({"pages": [{"id": "summary", "path": "/"}]}))
            for ph in ("3", "4"):
                r = p.cli("--update", "--phase", ph)
                self.assertEqual(r.returncode, 2, (p.layout, ph, r.stdout, r.stderr))
                self.assertIn("not page ids", r.stderr)
            r = p.cli("--update", "--phase", "99")
            self.assertEqual(r.returncode, 2, (p.layout, r.stderr))
            self.assertIn("no heading for phase 99", r.stderr)

    def test_words_that_name_no_configured_page_are_refused(self):
        # Review r2: "Visual changes: the home button moves on settings" declared home and settings.
        for p in self.each():
            p.write("docs/PHASES.md", "## Phase 6 — Words\nVisual changes: the home button moves on settings\n")
            p.write("docs/project/ui-pages.json", json.dumps({"pages": [
                {"id": "home", "path": "/"}, {"id": "settings", "path": "/s", "viewports": ["phone"]}]}))
            r = p.cli("--update", "--phase", "6")
            self.assertEqual(r.returncode, 2, (p.layout, r.stdout, r.stderr))
            self.assertIn("does not have: the, button, moves, on", r.stdout)
            p.write("docs/PHASES.md", "## Phase 7 — Desktop settings\nVisual changes: settings@desktop\n")
            r = p.cli("--update", "--phase", "7")
            self.assertEqual(r.returncode, 2, (p.layout, r.stdout, r.stderr))
            self.assertIn("settings@desktop", r.stdout)

    def test_the_cli_refuses_a_hand_passed_declaration(self):
        for p in self.each():
            r = p.cli("--update", "--declared-json", '["home"]')
            self.assertEqual(r.returncode, 2, (p.layout, r.stdout, r.stderr))
            self.assertIn("internal", r.stderr)

    def test_bad_declared_json_and_project_arguments_are_usage_errors(self):
        for args in (["--declared-json"], ["--declared-json", '{"x": 1}'], ["--declared-json", '["a b"]'],
                     ["--declared-json", "nope"], ["--project"]):
            r = subprocess.run([NODE, str(UI_CHECK), *args], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2, (args, r.stdout, r.stderr))

    def test_a_root_that_resolves_outside_the_project_is_refused(self):
        p = _Project("vendored")
        self.addCleanup(p.close)
        (p.root / "site").symlink_to(p.tmp)
        p.write("docs/project/ui-pages.json", json.dumps({"root": "site", "pages": [{"id": "a", "path": "/"}]}))
        r = p.cli()
        self.assertEqual(r.returncode, 2, (r.stdout, r.stderr))
        self.assertIn("resolves outside the project", r.stdout)


# --- the browser tier ---------------------------------------------------------

CLEAN_CSS = "body{margin:0;font:16px system-ui;background:#fff;color:#111}main{padding:24px}" \
            "button{min-width:48px;min-height:48px;font:16px system-ui;background:#0b5;color:#000;border:0}"
PAGES = {
    "clean": '<main><h1>Clean</h1><p>Plain text.</p><button type="button">Start</button></main>',
    "contrast": '<main><h1>Contrast</h1><p style="color:#bbb">This text is far too light.</p></main>',
    "unlabeled": '<main><h1>Unlabeled</h1><button type="button" style="width:48px;height:48px"></button></main>',
    "tiny": '<main><h1>Tiny</h1><button type="button" style="min-width:0;min-height:0;width:20px;height:20px;padding:0">x</button></main>',
    "font": '<main><h1 style="font-family:Fancy">Fancy font</h1></main>',
    # review r1 fixtures, each failing exactly one rule
    "crash": '<main><h1>Crash</h1></main><script>document.querySelector("main").remove(); throw new Error("boom")</script>',
    "missing": '<main><h1>Missing script</h1></main><script src="/missing-app.js"></script>',
    "external": '<main><h1>External</h1><img alt="logo" src="http://198.51.100.7/logo.png" width="10" height="10"></main>',
    "navlinks": '<main><h1>Nav</h1><nav><a href="#a" style="font-size:9px">A</a> · <a href="#b" style="font-size:9px">B</a></nav></main>',
    "socket": '<main><h1>Socket</h1></main><script>new WebSocket("ws://198.51.100.7/feed")</script>',
}
# Not in the every-fixture run: pages that are clean but stress determinism and CSP.
EXTRA = {
    "timer": '<main><h1>Timer</h1><div id="bar" style="height:20px;width:0;background:#0b5"></div><p id="n">0</p></main>'
             '<script>let i=0;setInterval(()=>{i++;document.getElementById("bar").style.width=(i*7%300)+"px";'
             'document.getElementById("n").textContent=String(i)+" "+(performance.now()-t0).toFixed(1)+" "+crypto.randomUUID()},37);'
             'const t0=performance.now()</script>',
    "inline": '<main><h1>Inline</h1><p>Read the <a href="#t" style="font-size:9px">terms</a> before you play.</p></main>',
}


def page_html(name, css=CLEAN_CSS, csp=False):
    body = PAGES.get(name) or EXTRA[name]
    meta = '<meta http-equiv="Content-Security-Policy" content="script-src \'self\'">' if csp else ""
    font_face = ""
    if name == "font":
        font_face = "@font-face{font-family:Fancy;src:url(/fancy.ttf)}"
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{name}</title>'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">{meta}'
            f'<style>{font_face}{css}</style></head><body>{body}</body></html>')


@unittest.skipUnless(NODE and HAVE_TOOLS, "the runner image's UI tools are not here (in-container only)")
class Browser(unittest.TestCase):
    def setUp(self):
        self.p = _Project("vendored")
        self.addCleanup(self.p.close)
        for name in (*PAGES, *EXTRA):
            self.p.write(f"site/{name}.html", page_html(name))
        self.p.write("site/csp.html", page_html("clean", csp=True))
        shutil.copy(Path(PREFIX) / "fonts" / "DejaVuSans.ttf", self.p.root / "site" / "fancy.ttf")

    def config(self, names, viewports=("phone",)):
        self.p.write("docs/project/ui-pages.json", json.dumps({
            "root": "site", "pages": [{"id": n, "path": f"/{n}.html", "viewports": list(viewports)} for n in names]}))

    def check(self, *args):
        r = self.p.cli("--json", *args)
        try:
            rep = json.loads(r.stdout)
        except json.JSONDecodeError:
            self.fail(f"no JSON report: {r.stdout} {r.stderr}")
        return r.returncode, rep

    def rules(self, rep, page):
        return sorted({x["rule"].split(":")[0] for x in rep["red"] if x["page"] == page})

    def test_each_fixture_fails_exactly_its_rule_and_the_clean_page_passes(self):
        self.config(list(PAGES))
        code, rep = self.check("--update")  # first capture: baselines are advisory, then written
        self.assertEqual(code, 1, rep)
        self.assertEqual(self.rules(rep, "clean"), [])
        self.assertEqual(self.rules(rep, "contrast"), ["axe"])
        self.assertIn("axe:color-contrast", [x["rule"] for x in rep["red"] if x["page"] == "contrast"])
        self.assertEqual(self.rules(rep, "unlabeled"), ["axe"])
        self.assertIn("axe:button-name", [x["rule"] for x in rep["red"] if x["page"] == "unlabeled"])
        self.assertEqual(self.rules(rep, "tiny"), ["tap-target"])
        self.assertEqual(self.rules(rep, "font"), ["font"])
        self.assertEqual(self.rules(rep, "crash"), ["page-error"])
        self.assertEqual(self.rules(rep, "missing"), ["request-failed"])
        self.assertEqual(self.rules(rep, "external"), ["external-request"])
        self.assertIn("http://198.51.100.7/logo.png", rep["blocked"])
        self.assertIn("tap-target", self.rules(rep, "navlinks"))  # axe's target-size may also fire
        self.assertEqual(self.rules(rep, "socket"), ["external-request"])
        # a view that went RED never leaves a captured baseline behind
        self.assertFalse((self.p.root / "tests/ui-baselines/contrast@phone.png").exists())
        self.assertTrue((self.p.root / "tests/ui-baselines/clean@phone.png").is_file())

    def test_two_runs_on_the_same_tree_take_identical_screenshots(self):
        self.config(["clean"], viewports=("phone", "desktop", "phone-dark"))
        code1, rep1 = self.check("--update")
        code2, rep2 = self.check()
        self.assertEqual((code1, code2), (0, 0), (rep1, rep2))
        self.assertEqual([p["screenshot_sha256"] for p in rep1["pages"]], [p["screenshot_sha256"] for p in rep2["pages"]])
        self.assertTrue(all(p["baseline"] == "match" for p in rep2["pages"]), rep2)
        self.assertIn("clean.html", rep2["fetched"])

    def test_an_undeclared_change_is_red_and_the_declared_update_is_green(self):
        self.config(["clean"])
        self.assertEqual(self.check("--update")[0], 0)
        self.p.write("site/clean.html", page_html("clean", CLEAN_CSS.replace("padding:24px", "padding:64px")))
        code, rep = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(self.rules(rep, "clean"), ["visual-diff"])
        code, rep = self.check("--update")  # --update without a declaration may not rewrite it
        self.assertEqual((code, self.rules(rep, "clean")), (1, ["visual-diff"]))
        self.p.write("docs/PHASES.md", "## Phase 2 — Roomier\nVisual changes: clean\n")
        code, rep = self.check("--update", "--phase", "2")
        self.assertEqual(code, 0, rep)
        self.assertEqual(rep["pages"][0]["baseline"], "updated")
        self.assertEqual(self.check()[0], 0)

    def test_a_page_loaded_font_passes_once_declared(self):
        self.p.write("docs/project/ui-pages.json", json.dumps({
            "root": "site", "fonts": ["DejaVu Sans"],
            "pages": [{"id": "font", "path": "/font.html", "viewports": ["phone"]}]}))
        code, rep = self.check("--update")
        self.assertEqual(code, 0, rep)
        self.assertTrue(any(f["custom"] for f in rep["pages"][0]["fonts"]), rep["pages"][0]["fonts"])

    def test_a_timer_driven_page_is_identical_across_runs(self):
        # Review r1: Date-only freezing let intervals, performance.now and crypto vary.
        self.config(["timer", "inline"])
        code1, rep1 = self.check("--update")
        code2, rep2 = self.check()
        self.assertEqual((code1, code2), (0, 0), (rep1["red"], rep2["red"]))
        self.assertEqual([p["screenshot_sha256"] for p in rep1["pages"]], [p["screenshot_sha256"] for p in rep2["pages"]])
        # the inline-link exception still holds for a link inside running text
        self.assertEqual(rep2["pages"][1]["tap_targets"], [])

    def test_a_strict_csp_page_is_checked_not_crashed(self):
        self.p.write("docs/project/ui-pages.json", json.dumps({
            "root": "site", "pages": [{"id": "csp", "path": "/csp.html", "viewports": ["phone"]}]}))
        code, rep = self.check("--update")
        self.assertEqual(code, 0, rep)
        self.assertTrue(rep["pages"][0]["ok"])

    def test_an_unreadable_baseline_is_red_and_a_declared_update_replaces_it(self):
        self.config(["clean"])
        (self.p.root / "tests/ui-baselines").mkdir(parents=True)
        (self.p.root / "tests/ui-baselines/clean@phone.png").write_bytes(b"\x89PNG\r\n\x1a\nnot really")
        code, rep = self.check()
        self.assertEqual((code, self.rules(rep, "clean")), (1, ["baseline-unreadable"]))
        self.assertTrue((self.p.root / rep["evidence"] / "report.json").is_file())
        self.p.write("docs/PHASES.md", "## Phase 5 — Fix the baseline\nVisual changes: clean@phone\n")
        code, rep = self.check("--update", "--phase", "5")
        self.assertEqual(code, 0, rep)
        self.assertEqual(self.check()[0], 0)

    def test_a_setup_eval_that_waits_on_a_timer_finishes(self):
        # Review r2: the paused clock never fired the eval's timer.
        self.p.write("docs/project/ui-pages.json", json.dumps({"root": "site", "pages": [{
            "id": "clean", "path": "/clean.html", "viewports": ["phone"],
            "setup": [{"eval": "new Promise((r) => setTimeout(r, 2000))"}]}]}))
        code, rep = self.check("--update")
        self.assertEqual(code, 0, rep)

    def test_a_deleted_tracked_baseline_is_red(self):
        self.config(["clean"])
        self.assertEqual(self.check("--update")[0], 0)
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(self.p.root)]
        subprocess.run([*git, "add", "tests/ui-baselines"], check=True)
        subprocess.run([*git, "commit", "-qm", "baselines"], check=True)
        (self.p.root / "tests/ui-baselines/clean@phone.png").unlink()
        code, rep = self.check()
        self.assertEqual((code, self.rules(rep, "clean")), (1, ["baseline-deleted"]))
