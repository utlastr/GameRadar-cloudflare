const fs = require("fs");
const path = require("path");
const { chromium } = require("D:/Users/KLEI/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright");
let connectedBrowser;

(async () => {
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  connectedBrowser = browser;
  let page;
  for (const context of browser.contexts()) {
    for (const candidate of context.pages()) {
      let session;
      try {
        session = await context.newCDPSession(candidate);
        const { targetInfo } = await session.send("Target.getTargetInfo");
        if (targetInfo.targetId === process.argv[2]) page = candidate;
      } catch {
        // Other tasks can close their pages during target enumeration.
      } finally {
        if (session) await session.detach().catch(() => {});
      }
    }
  }
  if (!page) throw new Error("Managed task target is missing");
  const errors = [];
  page.on("pageerror", err => errors.push(err.message));
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => typeof S !== "undefined" && S.cards?.length > 0);
  const live = await page.evaluate(() => ({
    sort: MUST.sort, cards: S.cards.length, version: S.revenue_meta?.version,
    text: document.querySelector("#content")?.innerText.slice(0, 1200)
  }));
  if (live.sort !== "revenue") throw new Error("Default is not revenue");
  const output = path.resolve("output/revenue-qa");
  fs.mkdirSync(output, { recursive: true });
  for (const [name, width, height] of [["desktop", 1440, 960], ["mobile", 390, 844]]) {
    await page.setViewportSize({ width, height });
    await page.screenshot({ path: path.join(output, `${name}.png`), fullPage: false });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2);
    if (overflow) throw new Error(`${name}: page horizontal overflow`);
  }
  const result = await page.evaluate(() => {
    const saved = S;
    const base = S.cards[0];
    try {
      const sample = (id, amount, heatValue) => ({
        ...base, gid: id, name: id, final: heatValue,
        revenue: { daily_cny: amount, daily_users: 100, net_per_user_cny: amount / 100,
          horizon_days: 90, selected_member: "steam:1", channels: [], status: "scenario" }
      });
      S = { ...S, cards: [sample("LowRevenueHighHeat", 100, 1000),
        sample("HighRevenueLowHeat", 900, -1000)],
        revenue_meta: { ...S.revenue_meta, estimated: 2, missing: 0 } };
      const root = document.createElement("div");
      root.innerHTML = mustView();
      const names = Array.from(root.querySelectorAll(".g-name")).map(e => e.textContent);
      if (names[0] !== "HighRevenueLowHeat") throw new Error("Revenue order follows heat");
      if (!root.textContent.includes("¥900")) throw new Error("Missing monetary unit");
      MUST.sort = "trend";
      const trendHtml = mustView();
      if (!trendHtml.includes("纯趋势")) throw new Error("Trend view missing");
      MUST.sort = "combined";
      const heatHtml = mustView();
      if (!heatHtml.includes("平台指标")) throw new Error("Heat view missing");
      return { names, monetary: true, trend: true, heat: true };
    } finally {
      S = saved;
      MUST.sort = "revenue";
    }
  });
  await page.setViewportSize({ width: 1440, height: 960 });
  if (errors.length) throw new Error(errors.join("\n"));
  console.log(JSON.stringify({ live, checks: result, errors, output }, null, 2));
})().catch(error => { console.error(error); process.exitCode = 1; })
  .finally(async () => { if (connectedBrowser) await connectedBrowser.close(); });
