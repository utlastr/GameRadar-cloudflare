# -*- coding: utf-8 -*-
"""在你登录过的浏览器里打开页面，把页面自己拿到的数据"顺手记下来"。

为什么这样做：抖音、抖音指数、快手、小红书的接口要现算签名，抖音指数的返回还是加密的。
直接请求接口拿不到；但页面自己能拿到、自己能解密。所以：
  1. 开一个后台标签（不抢焦点），在页面脚本运行之前装一个"记录器"；
  2. 记录器记下页面收到的每一份数据：fetch 的 .json()、XHR 的 json、以及页面自己 JSON.parse 解出来的
     （抖音指数解密后也要 JSON.parse，所以解密后的数据也能记到）；
  3. 打开网址，等几秒，把记下的数据和页面文字取回来，关掉标签。

只读页面本来就会加载的数据，不额外发请求，和你自己打开这个页面一样。
需要浏览器开着调试端口（默认 9222）。没开的话，面板上有"打开雷达专用浏览器"按钮。
"""
import json
import time

from .. import cdp

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
      if (!hasList(o, 0)) return;
      var s = raw || JSON.stringify(o);
      if (!s || s.length < 120) return;
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
  if (window.Response && Response.prototype.json) {
    var RJ = Response.prototype.json;
    Response.prototype.json = function(){
      var u = this.url;
      return RJ.apply(this, arguments).then(function(o){ keep(o, 'fetch:' + u); return o; });
    };
  }
  var XO = XMLHttpRequest.prototype.open, XS = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, u){ this.__u = u; return XO.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function(){
    var x = this;
    x.addEventListener('load', function(){
      try {
        if (x.responseType === 'json' && x.response) keep(x.response, 'xhr:' + x.__u);
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
    caps: out
  });
})()
"""

# 在页面的搜索框里输入关键词并回车（哔哩指数关键词页网址未知时用）
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
        raise NoBrowser("现有浏览器的 CDP %d 未连接，采集未执行" % p)
    tab = cdp.open_tab("about:blank", p, wait=0.5, background=True)
    try:
        tab.call("Page.enable")
        tab.call("Page.addScriptToEvaluateOnNewDocument", {"source": HOOK})
        tab.call("Page.navigate", {"url": url}, timeout=45)
        time.sleep(wait)
        if after:
            after(tab)
            time.sleep(wait)
        for _ in range(max(0, scroll)):
            try:
                tab.js("window.scrollBy(0, document.body ? document.body.scrollHeight : 3000); 1")
            except Exception:                    # noqa: BLE001
                pass
            time.sleep(2.0)
        raw = tab.js(READ, timeout=60) or "{}"
    finally:
        cdp.close_tab(tab)
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
    return d


def type_search(kw):
    def go(tab):
        result = tab.js(TYPE % json.dumps(kw, ensure_ascii=False))
        if result != "ok":
            raise RuntimeError("页面没有可用的搜索框，未提交查询词")
    return go


def problem(d):
    """页面没认出数据时，按页面文字和标题猜原因；猜不出返回空字符串。"""
    text = (d.get("text") or "")
    head = text[:4000]
    title = d.get("title") or ""
    if any(w in head or w in title for w in ("验证码", "安全验证", "人机验证", "拖动滑块", "滑块验证", "请完成验证",
                                             "Verify", "verify you are human", "captcha")):
        return "平台要求人机验证，采集已停止；请在现有浏览器中处理"
    if len(text.strip()) < 40:
        return "页面是空的（可能没打开：网络不通、TikTok 要能上外网，或者页面还没加载完）"
    if login_hint(text):
        return "页面要求登录；请在现有浏览器中登录这个平台"
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
    """在雷达专用浏览器里把一个页面打开到前面（平台要人机验证时，你拖一下滑块就行）。"""
    p = port()
    if not cdp.available(p):
        r = launch_browser()
        if not cdp.available(p):
            return r
    try:
        cdp.create_target(url, p, background=False).close()     # 只断开连接，页面留着
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "note": "打开失败：%s" % e}
    return {"ok": True, "note": "已在现有浏览器中打开页面"}


def launch_browser(background=False):
    """Compatibility endpoint: inspect the existing browser, never start a profile."""
    p = port()
    if cdp.available(p):
        return {"ok": True, "note": "已连接现有浏览器（端口 %d）" % p}
    return {"ok": False, "note": "现有浏览器的 CDP %d 未连接；未启动新浏览器或修改浏览器设置" % p}
