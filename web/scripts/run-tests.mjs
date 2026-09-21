import { mkdtempSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { build } from "esbuild";

// Every `tests/*.test.ts` is bundled (no DOM, node:test) and run in one process,
// so a new suite is picked up by dropping the file in — no list to maintain.
const sources = readdirSync("tests").filter((name) => name.endsWith(".test.ts")).sort();
if (sources.length === 0) {
  console.error("tests/: no *.test.ts files found");
  process.exit(1);
}

const out = mkdtempSync(join(tmpdir(), "qd-web-test-"));
await build({
  entryPoints: sources.map((name) => join("tests", name)),
  outdir: out,
  entryNames: "[name]",
  outExtension: { ".js": ".mjs" },
  bundle: true,
  format: "esm",
  platform: "node",
  logLevel: "warning",
  // The app is built by Vite with the automatic JSX runtime; without this the test
  // bundle emits `React.createElement` and a component under test cannot be called.
  jsx: "automatic",
});

const bundles = sources.map((name) => join(out, name.replace(/\.ts$/, ".mjs")));
const result = spawnSync(process.execPath, ["--test", ...bundles], { stdio: "inherit" });
rmSync(out, { recursive: true, force: true });
process.exit(result.status ?? 1);
