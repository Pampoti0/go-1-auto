import { mkdir, readFile, writeFile, copyFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import * as esbuild from "esbuild";

const require = createRequire(import.meta.url);
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const staticDir = join(root, "static");
const templateHtmlPath = join(root, "frontend", "index.template.html");
const appSourcePath = join(root, "frontend", "app.jsx");
const outHtmlPath = join(staticDir, "index.html");
const distDir = join(staticDir, "dist");
const vendorDir = join(staticDir, "vendor");
const cacheDir = join(root, ".cache", "frontend-build");

const vendorFiles = [
  ["react", "umd/react.production.min.js", "react.production.min.js"],
  ["react-dom", "umd/react-dom.production.min.js", "react-dom.production.min.js"],
  ["particles.js", "particles.js", "particles.js"],
  ["three", "build/three.min.js", "three.min.js"],
  ["three", "examples/js/loaders/GLTFLoader.js", "GLTFLoader.js"],
  ["three", "examples/js/controls/OrbitControls.js", "OrbitControls.js"],
  ["three", "examples/js/geometries/DecalGeometry.js", "DecalGeometry.js"],
];

function fail(message) {
  console.error(message);
  process.exit(1);
}

await mkdir(distDir, { recursive: true });
await mkdir(vendorDir, { recursive: true });
await mkdir(cacheDir, { recursive: true });

const appSource = await readFile(appSourcePath, "utf8");

const transformed = await esbuild.transform(appSource, {
  loader: "jsx",
  jsxFactory: "React.createElement",
  jsxFragment: "React.Fragment",
  target: "es2018",
  minify: true,
  legalComments: "none",
});
await writeFile(join(distDir, "app.js"), transformed.code, "utf8");

await writeFile(join(cacheDir, "tailwind.input.css"), "@tailwind base;\n@tailwind components;\n@tailwind utilities;\n", "utf8");
const tailwindBin = process.platform === "win32" ? "tailwindcss.cmd" : "tailwindcss";
const tailwind = spawnSync(
  join(root, "node_modules", ".bin", tailwindBin),
  ["-c", join(root, "tailwind.config.cjs"), "-i", join(cacheDir, "tailwind.input.css"), "-o", join(distDir, "tailwind.css"), "--minify"],
  { cwd: root, stdio: "inherit" },
);
if (tailwind.status !== 0) fail("Tailwind build failed");

function packageFile(pkg, rel) {
  return join(dirname(require.resolve(`${pkg}/package.json`)), rel);
}

for (const [pkg, rel, destName] of vendorFiles) {
  await copyFile(packageFile(pkg, rel), join(vendorDir, destName));
}

const templateHtml = await readFile(templateHtmlPath, "utf8");
for (const required of ["/static/dist/tailwind.css", "/static/dist/app.js", "/static/vendor/react.production.min.js"]) {
  if (!templateHtml.includes(required)) fail(`Template is missing ${required}`);
}

await writeFile(outHtmlPath, templateHtml, "utf8");
console.log("Built static/index.html from frontend/app.jsx and frontend/index.template.html.");
