import json
import os
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path


class Scripts(HTMLParser):
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


class HeatDashboardTests(unittest.TestCase):
    def test_video_only_global_ranking_references_and_loading(self):
        bundled = Path(os.environ.get("USERPROFILE", str(Path.home()))) / (
            ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe")
        node = shutil.which("node") or (str(bundled) if bundled.is_file() else None)
        if not node:
            self.skipTest("Node.js is required for dashboard JavaScript tests")
        parser = Scripts()
        parser.feed((Path(__file__).resolve().parents[1] / "radar/web/index.html").read_text(
            encoding="utf-8"))
        harness = r"""
const assert=require("node:assert/strict"), vm=require("node:vm");
const {script}=JSON.parse(require("node:fs").readFileSync(0,"utf8"));
const elements=new Map();
function element(id){
  if(!elements.has(id)) elements.set(id,{
    innerHTML:"",textContent:"",scrollTop:0,querySelector(){return null;},
    classList:{add(){},remove(){}},scrollTo(){}
  });
  return elements.get(id);
}
const context=vm.createContext({
  console,URL,location:{hash:"#must"},history:{replaceState(){}},
  window:{addEventListener(){}},
  document:{addEventListener(){},createElement(){return {};},getElementById:element,
    querySelectorAll(){return [];},head:{appendChild(){}},querySelector:element},
  fetch:()=>new Promise(()=>{}),setInterval(){},setTimeout(){}
});
vm.runInContext(script,context);
const run=code=>vm.runInContext(code,context);
assert.equal(run("page"),"heat");
for(const old of ["must","revenue","trend","combined"])
  assert.equal(run(`resolvePage("${old}")`),"heat");
assert.match(element("nav").innerHTML,/data-page="heat"/);
assert(!element("nav").innerHTML.includes("必看"));
run(`
  const now=Math.floor(Date.now()/1000), day=heatDay(1);
  function native(key,cur={},prev={},age=0,hours=24,extra={}){
    return {key,id:key.split(":")[1],name:key,cur,prev,extra,
      sampled:Object.fromEntries(Object.keys(cur).map(m=>[m,now-age])),
      previous_at:Object.fromEntries(Object.keys(prev).map(m=>[m,now-age-hours*3600]))};
  }
  function group(id,name,index,gid=null,member=null,aliases=[]){
    return {id,name,index,aliases,archive:gid?{gid,member}:null,
      videos:5,authors:4,series:[],today:{views:100,authors:3,remaining_views:50}};
  }
  const forge=group("forge","ForgeTax",5,"forge","steam:1",["铸剑大师"]);
  const roblox=group("roblox","Roblox hit",9,"roblox","roblox:1");
  const tap=group("tap","Tap hit",2,"tap","tapmaker:1");
  const unknown=group("unknown","<Unreleased>",null,null,null,["新游戏"]);
  HEAT.data={bvideo:{day,groups:[forge,roblox,tap,unknown]},youtube:{day,groups:[
    group("forge-youtube","ForgeTax",3,"forge","steam:1"),
    group("youtube","YouTube hit",8,"youtube","steam:2")
  ]}};
  HEAT.references={
    steam:{"steam:1":native("steam:1",{store_reviews:41},{store_reviews:1})},
    tapmaker:{"tapmaker:1":native("tapmaker:1",{downloads:1e12},{downloads:1})}
  };
  HEAT.loaded=Date.now(); HEAT.loading=null;
  S={cards:[],totals:{cards:0}};
`);
assert.equal(run("heatRows().length"),5,"Same archive must be one game across video platforms");
assert.equal(run("heatRows()[0].name"),"Roblox hit");
assert.equal(run("heatRows()[1].name"),"YouTube hit");
assert.equal(run("heatRows()[2].name"),"ForgeTax");
assert.equal(run("heatRows()[2].index"),7,"1 + (5-1) + (3-1)");
assert.equal(run("heatRows()[4].rank"),null,"Missing index is not 1x or a numerical rank");
assert.equal(run("heatRows('bvideo','铸剑大师')[0].index"),7,
  "Filtering must not recompute the global index");
assert.equal(run("heatRows('bvideo','铸剑大师')[0].rank"),3);
assert.equal(run("heatRows('all','新游戏').length"),1);
assert.equal(run("heatRows('all','steam:1').length"),1);
run("HEAT.references.tapmaker['tapmaker:1'].cur.downloads=1e20");
assert.equal(run("heatRows()[0].name"),"Roblox hit","Downloads cannot affect rank");
run("HEAT.data.steam={rows:[{name:'Huge download game',cur:{downloads:1e30}}]}");
assert.equal(run("heatRows().length"),5,"Store records cannot become video ranking candidates");
run(`HEAT.data.bvideo.groups.push(group("forge-duplicate","铸剑纳贡",4,"forge","steam:1"))`);
assert.equal(run("heatRows().find(r=>r.name==='ForgeTax').index"),7,
  "Same-platform alias groups must not double count");
assert(run("heatRows().find(r=>r.name==='ForgeTax').aliases.includes('铸剑纳贡')"));
run(`S.config={video_heat:{weights:{bvideo:2,youtube:.5}}}`);
assert.equal(run("heatRows().find(r=>r.name==='ForgeTax').index"),10);
assert.equal(run("heatIndex([{source:'bvideo',index:.5},{source:'youtube',index:1}])"),1);
assert.equal(run("heatIndex([{source:'bvideo',index:null}])"),null);
run("S.config={}");
const html=run("heatView()");
assert.match(html,/视频热度指数/);
assert.match(html,/&lt;Unreleased&gt;/);
assert.match(html,/铸剑大师/);
assert.match(html,/data-game="steam:1"/);
assert.match(html,/视频证据/);
assert.match(html,/商店数据（参考）/);
assert(!/data-heatsrc|<th>市场|TapTap 同类/.test(html),"One list, no source classifications");
assert(!/轮换|预估收益|纯趋势/.test(html));
assert.equal(run("counts().heat"),5);
run(`HEAT.data.bvideo.day=heatDay(3)`);
assert.equal(run("heatRows().find(r=>r.name==='Roblox hit').rank"),null,
  "Stale evidence cannot carry a live rank");
assert.equal(run("heatRows().find(r=>r.name==='ForgeTax').index"),3);
run(`HEAT.data.bvideo.day=day;HEAT.data.bvideo.baseline_started=now`);
assert.equal(run("heatRows().find(r=>r.name==='Roblox hit').rank"),null,
  "Previous baseline results cannot rank after reset");
run("delete HEAT.data.bvideo.baseline_started");
run(`
  HEAT.history={
    ["bvideo:"+day]:{day,groups:[forge]},
    ["bvideo:"+heatDay(2)]:{day:heatDay(2),groups:[
      group("old-id","铸剑大师",3,"forge","steam:1"),
      group("double","ForgeTax",4,"forge","steam:1")
    ]},
    ["bvideo:"+heatDay(3)]:{day:heatDay(3),groups:[group("no","ForgeTax",null)]},
    ["bvideo:"+heatDay(4)]:{day:heatDay(4),groups:[group("eq","ForgeTax",2)]},
    ["bvideo:"+heatDay(15)]:{day:heatDay(15),groups:[forge]}
  };
`);
assert.equal(run("heatRecurrence(heatRows().find(r=>r.name==='ForgeTax')).days"),2);
assert.equal(run("heatRecurrence(heatRows().find(r=>r.name==='ForgeTax')).measured"),3);
run(`HEAT.data.bvideo.groups=Array.from({length:601},(_,i)=>group("new"+i,"Game "+i,2+i));delete HEAT.data.youtube`);
assert.equal(run("heatRows().length"),601,"No old 400-row cap or score/catalog-match gate");
assert.match(run("heatView()"),/data-heatmore/);
run("HEAT.limit=1000");
assert.equal((run("heatView()").match(/data-videoheat=/g)||[]).length,601);
assert.equal(run("heatMetrics('steam',native('steam:1',{store_reviews:41},{store_reviews:1}))[0].delta"),40);
assert.equal(run("heatMetrics('roblox',native('roblox:1',{players:0},{players:10}))[0].delta"),-10);
assert.equal(run("heatMetrics('tapmaker',native('tapmaker:1',{downloads:1},{downloads:10}))[0].state"),"reset");
assert.equal(run("heatMetrics('steam',native('steam:1',{store_reviews:1},{},40*3600))[0].state"),"stale");
assert.equal(run("heatMetrics('dymini',native('dymini:1',{rank_new:30},{rank_new:50}))[0].delta"),20);

// Empty growth results must not erase games, annotations or existing matches.
run(`
  HEAT.data={bvideo:{day,groups:[],baseline_started:now}};
  HEAT.matches=[
    {name:"铸剑大师",archive:{gid:"forge",member:"steam:1"},seed_videos:20,seed_authors:8},
    {name:"植物大战僵尸",archive:{gid:"pvz",member:"bili:植物大战僵尸"},
      seed_videos:1150,seed_authors:831,status:"candidate"},
    {name:"重复别名",archive:{gid:"pvz",member:"bili:植物大战僵尸"},
      seed_videos:100,seed_authors:90},
    {name:"Seed without video",archive:{gid:"seed",member:"steam:seed"},
      seed_videos:0,seed_authors:0}
  ];
  S.cards=[
    {gid:"forge",name:"ForgeTax",members:["steam:1"],devs:["Forge Studio"],
      flags:[{t:"上线以来每天 2890 热度，同平台新游戏前 1%",c:"hot"}],
      info:{launch_ts:now-86400,launch_src:"steam"},first_seen:now-86400,
      label:{note:"值得测试 <demo>",aliases:["铸剑纳贡"]},
      heat:{videos:[{p:"bilibili",name:"B站",recent:991,n:6,trend_known:false}]}},
    {gid:"unseen",name:"Store only",members:["tapmaker:9"],final:1e10,
      heat:{videos:[],down:{value:1e20,x:1e10}},label:{}},
    {gid:"other",name:"Video leader",members:["roblox:9"],
      heat:{videos:[{p:"bilibili",name:"B站",recent:2000,n:10}]}},
    {gid:"hidden",name:"Hidden",members:["steam:99"],label:{blocked:true}}
  ];
`);
assert.equal(run("heatRows().length"),4,"Catalog plus matched game union; not growth-groups only");
assert.equal(run("heatRows()[0].name"),"Video leader","Provisional order uses only video observations");
assert.equal(run("heatRows()[1].name"),"ForgeTax");
assert.equal(run("heatRows('all','铸剑大师').length"),1,"Term joins the existing card");
assert.equal(run("heatRows('all','铸剑大师')[0].position"),2,"Search preserves global table position");
assert.equal(run("heatRows('all','植物大战僵尸').length"),1,"Candidate matches stay visible and deduplicated");
assert.equal(run("heatRows('all','值得测试').length"),1,"Notes remain searchable");
assert.equal(run("heatRows().every(r=>r.index===null)"),true,"No invented baseline ratios");
const restored=run("heatView()");
assert.match(restored,/同平台新游戏前 1%/);
assert.match(restored,/Forge Studio/);
assert.match(restored,/值得测试 &lt;demo&gt;/);
assert.match(restored,/data-gid="forge"/);
assert.match(restored,/趋势历史不足/);
assert.match(restored,/历史发现样本，非当日增量/);
assert(!/没有匹配游戏|连续 10 天|data-heatsrc/.test(restored));
run(`S.cards.find(c=>c.gid==="unseen").heat.down.value=1e100`);
assert.equal(run("heatRows()[0].name"),"Video leader");
run(`HEAT.data.bvideo.groups=[group("forge","铸剑大师",8,"forge","steam:1")];
     delete HEAT.data.bvideo.baseline_started`);
assert.equal(run("heatRows()[0].name"),"ForgeTax");
assert.equal(run("heatRows()[0].index"),8);
assert.equal(run("heatRows().length"),4,"Growth joins, rather than replacing or duplicating the game");

(async()=>{
  await run(`(async()=>{
    page="status";HEAT.source="all";HEAT.loaded=0;HEAT.loading=null;
    S.cards=[];HEAT.matches=[];
    HEAT.data={bvideo:{day,groups:[forge]}};HEAT.history={};HEAT.historyLoaded=0;
    let active=0,maximum=0;const requests=[];
    getJSON=async url=>{
      requests.push(url);maximum=Math.max(maximum,++active);await Promise.resolve();active--;
      if(url==="/api/bili-growth")return {day,groups:[forge]};
      if(url==="/api/bili-terms?matched=1")return {rows:[]};
      if(url.startsWith("/api/raw?"))return {rows:[native("steam:1",{store_reviews:42})]};
      const d=new URL(url,"http://localhost").searchParams.get("day");
      return {day:d,groups:[]};
    };
    const one=loadHeat(true),two=loadHeat(true);
    if(one!==two)throw Error("Duplicate requests");
    await one;
    if(maximum>3)throw Error("Unbounded requests");
    if(requests.filter(u=>u==="/api/bili-growth").length!==1)throw Error("Duplicate growth read");
    if(requests.some(u=>u.includes("source=tapmaker")))throw Error("Unrelated stores loaded");
    if(!HEAT.references.steam["steam:1"])throw Error("Missing store references");
    if(Object.keys(HEAT.history).length!==14)throw Error("Incomplete history");
    getJSON=async()=>null;
    await loadHeat(true);
    if(HEAT.data.bvideo.groups[0].name!=="ForgeTax")throw Error("Failure erased prior video data");
    if(!HEAT.failed.bvideo)throw Error("Video read failure hidden");
    if(HEAT.loading!==null)throw Error("Loading lock not released");
  })()`);
  console.log("Video-only global ranking and reference checks passed");
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(
            [node, "-e", harness], input=json.dumps({"script": "\n".join(parser.parts)}),
            capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Video-only global ranking and reference checks passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
