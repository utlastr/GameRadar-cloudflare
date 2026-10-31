# -*- coding: utf-8 -*-
"""在你登录过的浏览器里打开页面，把页面自己拿到的数据"顺手记下来"。

为什么这样做：抖音、快手、小红书的接口要现算签名。
直接请求接口拿不到；但页面自己能拿到、自己能解密。所以：
  1. 开一个后台标签（不抢焦点），在页面脚本运行之前装一个"记录器"；
  2. 记录器记下页面收到的每一份数据：fetch 的 .json()、XHR 的 json、以及页面自己 JSON.parse 解出来的
     记录页面解析的数据；
  3. 打开网址，等几秒，把记下的数据和页面文字取回来，关掉标签。

只读页面本来就会加载的数据，不额外发请求，和你自己打开这个页面一样。
需要浏览器开着调试端口（默认 9222）。没开的话，面板上有"打开雷达专用浏览器"按钮。
"""
import json
import os
import subprocess
import time

from .. import cdp, config

HOOK = r"""
(function(){
  if (window.__rc) return;
  var cap = window.__rc = [];
  var total = 0;
  // v79：只记"里面有一串对象"的数据（榜单、曲线、视频列表都是这样）；配置、埋点这些不记。
  // 以前记满 300 份就把最早的挤掉，抖音页面 JSON.parse 太多，热榜数据被挤掉了。
  function hasList(o, d){
    if (!o || typeof o !== 'object' || d > 7) return false;
    if (Array.isArray(o)) {
      if (o.length >= 3 && o[0] && typeof o[0] === 'object') return true;
      for (var i = 0; i < Math.min(o.length, 5); i++) if (hasList(o[i], d + 1)) return true;
      return false;
    }
    var n = 0;
    for (var k in o) {
      if (++n > 300) break;
      var v = o[k];
      if (v && typeof v === 'object' && hasList(v, d + 1)) return true;
    }
    return false;
  }
  function keep(o, src, raw){
    try {
      if (!o || typeof o !== 'object') return;
      var search = /\/api\/search\/general\/full\/|\/aweme\/v1\/web\/(?:general\/search|search\/item)\//.test(String(src));
      if (src === 'parse' && Array.isArray(o.data)) search = o.data.some(function(row){
        return row && row.aweme_info && row.aweme_info.aweme_id && row.aweme_info.statistics;
      });
      if (search && Array.isArray(o.data) && o.data.some(function(row){ return row && row.aweme_info; })) {
        o = {status_code: o.status_code, has_more: o.has_more, cursor: o.cursor,
          data: o.data.filter(function(row){ return row && row.aweme_info; }).map(function(row){
            var v = row.aweme_info, s = v.statistics || {};
            return {aweme_info: {aweme_id: v.aweme_id, desc: v.desc, create_time: v.create_time,
              statistics: {play_count: s.play_count, comment_count: s.comment_count,
                digg_count: s.digg_count, share_count: s.share_count, collect_count: s.collect_count}}};
          })};
        raw = null;
      }
      if (!search && !hasList(o, 0)) return;
      var s = raw || JSON.stringify(o);
      if (!s || (!search && s.length < 120)) return;
      if (s.length > 1500000) return;
      cap.push({src: String(src).slice(0, 300), t: Date.now(), n: s.length, s: s});
      total += s.length;
      // 满了先挤掉最早的 parse（网络请求拿到的更可能是榜单，留着）
      while (cap.length > 500 || total > 10000000) {
        var idx = -1;
        for (var j = 0; j < cap.length - 1; j++) if (cap[j].src === 'parse') { idx = j; break; }
        if (idx < 0) idx = 0;
        total -= cap.splice(idx, 1)[0].n;
      }
    } catch (e) {}
  }
  var P = JSON.parse;
  JSON.parse = function(t){
    var o = P.apply(this, arguments);
    if (typeof t === 'string' && t.length >= 120 && !arguments[1]) keep(o, 'parse', t);
    return o;
  };
  try { JSON.parse.toString = function(){ return 'function parse() { [native code] }'; }; } catch (e) {}
  if (window.Response && Response.prototype.json) {
    var RJ = Response.prototype.json;
    Response.prototype.json = function(){
      var u = this.url;
      return RJ.apply(this, arguments).then(function(o){
        if (u && u.indexOf('/api/search/general/full/') >= 0 && o && Array.isArray(o.data)) {
          var compact = {
            status_code: o.status_code, has_more: o.has_more, cursor: o.cursor,
            data: o.data.map(function(row){
              var i = (row && row.item) || {}, a = i.author || {}, s = i.stats || {};
              return {type: row && row.type, item: {
                isVideo: !!(row && row.type === 1 && i.video),
                id: i.id, desc: i.desc, createTime: i.createTime,
                share_url: i.id ? 'https://www.tiktok.com/video/' + i.id : '',
                author: {nickname: a.nickname},
                stats: {playCount: s.playCount, commentCount: s.commentCount,
                  diggCount: s.diggCount, shareCount: s.shareCount, collectCount: s.collectCount}
              }};
            })
          };
          keep(compact, 'fetch:https://www.tiktok.com/api/search/general/full/');
        } else {
          keep(o, 'fetch:' + u);
        }
        return o;
      });
    };
  }
  var XO = XMLHttpRequest.prototype.open, XS = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, u){ this.__u = u; return XO.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function(){
    var x = this;
    x.addEventListener('load', function(){
      try {
        if (x.responseType === 'json' && x.response) {
          keep(x.response, 'xhr:' + x.__u);
        } else if (!x.responseType || x.responseType === 'text') {
          var text = x.responseText || '';
          if (text.length >= 120 && /^[\s]*[\[{]/.test(text)) {
            try { keep(JSON.parse(text), 'xhr:' + x.__u); } catch (e) {}
          }
        }
      } catch (e) {}
    });
    return XS.apply(this, arguments);
  };
})();
"""

