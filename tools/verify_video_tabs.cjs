const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("D:/Users/KLEI/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright");

async function main() {
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  try {
    let page;
    for (const candidate of browser.contexts()[0].pages()) {
      const session = await candidate.context().newCDPSession(candidate);
      const {targetInfo} = await session.send("Target.getTargetInfo");
      await session.detach();
      if (targetInfo.targetId === process.argv[2]) page = candidate;
    }
    assert(page, "Managed page not found");
    const errors = [];
    page.on("pageerror", error => errors.push(String(error)));
    await page.setViewportSize({width: 1440, height: 1000});
    await page.reload();
    await page.waitForFunction(() => typeof BTERMS !== "undefined" && BTERMS.data && !BTERMS.loading);
    assert.deepEqual(await page.locator(".pl-main [data-ptab]").allTextContents(), ["趋势", "全部数据"]);
    assert.equal(await page.locator("[data-bterms-status]").count(), 0);
    assert(!await page.locator("#bili-term-results").innerText().then(t => /待判断|已排除|已匹配游戏/.test(t)));
    const linked = page.locator("#bili-term-results [data-game]").first();
    assert(await linked.count(), "Existing archives must be linked");
    const linkedName = await linked.innerText();
    await linked.click();
    await page.waitForFunction(() => document.querySelector("#drawer").classList.contains("on"));
    await page.locator("#drawer [data-close]").first().click();
    await page.evaluate(() => {
      window.qaSearch = document.querySelector("#bili-term-search");
      window.qaCount = document.querySelector("#bili-term-total");
      window.qaCountText = qaCount.textContent;
      window.qaPager = document.querySelector("#bili-term-page");
    });
    await page.route("**/api/bili-terms?**", async route => {
      if (new URL(route.request().url()).searchParams.get("page") === "2")
        await new Promise(resolve => setTimeout(resolve, 1200));
      await route.continue();
    });
    await page.locator('[data-bterms-page="1"]').click();
    await page.waitForFunction(() => BTERMS.loading);
    assert(await page.evaluate(() => qaSearch === document.querySelector("#bili-term-search")
      && qaCount === document.querySelector("#bili-term-total")
      && qaCount.textContent === qaCountText
      && qaPager === document.querySelector("#bili-term-page")
      && document.querySelectorAll("#bili-term-results tr").length === 100));
    await page.waitForFunction(() => !BTERMS.loading && BTERMS.data.page === 2);
    assert(await page.evaluate(() => qaSearch === document.querySelector("#bili-term-search")
      && qaCount === document.querySelector("#bili-term-total")
      && qaCount.textContent === qaCountText));
    await page.unroute("**/api/bili-terms?**");
    await page.locator('#bili-term-search input').fill("三角洲行动");
    await page.locator('#bili-term-search button').click();
    await page.waitForFunction(() => !BTERMS.loading && BTERMS.data.query === "三角洲行动");
    assert(await page.locator("#bili-term-results").innerText().then(t => t.includes("三角洲行动")));
    await page.locator('[data-ptab="movers"]').click();
    await page.waitForFunction(() => document.querySelector("#bili-growth-controls"));
    assert.deepEqual(await page.locator(".pl-main [data-ptab]").allTextContents(), ["趋势", "全部数据"]);
    assert.equal(await page.locator("[data-bg-mode]").count(), 0);
    await page.locator('[data-plat="bvideo"]').first().click();
    await page.waitForFunction(() => PLAT.src === "bvideo" && PLAT.tab === "raw");
    assert.deepEqual(await page.locator(".pl-main [data-ptab]").allTextContents(), ["趋势", "全部数据"]);
    await page.locator('#bili-term-search input').fill("");
    await page.locator('#bili-term-search button').click();
    await page.waitForFunction(() => !BTERMS.loading && BTERMS.data.query === "");
    const output = path.resolve(__dirname, "../output/video-tabs");
    fs.mkdirSync(output, {recursive: true});
    await page.screenshot({path: path.join(output, "desktop.png")});
    await page.setViewportSize({width: 390, height: 844});
    const mobile = await page.evaluate(() => ({
      width: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    }));
    assert(mobile.scrollWidth <= mobile.width, JSON.stringify(mobile));
    await page.screenshot({path: path.join(output, "mobile.png")});
    assert.deepEqual(errors, []);
    const report = {linkedName, mobile, errors, verified: new Date().toISOString()};
    fs.writeFileSync(path.join(output, "verification.json"), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report));
  } finally {
    await browser.close();
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
