const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("D:/Users/KLEI/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright");

async function main() {
  const target = process.argv[2];
  if (!target) throw new Error("Pass the managed task-owned CDP target ID");
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  try {
    let page;
    for (const candidate of browser.contexts()[0].pages()) {
      const session = await candidate.context().newCDPSession(candidate);
      const info = await session.send("Target.getTargetInfo");
      await session.detach();
      if (info.targetInfo.targetId === target) page = candidate;
    }
    assert(page, "Managed page is unavailable");
    page.setDefaultTimeout(30000);
    const errors = [], writes = [];
    page.on("pageerror", error => errors.push(String(error)));
    page.on("request", request => {
      if (request.method() !== "GET") writes.push(request.method() + " " + request.url());
    });
    await page.goto("http://127.0.0.1:8765/", { waitUntil: "domcontentloaded" });
    await page.waitForSelector('[data-page="plat"]');
    const started = Date.now();
    await page.locator('[data-page="plat"]').first().click();
    const navigationMs = Date.now() - started;
    await page.waitForFunction(() => S.cards?.length > 0);
    const singleFlight = await page.evaluate(() => load() === load());
    assert(singleFlight);
    await page.evaluate(() => load());
    const sources = ["steam", "tapmaker", "taptap", "haoyou"];
    const timings = {};
    for (const source of sources) {
      const result = await page.evaluate(source => {
        const begin = performance.now();
        PLAT.tab = "movers";
        pickPlat(source);
        return performance.now() - begin;
      }, source);
      timings[source] = Math.round(result);
      await page.waitForFunction(source => !!RISING[source]?.data, source);
    }
    await page.evaluate(() => { PLAT.tab = "raw"; pickPlat("steam"); });
    await page.waitForFunction(() => RAW.data?.source === "steam");
    const steam = await page.evaluate(() => ({
      total: RAW.data.entities, returned: RAW.data.rows.length,
      header: document.querySelector(".ph-d").innerText,
      visibleRows: document.querySelectorAll("table.dense tbody tr").length
    }));
    assert(steam.total > 1000);
    assert.equal(steam.returned, steam.total);
    const arrows = await page.evaluate(() => [rankChange(3), rankChange(-2)]);
    assert(arrows[0].includes("↑"));
    assert(arrows[1].includes("↓"));
    const stateResource = await page.evaluate(() =>
      performance.getEntriesByType("resource").filter(x => x.name.endsWith("/api/state")).map(x => ({
        milliseconds: Math.round(x.duration), transferred: x.transferSize, decoded: x.decodedBodySize
      })));
    const output = path.resolve(__dirname, "../output/dashboard-loading-fix");
    fs.mkdirSync(output, { recursive: true });
    await page.setViewportSize({ width: 1440, height: 960 });
    await page.screenshot({ path: path.join(output, "steam-all.png") });
    const report = { navigationMs, singleFlight, timings, steam, stateResource, errors, writes };
    fs.writeFileSync(path.join(output, "verification.json"), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report));
    assert.deepEqual(errors, []);
    assert.deepEqual(writes, [], "Navigation must not modify saved labels or trigger collection");
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
