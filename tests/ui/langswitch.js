// Switching the language and back leaves the window as it was: every view
// and Settings tab reads the same in English after a round trip through
// Chinese, and through System, as before it; in Chinese nothing is left from
// the English it switched from; and nothing breaks on the way.
const R = [];
const t = (name, fn) => { try { fn(); R.push("PASS " + name); } catch (e) { R.push("FAIL " + name + ": " + e.message); } };
ui.images.push({ Id: "sha256:" + "ab".repeat(32), RepoTags: ["nginx:alpine"], Size: 5e7, Created: Date.now() / 1000 - 86400 });
const pick = (lang) => { show("settings"); document.querySelector('[data-stab="appearance"]').click(); document.querySelector(`[data-lang-pick="${lang}"]`).click(); };
// What a person reads: the text and the words in attributes, of the whole window.
const words = () => {
  const c = document.body.cloneNode(true);
  c.querySelectorAll("script, #results, .toasts").forEach((x) => x.remove());
  const attrs = [...c.querySelectorAll("[title], [aria-label], [placeholder]")].map((el) => [el.title, el.getAttribute("aria-label"), el.getAttribute("placeholder")].join("|"));
  return c.textContent.replace(/\s+/g, " ") + "\n" + attrs.join("\n");
};
const PLACES = [["containers"], ["images"], ["volumes"], ["networks"], ["builds"], ["share"], ["image", () => { show("images"); openImage("nginx:alpine"); }]];
for (const id of ["appearance", "resources", "network", "builds", "kubernetes", "import", "domains", "diagnostics", "about"])
  PLACES.push([`settings ${id}`, () => { show("settings"); document.querySelector(`[data-stab="${id}"]`).click(); }]);
const visit = ([name, go]) => (go ? go() : show(name));
const before = new Map(PLACES.map((p) => { visit(p); return [p[0], words()]; }));
for (const round of ["zh-Hans", "system", "zh-Hans", "en", "zh-Hans"]) {
  pick(round);
  for (const p of PLACES) {
    visit(p);
    // In Chinese, nothing kept from the English it switched from.
    if (round === "zh-Hans") { const left = english(document.body); if (left.length) R.push(`FAIL ${p[0]} in Chinese (switched to, round ${R.length}): English left: ${left.slice(0, 5).map((x) => JSON.stringify(x)).join(", ")}`); }
  }
}
pick("en");
for (const p of PLACES) t(`${p[0]} reads as before after the round trip`, () => {
  visit(p);
  const now = words(), was = before.get(p[0]);
  if (now !== was) {
    let i = 0; while (i < now.length && now[i] === was[i]) i++;
    throw new Error(`differs at …${JSON.stringify(was.slice(Math.max(0, i - 30), i + 50))} → ${JSON.stringify(now.slice(Math.max(0, i - 30), i + 50))}`);
  }
});
t("System picks English for an English system", () => {
  pick("system");
  if (document.documentElement.lang !== "en") throw new Error("lang is " + document.documentElement.lang);
  if (!sent.some((m) => m.type === "setpref" && m.name === "lang" && m.value === "system")) throw new Error("System wasn't saved");
});
report(R);
