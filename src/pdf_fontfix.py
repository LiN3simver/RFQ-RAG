"""修復壞掉的 PDF 文字層：依字寬比對系統字型推回正確字元，不靠 OCR。"""
from __future__ import annotations

import io
import os
import re
import statistics
from functools import lru_cache

_WINFONTS = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")

_CANDIDATES = [
    "mingliu.ttc", "msjh.ttc", "msjhbd.ttc", "simsun.ttc", "simsunb.ttf",
    "msyh.ttc", "msyhbd.ttc", "kaiu.ttf", "simhei.ttf", "simkai.ttf",
    "arial.ttf", "arialbd.ttf", "times.ttf", "timesbd.ttf", "tahoma.ttf",
    "calibri.ttf", "msgothic.ttc",
]

_GNAME = re.compile(r"^g(\d+)$")
_DIFF = re.compile(r"/Differences\s*\[(.*?)\]", re.S)
_TOKEN = re.compile(r"(\d+)|/([^\s/\[\]]+)")


@lru_cache(maxsize=1)
def _system_fonts() -> dict:
    from fontTools.ttLib import TTFont, TTCollection

    out: dict = {}
    for fn in _CANDIDATES:
        path = os.path.join(_WINFONTS, fn)
        if not os.path.exists(path):
            continue
        try:
            fonts = (TTCollection(path).fonts if path.lower().endswith(".ttc")
                     else [TTFont(path, lazy=True)])
        except Exception:
            continue
        for tt in fonts:
            try:
                name = tt["name"].getDebugName(6) or fn
                if name in out:
                    continue
                order = tt.getGlyphOrder()
                cmap = tt.getBestCmap()
                rev: dict = {}
                for uni, g in cmap.items():
                    rev.setdefault(g, uni)
                upm = tt["head"].unitsPerEm or 1000
                hm = tt["hmtx"].metrics
                g2u, g2w = {}, {}
                for gid, g in enumerate(order):
                    if g in rev:
                        g2u[gid] = rev[g]
                    if g in hm:
                        g2w[gid] = hm[g][0] * 1000.0 / upm
                out[name] = (g2u, g2w)
            except Exception:
                continue
    return out


def _differences(font_obj: str) -> dict:
    m = _DIFF.search(font_obj)
    if not m:
        return {}
    code, out = 0, {}
    for num, name in _TOKEN.findall(m.group(1)):
        if num:
            code = int(num)
        elif name:
            g = _GNAME.match(name)
            if g:
                out[code] = int(g.group(1))
            code += 1
    return out


def _widths(doc, xref: int) -> dict:
    kind, first = doc.xref_get_key(xref, "FirstChar")
    if kind != "int":
        return {}
    kind, arr = doc.xref_get_key(xref, "Widths")
    if kind not in ("array", "xref"):
        return {}
    if kind == "xref":
        try:
            arr = doc.xref_object(int(str(arr).split()[0]), compressed=False)
        except Exception:
            return {}
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", str(arr))]
    return {int(first) + i: v for i, v in enumerate(nums)}


def _plausible(ch: str) -> bool:
    o = ord(ch)
    return (0x20 <= o <= 0x7E
            or 0x4E00 <= o <= 0x9FFF
            or 0x3000 <= o <= 0x303F
            or 0xFF00 <= o <= 0xFFEF
            or 0x00A0 <= o <= 0x00FF
            or o in (0x2022, 0x25CF, 0x25CB, 0x25A0, 0x2103, 0x2500))


WIDTH_TOL_ABS = 2.0
WIDTH_TOL_REL = 0.01

MIN_WIDTH_PAIRS = 2


def _score_candidates(code2gid: dict, widths: dict) -> dict:
    out = {}
    for name, (g2u, g2w) in _system_fonts().items():
        pairs = [(widths[c], g2w[g]) for c, g in code2gid.items()
                 if c in widths and g in g2w and g2w[g] > 0]
        if len(pairs) < MIN_WIDTH_PAIRS:
            continue
        scale = statistics.median(p / s for p, s in pairs)
        if not (0.9 < scale < 1.1):
            continue
        rate = sum(1 for p, s in pairs
                   if abs(p / scale - s) <= WIDTH_TOL_ABS + WIDTH_TOL_REL * s
                   ) / len(pairs)
        decoded = [chr(g2u[g]) for g in code2gid.values() if g in g2u]
        coverage = len(decoded) / len(code2gid)
        plaus = (sum(1 for c in decoded if _plausible(c)) / len(decoded)
                 if decoded else 0.0)
        out[name] = (rate, plaus, coverage, len(pairs))
    return out


MIN_WIDTH_MATCH = 0.80
MIN_WIDTH_MATCH_DOMINANT = 0.50
MIN_PLAUSIBLE_DOMINANT = 0.85


_TIE_AGREE_MIN = 0.5

