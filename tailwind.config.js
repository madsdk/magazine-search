/** Tailwind v3 config — scans Jinja templates and emits only the utilities
 *  actually used. Output is committed to src/magsearch/web/static/tailwind.css
 *  so the desktop bundle works offline without a node build step. */
module.exports = {
  content: ["./src/magsearch/web/templates/**/*.html"],
  theme: { extend: {} },
  plugins: [],
};
