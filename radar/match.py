"""名字清洗和比对（不用 AI）。"""
import difflib
import re
import unicodedata

_SUFFIXES = [
    "手游版", "移动版", "官方正版", "官方版", "正版", "手游", "测试服", "体验服", "国际服", "国服",
    "免费版", "中文版", "demo", "mobile", "playtest", "free to play", "early access",
]
_BRACKETS = re.compile(r"[\[\(（【《「『〖<].*?[\]\)）】》」』〗>]")
_KEEP = re.compile(r"[^0-9a-z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+")
_CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def normalize(name):
    s = unicodedata.normalize("NFKC", name or "").lower().strip()
    stripped = _BRACKETS.sub(" ", s)
    if _KEEP.sub("", stripped):      # 去掉括号后还有内容才去掉括号
        s = stripped
    for suf in _SUFFIXES:
        s = s.replace(suf, " ")
    return _KEEP.sub("", s)


def has_cjk(s):
    return bool(_CJK.search(s or ""))


def linkable(norm):
    """够长才自动合并，避免"合成""射击"这种短词把不相关的游戏连在一起。"""
    if not norm:
        return False
    return len(norm) >= (2 if has_cjk(norm) else 3)


def similarity(a, b):
    """a、b 都是 normalize 之后的名字。返回 0–1。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    short, long_ = sorted([a, b], key=len)
    if len(short) >= (2 if has_cjk(short) else 4) and short in long_:
        return 0.85
    sm = difflib.SequenceMatcher(None, a, b)
    if sm.real_quick_ratio() < 0.5:
        return 0.0
    return sm.ratio()


def company_match(dev, companies):
    d = normalize(dev)
    if not d:
        return False
    for c in companies:
        cn = normalize(c)
        if not cn:
            continue
        if d == cn or (len(cn) >= 4 and cn in d):
            return True
    return False
