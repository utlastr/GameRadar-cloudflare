import json
import os
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path


class InlineScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inline = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.inline = "src" not in dict(attrs)

    def handle_endtag(self, tag):
        if tag == "script":
            self.inline = False

    def handle_data(self, data):
        if self.inline:
            self.parts.append(data)


class WebRouteTests(unittest.TestCase):
    def test_old_links_and_bili_keyword_visibility(self):
        bundled = Path(os.environ.get("USERPROFILE", str(Path.home()))) / (
            ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe")
        node = shutil.which("node") or (str(bundled) if bundled.is_file() else None)
        if not node:
            self.skipTest("Node.js is required for dashboard JavaScript regression")
        parser = InlineScripts()
        parser.feed((Path(__file__).resolve().parents[1] / "radar/web/index.html").read_text(
            encoding="utf-8"))
        harness = r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");
const {script} = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const listeners = {};
const location = {hash:"#kw"};
const elements = new Map();
let stateRequests = 0;
function element(id){
  if(!elements.has(id)) elements.set(id, {
    innerHTML:"", textContent:"", scrollTop:0, querySelector(){return null;},
    classList:{add(){},remove(){}}, scrollTo(){}
  });
  return elements.get(id);
}
const context = vm.createContext({
  console, location,
  history:{replaceState(a,b,url){ location.hash=url; }},
  window:{addEventListener(name,fn){ listeners[name]=fn; }},
  document:{addEventListener(){}, createElement(){ return {}; },
    getElementById:element, querySelectorAll(){return [];},
    head:{appendChild(){}}, querySelector:element},
  fetch:url=>{if(url==="/api/state")stateRequests++;return new Promise(()=>{});},
  setInterval(){}, setTimeout(){},
});
vm.runInContext(script, context);
const evaluate = code => vm.runInContext(code, context);
assert(element("nav").innerHTML.includes('data-page="plat"'), "Navigation must render before data arrives");
assert.equal(evaluate("load()===load()"), true, "Pending state requests must be shared");
assert.equal(stateRequests, 1, "Repeated refreshes must not stack state requests");
assert.match(evaluate("rankChange(3)"), /↑ 3/);
assert.match(evaluate("rankChange(-2)"), /↓ 2/);
assert.equal(evaluate("rankChange(0)"), "-");
evaluate('S={steam_priority:{running:true,phase:"discovery",ts:1793060400}}');
assert.equal(evaluate("itemState({src:'steam'}).v.sample_progress"), "正在扫描近15天发行游戏");
evaluate('S={steam_priority:{running:true,phase:"sampling",ts:1793060400,completed:13,total:1520}}');
assert.equal(evaluate("itemState({src:'steam'}).v.sample_progress"), "本轮在线采样 13 / 1520 款");
assert.equal(evaluate("page"), "plat");
assert.equal(evaluate("PLAT.src"), "bvideo");
assert.equal(evaluate("PLAT.tab"), "raw");
assert.equal(location.hash, "#kw");
assert.equal(evaluate("resolvePage('maker')"), "plat");
assert.equal(evaluate("PLAT.src"), "tapmaker");
assert.equal(evaluate("resolvePage('tapboard')"), "plat");
assert.equal(evaluate("PLAT.src"), "taptap");
assert.equal(evaluate("resolvePage('unknown')"), "heat");
assert.equal(evaluate("resolvePage('status')"), "status");
evaluate(`page="plat"; S={video_hotspots:{stream:{},daily:{topics:[]}}}; pickPlat("bvideo");`);
assert.equal(evaluate("PLAT.tab"), "raw");
assert.match(evaluate("platView()"), /全部数据/);
assert.match(evaluate("platView()"), /趋势/);
assert(!evaluate("platView()").includes("全部关键词"));
assert(!evaluate("platView()").includes('data-ptab="terms"'));
assert(!evaluate("platView()").includes("data-bili-collector"),
  "Individual Bilibili pages must not repeat hub collection status");
assert(!evaluate("platHead({src:'bvideo',g:'video'},{k:'err',t:'失败',v:{error:'Collector error',ok_ts:100}})").includes("Collector error"));
assert.match(evaluate("videoView(false)"), /搜索B站关键词/);
evaluate(`BTERMS.loading=false; BTERMS.data={saved_total:130616,total:130616,page:1,pages:1307,
  status_counts:{pending:113170},rows:[{name:"<New game>",status:"pending",kind:"unknown",
  seed_videos:2,seed_authors:2,sources:{tag:2},first_day:"2026-10-05",last_day:"2026-10-27"}]};`);
const terms=evaluate("biliTermsView()");
assert.match(terms,/130,616/);
assert.match(terms,/&lt;New game&gt;/);
assert(!terms.includes("待判断"));
assert(!terms.includes("data-bterms-status"));
assert(!terms.includes("状态 / 类型"));
assert.match(evaluate(`archivedTermName({name:"ForgeTax",archive:{gid:"g1",member:"steam:123"}})`),/data-game="steam:123"/);
assert.equal(evaluate(`archivedTermName({name:"模拟器"})`),"模拟器");
evaluate("BTERMS.loading=true");
assert.match(evaluate("biliTermsView()"),/&lt;New game&gt;/,
  "Keep existing rows and counts while the next page is loading");
evaluate("BTERMS.loading=false");
assert.match(terms,/data-bterms-page="1"/);
evaluate('SOC.data=null; pickPlat("hub:video")');
assert.match(evaluate("platView()"), /&lt;New game&gt;/,
  "The video hub must show saved Bilibili keywords before social state is available");
assert.equal(location.hash,"#video");
assert.equal(evaluate("resolvePage('video')"),"plat");
assert.equal(evaluate("PLAT.src"),"hub:video");
evaluate(`SOC.data={validation_version:1,enabled:true,browser:true,jobs:[],
  hot:{kuaishou_hot:{name:"快手热榜",items:[]}},lookups:[],buckets:{new:[]},
  bucket_names:[["new","新游戏"]],settings:{}}`);
const hub=evaluate("socHubView('video')");
assert.match(hub,/全部数据/);
assert.match(hub,/趋势/);
assert(!hub.includes("data-bg-hub"));
assert.match(hub,/130,616/);
assert.match(hub,/&lt;New game&gt;/);
assert(!hub.includes("这一栏还没有词"),"An empty legacy hot funnel must not replace saved Bilibili keywords");
assert(!hub.includes("热榜原样"));
assert(!hub.includes("暂无已验证查询记录"));
assert.match(hub,/data-platform-status="video"/);
assert.match(hub,/平台接入状态/);
assert.match(hub,/最近成功/);
assert.match(hub,/最近失败/);
assert.match(hub,/data-plat="soc:douyin"/);
assert.match(hub,/data-plat="todo:YouTube Shorts"/);
assert(!hub.includes("其他平台采集状态"),"Platform states must not be hidden in collapsed details");
assert(hub.indexOf('data-platform-status="video"')<hub.indexOf('id="bili-term-search"'),
  "Platform collection status must precede the keyword data");
assert.match(hub,/data-hub-collector/);
assert(!evaluate(`socItemView("douyin")`).includes("采集状态"),
  "Individual social platforms must not duplicate hub status");
for(const group of ["store","mini","live","index"]){
  evaluate(`page="plat";pickPlat("hub:${group}")`);
  assert.equal(evaluate("PLAT.src"),`hub:${group}`);
  assert.equal(location.hash,`#hub:${group}`);
  const view=evaluate("platView()");
  assert.match(view,new RegExp(`data-platform-status="${group}"`));
  assert.match(view,/最近成功/);
  assert.match(view,/最近失败/);
  assert.equal(evaluate(`resolvePage("hub:${group}")`),"plat");
  assert.equal(evaluate("PLAT.src"),`hub:${group}`);
}
assert.match(evaluate(`platDir("hub:store")`),/button type="button" class="pd-h hub on" data-plat="hub:store"/);
evaluate(`S={sources:{appcn:{ok_ts:100,err_ts:200,error:"Example <failure>"}}}`);
assert.match(evaluate(`platformStatusBody("store")`),/Example &lt;failure&gt;/);
assert.match(evaluate(`platformStatusBody("store")`),/Google Play/);
evaluate(`SOC.data={validation_version:1,enabled:true,browser:true,jobs:[
  {job:"douyin_kw",on:true,ready:true,ok_ts:100,err_ts:200,last_ok:false,error:"Lookup failed"}
],hot:{},lookups:[],buckets:{},bucket_names:[],settings:{}}`);
assert.match(evaluate(`platformStatusBody("video")`),/Lookup failed/);
assert.match(evaluate(`platformStatusBody("video")`),/已接入/);
evaluate(`S={video_hotspots:{stream:{enabled:true,status:"paused",pause_seconds:120,
  latest:{received:100,last_attempt:200,new:5,error:"Paused request"}}}}`);
assert.equal(evaluate("itemState({src:'bvideo'}).v.ok_ts"),100,
  "A failed latest request must not hide the last successful request");
assert.match(evaluate(`platformStatusBody("video")`),/风控暂停剩余 2 分钟/);
const index=evaluate("socHubView('index')");
assert(!index.includes("搜索B站关键词"),"Trend indices must remain separate from video keywords");
assert(!evaluate("SOC_ITEMS.douyin.jobs.includes('douyin_hot')"));
const safe=evaluate(`safeSocialData({validation_version:1,jobs:[{job:"douyin_hot"}],
  hot:{douyin_hot:{items:[{text:"Bogus"}]}},buckets:{new:[
  {term:"Bogus",sources:["douyin_hot"]},{term:"Keep",sources:["douyin_index_hot"]}
  ]}})`);
assert.equal(safe.jobs.length,0);
assert.equal(Object.keys(safe.hot).length,0);
assert.equal(safe.buckets.new.length,1);
assert.equal(evaluate("resolvePage('bili-growth')"),"plat");
assert.equal(evaluate("PLAT.src"),"bvideo");
assert.equal(evaluate("PLAT.tab"),"movers");
assert.equal(evaluate("pageHash()"),"#bili-growth");
evaluate(`BGR.loading=true; BGR.data={day:"2026-10-26",smoothing:100,selected_count:2,
  coverage:{ready_groups:1,baseline_pending_groups:0},sampling:{n:20,due:2},
  tags:[{name:"<Tag>",seed_index:2,index:1.2,today:{views:200,authors:3}}],
  groups:[{name:"ForgeTax",aliases:["铸剑大师","<别名>"],candidate:true,keys:["forgetax","铸剑大师"],
    videos:6,authors:4,seed_index:2,index:1.2,state:"growing",estimated:true,
    today:{views:200,authors:3,remaining_views:50,top_share:.75,new_videos:2},
    series:[],edges:[{a:"forgetax",b:"铸剑大师",left:"ForgeTax",right:"铸剑大师",
      videos:3,authors:3,examples:["BV1000000001"]}]}]}; BGR.mode="groups";`);
const growth=evaluate("biliGrowthView()");
assert.match(growth,/扩散指数/);
assert.match(growth,/铸剑大师/);
assert.match(growth,/&lt;别名&gt;/);
assert.match(growth,/75.0%/);
assert.match(evaluate("biliGrowthDetail(BGR.data.groups[0])"),/data-bg-a="forgetax"/);
assert.match(growth,/播放含边界估算/);
assert(!growth.includes("data-bg-mode"));
assert(!growth.includes("待复采"),"Growth results must not repeat hub collection telemetry");
assert.match(evaluate("biliGrowthSamplingView()"),/待复采/);
evaluate(`BGR.mode="single"`);
assert.match(evaluate("biliGrowthView()"),/暂无单 UP 线索/);
evaluate(`BGR.mode="tags"`);
assert.match(evaluate("biliGrowthView()"),/&lt;Tag&gt;/);
evaluate(`S={video_hotspots:{stream:{
  source:"legacy_game_latest_web",collector_revision:"2026-10-28-fresh-baseline-v6",
  statistics_scope:"all_saved_videos",videos:544000,tagged:114000,pending_tags:427000,
  unavailable:3000,partitions:[],queue:{recent:10,backlog:426990,deferred:200,
    analysis_pending:5,directory_paused:true,low_water:10000},
  throughput:{window_seconds:600,queued_tags_per_minute:30,tagged_per_minute:90,
    net_growth_per_minute:-60,status:"draining",oldest_pending_day:"2026-10-19"}
}}};`);
const collector=evaluate("biliStreamView()");
assert.match(collector,/全库待补 427,000/);
assert.match(collector,/历史队列/);
assert.match(collector,/历史目录扩张已暂缓/);
assert.match(collector,/本地分析待处理 5/);
assert(!collector.includes("后台尚未加载"), "The active queue revision must not show a stale restart warning");
evaluate(`S.video_hotspots.stream.collector_revision="old";`);
assert.match(evaluate("biliStreamView()"),/后台尚未加载最新采集策略/);
evaluate(`S.video_hotspots.stream.collector_revision="2026-10-28-fresh-baseline-v6";
S.video_hotspots.stream.collection_policy={started:1793157600,archived:419000};
S.video_hotspots.stream.baseline={videos:15,tagged:14,pending_tags:1};
S.video_hotspots.stream.queue.historical_disabled=true;`);
const resetCollector=evaluate("biliStreamView()");
assert.match(resetCollector,/新基线起点/);
assert.match(resetCollector,/已归档 419,000 条/);
assert.match(resetCollector,/仅采集新投稿/);
assert(!resetCollector.includes("条后恢复"));
console.log("Route aliases and keyword visibility checks passed");
"""
        result = subprocess.run([node, "-e", harness],
                                input=json.dumps({"script": "\n".join(parser.parts)}),
                                capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
