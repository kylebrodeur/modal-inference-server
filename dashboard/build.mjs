// Build the dashboard: bundle src/main.ts -> dist/dashboard.js (IIFE, minified).
// Output is baked into the Modal image and served by the FastAPI bridge.
import { build } from "esbuild";
import { mkdirSync, cpSync } from "node:fs";

mkdirSync("dist", { recursive: true });

await build({
  entryPoints: ["src/main.ts"],
  bundle: true,
  format: "iife",
  target: "es2022",
  minify: true,
  outfile: "dist/dashboard.js",
  sourcemap: false,
  logLevel: "info",
});

// index.html shell lives beside this file so the FastAPI side can serve it verbatim.
cpSync("index.html", "dist/index.html");
console.log("dashboard build complete: dist/dashboard.js + dist/index.html");