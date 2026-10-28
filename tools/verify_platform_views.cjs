const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("D:/Users/KLEI/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright");

async function main() {
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  const output = path.resolve(__dirname, "../output/platform-source-fix");
  fs.mkdirSync(output, { recursive: true });
  let page;
  for (const candidate of browser.contexts()[0].pages()) {
    const session = await candidate.context().newCDPSession(candidate);
    const info = await session.send("Target.getTargetInfo");
    await session.detach();
    if (info.targetInfo.targetId === "BA4DFD8C81DA8193A617E778E7F1E347") page = candidate;
  }
  if (!page) throw new Error("Task-owned managed target is unavailable");
  const errors = [];
  page.on("pageerror", e => errors.push(String(e)));
  try {
    await page.setViewportSize({ width: 1440, height: 960 });
    await page.goto("http://127.0.0.1:8765/#plat");
    await page.reload();
    await page.waitForFunction(() => document.querySelector("[data-ptab]"));
    const sources = await page.evaluate(() => PLAT_ITEMS.map(x => x.id));
    const tabs = [];
    for (const source of sources) {
      await page.evaluate(source => pickPlat(source), source);
      const labels = await page.locator("[data-ptab]").allTextContents();
      if (JSON.stringify(labels) !== JSON.stringify(["刚起势", "全部数字"])) {
        throw new Error(source + ": unexpected platform tabs " + JSON.stringify(labels));
      }
      tabs.push({ source, labels });
    }
    await page.evaluate(() => { PLAT.tab = "raw"; pickPlat("steam"); });
    await page.waitForFunction(() => RAW.data?.source === "steam");
    const raw = await page.evaluate(() => ({
      entities: RAW.data.entities, returned: RAW.data.rows.length,
      displayed: document.querySelectorAll("table.dense tbody tr").length,
      pending: RAW.data.pending_count
    }));
    if (raw.entities < 100 || raw.returned !== raw.entities) throw new Error("Steam raw pool is incomplete");
    await page.screenshot({ path: path.join(output, "desktop-steam-all.png") });
    await page.locator("[data-rawdays]").selectOption("15");
    const recent = await page.evaluate(() => rawRows().length);
    if (recent < 100) throw new Error("Recent Steam releases missing");
    const measured = {};
    for (const source of ["steam", "haoyou", "dymini", "taptap", "tapmaker"]) {
      await page.evaluate(source => { PLAT.tab = "movers"; pickPlat(source); }, source);
      await page.waitForFunction(source => !!RISING[source]?.data, source);
      measured[source] = await page.evaluate(source => RISING[source].data.measured, source);
    }
    await page.evaluate(() => { PLAT.tab = "movers"; pickPlat("haoyou"); });
    await page.screenshot({ path: path.join(output, "desktop-haoyou-growth.png") });
    await page.setViewportSize({ width: 390, height: 844 });
    const mobileSession = await page.context().newCDPSession(page);
    await mobileSession.send("Emulation.setDeviceMetricsOverride", {
      width: 390, height: 844, screenWidth: 390, screenHeight: 844,
      deviceScaleFactor: 1, mobile: true
    });
    await page.screenshot({ path: path.join(output, "mobile-haoyou-growth.png") });
    const mobile = await page.evaluate(() => ({
      width: innerWidth, clientWidth: document.documentElement.clientWidth,
      documentWidth: document.documentElement.scrollWidth,
      tabs: [...document.querySelectorAll("[data-ptab]")].map(e => e.textContent),
      removedExplanation: !document.body.innerText.includes("浮萍")
    }));
    if (mobile.documentWidth > mobile.clientWidth) throw new Error("Page-level mobile overflow");
    await mobileSession.detach();
    if (errors.length) throw new Error(errors.join("\n"));
    const report = { tabs, raw, recent, measured, mobile, errors };
    fs.writeFileSync(path.join(output, "browser-verification.json"), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report));
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
