import { build, context } from "esbuild";

const options = {
  entryPoints: ["src/powerpilot-panel.ts"],
  bundle: true,
  format: "esm",
  target: "es2021",
  minify: true,
  sourcemap: false,
  outfile: "../custom_components/powerpilot/frontend/powerpilot-panel.js",
  legalComments: "none",
};

if (process.argv.includes("--watch")) {
  const ctx = await context(options);
  await ctx.watch();
  console.log("watching…");
} else {
  await build(options);
  console.log("built", options.outfile);
}