READ = r"""
(function(){
  var c = window.__rc || [];
  var out = [];
  for (var i = 0; i < c.length; i++) out.push({src: c[i].src, n: c[i].n, s: c[i].s});
  return JSON.stringify({
    url: location.href, title: document.title,
    text: (document.body ? document.body.innerText : '').slice(0, 20000),
    caps: out, result_video_ids: Object.keys(window.__radarResultIds || {}),
    video_link_count: Array.from(document.querySelectorAll('a[href*="/video/"]'))
      .filter(function(a){ return a.getClientRects().length > 0; }).length
  });
})()
"""

SCROLL = r"""
(function(){
  var root = document.scrollingElement || document.documentElement, best = root;
  var area = 0, nodes = document.querySelectorAll('main, section, div');
  for (var i = 0; i < Math.min(nodes.length, 3000); i++) {
    var el = nodes[i], r = el.getBoundingClientRect();
    if (r.width < 200 || r.height < 180 || el.scrollHeight <= el.clientHeight + 20) continue;
    if (!/auto|scroll/.test(getComputedStyle(el).overflowY)) continue;
    var a = r.width * r.height;
    if (a > area) { best = el; area = a; }
  }
  best.scrollTop += Math.max(600, best.clientHeight * 0.9);
  return best.scrollTop;
})()
"""

RESULT_IDS = r"""
(function(){
  var ids = window.__radarResultIds = window.__radarResultIds || {};
  document.querySelectorAll('.search-result-card a[href*="/video/"]').forEach(function(a){
    var m = a.getAttribute('href').match(/\/video\/(\d+)/);
    if (m) ids[m[1]] = true;
  });
  return Object.keys(ids).length;
})()
"""

