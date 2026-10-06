// Regenerate a scene's SVGs and 2x WebPs: node docs/render-diagram.mjs [pipeline|standalone] [--png DIR]   (needs network and cwebp: brew install webp; temp-installs Playwright if missing)
//
// Loads Excalidraw in headless Chromium and calls its own exportToSvg, once light and once
// with exportWithDarkMode. Fonts are inlined as data: URLs so GitHub can render the SVG in the README.
// The lossless WebPs are the README's click-to-open full-size targets (GitHub's file view shows them
// full width; an opened SVG is small and loses its embedded font to raw.githubusercontent.com's CSP).
// --png DIR also writes the 2x PNG screenshots there (for checking the result; not committed).
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const EXCALIDRAW = "0.18.1";
const PLAYWRIGHT = "1.62.1";
const here = dirname(fileURLToPath(import.meta.url));
const sceneName = process.argv[2] && !process.argv[2].startsWith("--") ? process.argv[2] : "pipeline";
const scene = JSON.parse(readFileSync(join(here, `${sceneName}.excalidraw`), "utf8"));
const pngIdx = process.argv.indexOf("--png");
const pngDir = pngIdx > 0 ? process.argv[pngIdx + 1] : null;
execFileSync("cwebp", ["-version"], { stdio: "ignore" }); // fail early: brew install webp

async function loadPlaywright() {
  try {
    return await import("playwright");
  } catch {
    const dir = join(tmpdir(), `excalidraw-render-pw-${PLAYWRIGHT}`);
    if (!existsSync(join(dir, "node_modules", "playwright"))) {
      mkdirSync(dir, { recursive: true });
      execFileSync("npm", ["install", "--prefix", dir, "--no-save", `playwright@${PLAYWRIGHT}`], { stdio: "inherit" });
      execFileSync(join(dir, "node_modules", ".bin", "playwright"), ["install", "chromium"], { stdio: "inherit" });
    }
    return createRequire(join(dir, "package.json"))("playwright");
  }
}

const { chromium } = await loadPlaywright();
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ deviceScaleFactor: 2 });
  await page.setContent(
    `<html><body style="margin:0"><script>window.EXCALIDRAW_ASSET_PATH="https://unpkg.com/@excalidraw/excalidraw@${EXCALIDRAW}/dist/prod/";</script></body></html>`,
  );
  for (const dark of [false, true]) {
    const svg = await page.evaluate(
      async ({ scene, dark, version }) => {
        const EX = await import(`https://esm.sh/@excalidraw/excalidraw@${version}`);
        const elements = EX.restoreElements(scene.elements, null);
        const node = await EX.exportToSvg({
          elements,
          appState: {
            exportBackground: true,
            viewBackgroundColor: scene.appState?.viewBackgroundColor || "#ffffff",
            exportPadding: 32,
            exportWithDarkMode: dark,
            exportScale: 1,
          },
          files: scene.files || null,
        });
        return node.outerHTML;
      },
      { scene, dark, version: EXCALIDRAW },
    );
    const name = `${sceneName}-${dark ? "dark" : "light"}`;
    writeFileSync(join(here, `${name}.svg`), svg);
    console.log(`${name}.svg ${(Buffer.byteLength(svg) / 1024).toFixed(0)} KB`);
    {
      const shotDir = pngDir || tmpdir();
      mkdirSync(shotDir, { recursive: true });
      const png = join(shotDir, `${name}.png`);
      await page.setContent(`<html><body style="margin:0">${svg}</body></html>`);
      await page.locator("svg").first().screenshot({ path: png });
      execFileSync("cwebp", ["-quiet", "-lossless", "-z", "9", png, "-o", join(here, `${name}.webp`)]);
      console.log(`${name}.webp`);
      await page.setContent(
        `<html><body style="margin:0"><script>window.EXCALIDRAW_ASSET_PATH="https://unpkg.com/@excalidraw/excalidraw@${EXCALIDRAW}/dist/prod/";</script></body></html>`,
      );
    }
  }
} finally {
  await browser.close();
}