_FORM_WORDS = (
    "成本", "材料", "報價", "报价", "原因", "製造", "制造", "廠", "厂",
    "量產", "量产", "日期", "供應", "供应", "規格", "规格", "說明", "说明",
    "單價", "单价", "總價", "总价", "匯率", "汇率", "用量", "單位", "单位",
    "耗損", "耗损", "交期", "備註", "备注", "項目", "项目", "料號", "料号",
    "名稱", "名称", "人工", "工時", "工时", "工站", "組裝", "组装", "委外",
    "其它", "其他", "模具", "治具", "設備", "设备", "投資", "投资", "攤提",
    "管銷", "管销", "合計", "合计", "小計", "小计", "數量", "数量", "更新",
    "新案", "客戶", "客户", "需求", "效率", "幣別", "币别", "原幣", "原币",
    "作業", "作业", "機", "秒", "月", "年", "張", "张", "件", "個", "个",
)


def _has_form_words(text: str) -> bool:
    return any(w in text for w in _FORM_WORDS)


_CJK_RANGE_RE = re.compile(r"[一-鿿]")


def _decode(g2u: dict, code2gid: dict) -> str:
    return "".join(chr(g2u[g]) if g in g2u else "￿"
                   for _c, g in sorted(code2gid.items()))


def _tie_agrees(tied: list[str], code2gid: dict) -> bool:
    decoded = [_decode(_system_fonts()[n][0], code2gid) for n in tied]
    ref = decoded[0]
    if not ref:
        return True
    worst = 1.0
    for other in decoded[1:]:
        same = sum(1 for a, b in zip(ref, other)
                   if a == b or a == "￿" or b == "￿")
        worst = min(worst, same / len(ref))
    return worst >= _TIE_AGREE_MIN


def _dominant(scored: dict) -> str | None:
    weight: dict = {}
    for cands in scored.values():
        for name, (rate, plaus, cov, n) in cands.items():
            if rate >= 0.9 and plaus >= 0.9:
                weight[name] = weight.get(name, 0) + n
    if not weight:
        return None
    return max(weight.items(), key=lambda kv: kv[1])[0]


def glyph_tables(path: str) -> dict:
    import fitz

    doc = fitz.open(path)
    try:
        subsets: dict = {}
        scored: dict = {}
        for pno in range(doc.page_count):
            for f in doc.get_page_fonts(pno, full=True):
                xref, basefont = f[0], f[3]
                if basefont in subsets:
                    continue
                code2gid = _differences(doc.xref_object(xref, compressed=False))
                if not code2gid:
                    continue
                subsets[basefont] = code2gid
                scored[basefont] = _score_candidates(code2gid,
                                                     _widths(doc, xref))
    finally:
        doc.close()

    dom = _dominant(scored)
    tables: dict = {}
    for basefont, cands in scored.items():
        if not cands:
            continue
        name = max(cands, key=lambda n: (round(cands[n][0], 2),
                                         round(cands[n][1], 2),
                                         cands[n][2], cands[n][3]))
        top = (round(cands[name][0], 2), round(cands[name][1], 2))
        tied = [n for n in cands
                if (round(cands[n][0], 2), round(cands[n][1], 2)) == top]
        if len(tied) > 1 and not _tie_agrees(tied, subsets[basefont]):
            decoded = _decode(_system_fonts()[name][0], subsets[basefont])
            if _has_form_words(decoded):
                pass
            elif not _CJK_RANGE_RE.search(decoded):
                pass
            elif dom in tied:
                name = dom
            else:
                continue
        rate, plaus, cov, _ = cands[name]
        ok = rate >= MIN_WIDTH_MATCH
        if not ok and dom is not None:
            ok = (rate >= MIN_WIDTH_MATCH_DOMINANT
                  and plaus >= MIN_PLAUSIBLE_DOMINANT)
        if not ok:
            continue
        g2u = _system_fonts()[name][0]
        tbl = {c: chr(g2u[g]) for c, g in subsets[basefont].items() if g in g2u}
        if tbl:
            tables[basefont] = tbl
    return tables


def _basefont_name(font) -> str | None:
    bf = getattr(font, "basefont", None)
    if bf is None:
        return None
    return getattr(bf, "name", None) or str(bf)


_PATCH_ON = ("PDFSimpleFont", "PDFCIDFont")


class patched_fonts:
    def __init__(self, path: str):
        self.path = path
        self.tables: dict = {}
        self._orig: dict = {}

    def __enter__(self):
        self.tables = glyph_tables(self.path)
        if not self.tables:
            return self
        import pdfminer.pdffont as pf

        tables = self.tables
        for cls_name in _PATCH_ON:
            cls = getattr(pf, cls_name, None)
            if cls is None or "to_unichr" not in cls.__dict__:
                continue
            orig = cls.__dict__["to_unichr"]
            self._orig[cls_name] = orig

            def make(orig):
                def to_unichr(font, cid):
                    tbl = tables.get(_basefont_name(font))
                    if tbl is not None and cid in tbl:
                        return tbl[cid]
                    return orig(font, cid)
                return to_unichr

            cls.to_unichr = make(orig)
        return self

    def __exit__(self, *exc):
        import pdfminer.pdffont as pf
        for cls_name, orig in self._orig.items():
            setattr(getattr(pf, cls_name), "to_unichr", orig)
        self._orig = {}
        return False
