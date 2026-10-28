const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {chromium} = require("D:/Users/KLEI/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright");

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
    assert(page, "Managed target not found");
    const errors = [];
    page.on("pageerror", error => errors.push(String(error)));
    await page.setViewportSize({width: 1440, height: 1000});
    await page.reload();
    await page.waitForFunction(() => typeof SOC !== "undefined" && SOC.data && S.updated);
    const video = page.locator('[data-platform-status="video"]');
    assert(await video.isVisible());
    assert.equal(await video.locator("tbody tr").count(), 6);
    assert.equal(await video.locator("xpath=ancestor::details").count(), 0);
    assert((await video.innerText()).includes("B站视频"));
    assert((await video.innerText()).includes("抖音"));
    assert((await video.innerText()).includes("最近成功"));
    assert((await video.innerText()).includes("最近失败"));
    await page.waitForFunction(() => BTERMS.data && !BTERMS.loading);
    assert(await page.evaluate(() =>
      document.querySelector('[data-platform-status="video"]').getBoundingClientRect().top
      < document.getElementById("bili-term-search").getBoundingClientRect().top));
    await page.evaluate(() => {window.savedSearch=document.getElementById("bili-term-search");});
    await page.evaluate(() => renderContent());
    assert(await page.evaluate(() => savedSearch===document.getElementById("bili-term-search")));
    await page.locator("[data-hub-collector] > summary").click();
    await page.waitForFunction(() => document.querySelector("[data-bili-collector]").innerHTML.length > 0);
    await page.evaluate(() => renderContent());
    assert(await page.locator("[data-hub-collector]").getAttribute("open") !== null);
    assert(await page.evaluate(() => savedSearch===document.getElementById("bili-term-search")));
    await page.locator("[data-hub-collector] > summary").click();
    await page.waitForFunction(() => !document.querySelector("[data-bili-collector]").innerHTML);

    const results = [];
    for (const group of ["store","mini","live","video","index"]) {
      await page.locator(`button[data-plat="hub:${group}"]`).click();
      await page.waitForFunction(group => PLAT.src === `hub:${group}`, group);
      const section = page.locator(`[data-platform-status="${group}"]`);
      assert(await section.isVisible());
      const expected = await page.evaluate(group => PLAT_ITEMS.filter(it => it.g === group).length, group);
      assert.equal(await section.locator("tbody tr").count(), expected);
      const first = section.locator("[data-plat]").first();
      const destination = await first.getAttribute("data-plat");
      await first.click();
      await page.waitForFunction(id => PLAT.src === id, destination);
      assert.deepEqual(await page.locator(".pl-main [data-ptab]").allTextContents(), ["趋势","全部数据"]);
      assert.equal(await page.locator(".pl-main [data-platform-status],.pl-main [data-bili-collector],.pl-main .soc-jobs").count(), 0,
        "Collection status belongs to the platform hub, not individual platforms");
      await page.locator(`button[data-plat="hub:${group}"]`).click();
      await page.reload();
      await page.waitForFunction(group => typeof PLAT !== "undefined" && PLAT.src === `hub:${group}`, group);
      assert(await page.locator(`[data-platform-status="${group}"]`).isVisible());
      results.push({group, platforms: expected, destination});
    }
    await page.locator('button[data-plat="hub:store"]').click();
    await page.locator('[data-platform-status="store"] a[data-plat="todo:Google Play"]').click();
    await page.waitForFunction(() => PLAT.src === "todo:Google Play");
    assert((await page.locator(".pl-main").innerText()).includes("为什么没接上"));
    await page.locator('button[data-plat="hub:video"]').click();
    await page.waitForFunction(() => SOC.data && BTERMS.data && !BTERMS.loading);
    const output = path.resolve(__dirname, "../output/platform-hubs");
    fs.mkdirSync(output, {recursive: true});
    await page.screenshot({path: path.join(output, "desktop.png")});
    const session = await page.context().newCDPSession(page);
    await page.setViewportSize({width: 390, height: 844});
    await session.send("Emulation.setDeviceMetricsOverride", {
      width:390,height:844,screenWidth:390,screenHeight:844,deviceScaleFactor:1,mobile:true,
    });
    const mobile = await page.evaluate(() => ({
      width:document.documentElement.clientWidth,scrollWidth:document.documentElement.scrollWidth,
      visible:!!document.querySelector('[data-platform-status="video"]'),
    }));
    assert(mobile.scrollWidth <= mobile.width, JSON.stringify(mobile));
    assert(await page.locator('.pd-sel option[value="hub:store"]').count());
    await page.locator(".pd-sel").selectOption("hub:live");
    await page.waitForFunction(() => PLAT.src === "hub:live");
    assert(await page.locator('[data-platform-status="live"]').isVisible());
    await page.locator(".pd-sel").selectOption("hub:video");
    await page.waitForFunction(() => PLAT.src === "hub:video");
    await page.screenshot({path: path.join(output, "mobile.png")});
    await session.send("Emulation.clearDeviceMetricsOverride");
    await session.detach();
    assert.deepEqual(errors, []);
    const report = {results,mobile,errors,verified:new Date().toISOString()};
    fs.writeFileSync(path.join(output,"verification.json"), JSON.stringify(report,null,2));
    console.log(JSON.stringify(report));
  } finally {
    await browser.close();
  }
}
main().catch(error => {console.error(error);process.exitCode=1;});