# 在页面的搜索框里输入关键词并回车
TYPE = r"""
(function(kw){
  var cands = Array.prototype.slice.call(document.querySelectorAll('input'))
    .filter(function(e){ var t = (e.type || 'text').toLowerCase();
      return (t === 'text' || t === 'search') && e.offsetParent !== null; });
  if (!cands.length) return 'no-input';
  var el = cands[0];
  var setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  el.focus(); setter.call(el, kw);
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  ['keydown', 'keypress', 'keyup'].forEach(function(t){
    el.dispatchEvent(new KeyboardEvent(t, {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
  });
  var form = el.form; if (form && form.requestSubmit) { try { form.requestSubmit(); } catch (e) {} }
  var btn = (el.parentElement && el.parentElement.parentElement || document)
    .querySelector('button, [class*=search-btn], [class*=searchBtn], [class*=search_btn]');
  if (btn) btn.click();
  return 'ok';
})(%s)
"""


class NoBrowser(RuntimeError):
    pass


def port():
    from . import settings
    try:
        return int(settings.get("SOCIAL_CDP_PORT") or 9222)
    except (TypeError, ValueError):
        return 9222


def available():
    return cdp.available(port())


def open_capture(url, wait=6.0, after=None, scroll=1):
    """打开网址并记录页面收到的数据。返回 {"url","title","text","caps":[{"src","n","obj"}]}。

    after(tab)：页面打开后再做的操作（比如在搜索框里输入），做完再等 wait 秒。
    """
    p = port()
    if not cdp.available(p):
        raise NoBrowser("浏览器没开调试端口 %d：在各平台 → 视频平台大厅里点“打开雷达专用浏览器”，在里面登录一次各平台" % p)
    # Browser pages created by the collector must go through the shared
    # background-tab helper so they stay in the Codex 后台 group.
    owner = "gameradar-social"
    helper = os.path.join(os.path.expanduser("~"), ".codex", "skills",
                          "background-only-operations", "scripts", "agent-browser.cjs")
    node = os.environ.get("CODEX_NODE") or r"D:\Users\KLEI\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
    if not os.path.exists(helper) or not os.path.exists(node):
        raise NoBrowser("后台标签组 helper 不可用，未创建浏览器页面")
    try:
        initial = "http://127.0.0.1:%d/api/social/blank" % config.SERVER_PORT
        raw = subprocess.check_output([node, helper, "open", initial, owner],
                                      text=True, encoding="utf-8", timeout=40,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        managed = json.loads(raw)
    except Exception as e:
        raise NoBrowser("创建后台标签失败：%s" % e) from e
    if not managed.get("targetId") or not managed.get("tabId"):
        raise NoBrowser("后台标签组未返回可接管的标签")
    deadline = time.time() + 15
    tab = None
    while time.time() < deadline and tab is None:
        for target in cdp.targets(p):
            if target.get("id") == managed["targetId"] and target.get("webSocketDebuggerUrl"):
                tab = cdp.Tab(target["webSocketDebuggerUrl"])
                break
        if tab is None:
            time.sleep(0.2)
    if tab is None:
        subprocess.run([node, helper, "close", str(managed["tabId"]), owner],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        raise NoBrowser("后台标签已创建但无法连接")
    preserve = False
    try:
        tab.call("Page.enable")
        tab.call("Page.addScriptToEvaluateOnNewDocument", {"source": HOOK})
        tab.call("Page.navigate", {"url": url}, timeout=45)
        time.sleep(wait)
        if after:
            after(tab)
            time.sleep(wait)
        tab.js(RESULT_IDS)
        for _ in range(max(0, scroll)):
            try:
                tab.js(SCROLL)
            except Exception:                    # noqa: BLE001
                pass
            time.sleep(2.0)
            tab.js(RESULT_IDS)
        raw = tab.js(READ, timeout=60) or "{}"
        try:
            captured = json.loads(raw)
            reason = problem(captured)
            preserve = "人机验证" in reason or (
                "要登录" in reason and not captured.get("video_link_count")
                and not captured.get("result_video_ids"))
        except (ValueError, TypeError):
            pass
    finally:
        tab.close()
        if not preserve:
            subprocess.run([node, helper, "close", str(managed["tabId"]), owner],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        d = json.loads(raw)
    except ValueError:
        d = {"caps": []}
    caps = []
    for c in d.get("caps") or []:
        try:
            caps.append({"src": c.get("src") or "", "n": c.get("n") or 0, "obj": json.loads(c.get("s") or "null")})
        except ValueError:
            continue
    d["caps"] = caps
    if preserve:
        d["target_id"] = managed["targetId"]
        d["tab_id"] = managed["tabId"]
    return d


def verification_cleared(target_id, expected_url):
    """Only observe the retained page; never solve a challenge or navigate it."""
    if not target_id or not expected_url:
        return False
    target = next((t for t in cdp.targets(port()) if t.get("id") == target_id
                   and t.get("type") == "page" and t.get("url") == expected_url), None)
    if not target or not target.get("webSocketDebuggerUrl"):
        return False
    tab = cdp.Tab(target["webSocketDebuggerUrl"])
    try:
        state = tab.js("""(function(){
          var visible = function(e){ return e.getClientRects().length > 0; };
          var blocked = Array.from(document.querySelectorAll(
            'iframe[src*="captcha"],iframe[src*="verify"],[id*="captcha"],[class*="captcha"]'
          )).some(visible);
          return {title:document.title, text:document.body ? document.body.innerText : '',
            blocked:blocked, videos:Array.from(document.querySelectorAll(
              'a[href*="/video/"]')).filter(visible).length};
        })()""")
        return bool(isinstance(state, dict) and not state.get("blocked")
                    and state.get("videos", 0) > 0 and not problem(state))
    finally:
        tab.close()


def type_search(kw):
    def go(tab):
        tab.js(TYPE % json.dumps(kw, ensure_ascii=False))
    return go


def problem(d):
    """页面没认出数据时，按页面文字和标题猜原因；猜不出返回空字符串。"""
    text = (d.get("text") or "")
    head = text[:4000]
    title = d.get("title") or ""
    if any(w in head or w in title for w in ("验证码", "安全验证", "人机验证", "拖动滑块", "滑块验证", "请完成验证",
                                             "Verify", "verify you are human", "captcha")):
        return "平台弹了人机验证：在雷达专用浏览器里打开这个平台，手动过一次验证"
    if len(text.strip()) < 40:
        return "页面是空的（可能没打开：网络不通、TikTok 要能上外网，或者页面还没加载完）"
    if login_hint(text):
        return "页面要登录：在雷达专用浏览器里登录这个平台"
    return ""


def login_hint(text):
    """页面文字里有"登录"字样又没数据时，提示去专用浏览器登录。"""
    t = (text or "")[:3000]
    return any(w in t for w in ("扫码登录", "登录后", "请登录", "立即登录", "手机号登录", "Log in", "登录/注册"))


# ---------------- 雷达专用浏览器 ----------------

def _browser_exe():
    try:
        return cdp._find_browser()
    except Exception:                            # noqa: BLE001
        return None


def show(url):
    """Open a managed background page in the existing authorized browser."""
    p = port()
    if not cdp.available(p):
        return launch_browser()
    helper = os.path.join(os.path.expanduser("~"), ".codex", "skills",
                          "background-only-operations", "scripts", "agent-browser.cjs")
    node = os.environ.get("CODEX_NODE") or r"D:\Users\KLEI\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
    try:
        subprocess.check_output([node, helper, "open", url, "gameradar-social"],
                                text=True, encoding="utf-8", timeout=40,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "note": "打开失败：%s" % e}
    return {"ok": True, "note": "已在 Codex 后台标签组打开，请在平台完成登录或验证"}


def launch_browser(background=False):
    """Reuse the existing CDP session; never launch a separate browser/profile."""
    p = port()
    if cdp.available(p):
        return {"ok": True, "note": "已经开着（端口 %d）" % p}
    return {"ok": False, "note": "现有浏览器 CDP %d 未连接；未启动新浏览器，未修改调试设置" % p}
