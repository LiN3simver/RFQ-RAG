"""規格同義詞展開與數值單位正規化（中文數字、速率、長度等）。"""
from __future__ import annotations

import re
from decimal import Decimal

TERMS: dict[str, list[str]] = {
    "電纜": ["cable", "cbl"], "电缆": ["cable", "cbl"],
    "線材": ["cable", "cbl"], "线材": ["cable", "cbl"],
    "線纜": ["cable", "cbl"], "线缆": ["cable", "cbl"],
    "纜線": ["cable", "cbl"], "缆线": ["cable", "cbl"],
    "線": ["cable"], "线": ["cable"],
    "扁線": ["flat", "ffc"], "扁线": ["flat", "ffc"],
    "排線": ["flat", "ffc", "idc"], "排线": ["flat", "ffc", "idc"],
    "軟排線": ["ffc", "flat"], "软排线": ["ffc", "flat"],
    "圓線": ["round"], "圆线": ["round"],
    "裸線": ["open", "cut"], "裸线": ["open", "cut"],
    "銅纜": ["copper", "dac"], "铜缆": ["copper", "dac"],
    "銅": ["copper"], "铜": ["copper"],
    "光纖": ["fiber", "optical", "aoc"], "光纤": ["fiber", "optical", "aoc"],
    "網路線": ["cat6a", "cat6", "cat5e", "ethernet", "rj45"],
    "网路线": ["cat6a", "cat6", "cat5e", "ethernet", "rj45"],
    "網線": ["cat6a", "cat6", "ethernet", "rj45"],
    "网线": ["cat6a", "cat6", "ethernet", "rj45"],
    "乙太網": ["ethernet", "cat6a", "rj45"], "以太网": ["ethernet", "cat6a", "rj45"],
    "電源線": ["power", "pwr"], "电源线": ["power", "pwr"],
    "電源": ["power", "pwr"], "电源": ["power", "pwr"],
    "風扇線": ["fan"], "风扇线": ["fan"],
    "直連線": ["dac", "passive"], "直连线": ["dac", "passive"],
    "分接線": ["breakout", "splitter"], "分接线": ["breakout", "splitter"],
    "轉接線": ["adapter", "riser"], "转接线": ["adapter", "riser"],
    "延伸線": ["extension", "riser"], "延长线": ["extension", "riser"],
    "組合線": ["assembly", "assy"], "组合线": ["assembly", "assy"],

    "連接器": ["connector", "conn"], "连接器": ["connector", "conn"],
    "接頭": ["connector", "conn", "plug"], "接头": ["connector", "conn", "plug"],
    "端子": ["terminal", "ter"], "膠殼": ["housing", "hsg"], "胶壳": ["housing", "hsg"],
    "外殼": ["housing", "hsg"], "外壳": ["housing", "hsg"],
    "殼": ["housing", "hsg"], "壳": ["housing", "hsg"],
    "母頭": ["female", "receptacle"], "母头": ["female", "receptacle"],
    "公頭": ["male", "plug"], "公头": ["male", "plug"],
    "母": ["female"], "公": ["male"],
    "針": ["pin"], "针": ["pin"], "腳": ["pin"], "脚": ["pin"],
    "線位": ["pin", "position"], "线位": ["pin", "position"],
    "排": ["row"], "間距": ["pitch"], "间距": ["pitch"],
    "板端": ["board", "smt"], "板上": ["board", "smt"],
    "背板": ["backplane", "bp"], "光模組": ["transceiver", "qsfp", "sfp", "osfp"],
    "光模块": ["transceiver", "qsfp", "sfp", "osfp"],
    "顯示卡": ["pcie", "gpu", "riser"], "显示卡": ["pcie", "gpu", "riser"],
    "顯卡": ["pcie", "gpu"], "显卡": ["pcie", "gpu"],
    "硬碟": ["sas", "sata", "hdd", "ssd"], "硬盘": ["sas", "sata", "hdd", "ssd"],

    "直角": ["ra", "r/a"],
    "直式": ["str", "straight", "st"], "直": ["str", "straight"],
    "垂直": ["vt", "v/t", "vertical"],
    "折線": ["fold", "folded"], "折线": ["fold", "folded"],
    "折疊": ["fold", "folded", "flat"], "折叠": ["fold", "folded", "flat"],
    "預折": ["fold"], "预折": ["fold"],
    "薄型": ["thinline", "thin", "slimline", "lp"],
    "超薄": ["thinline", "thin", "slim"],
    "低背": ["lp"],

    "遮蔽": ["shielded", "stp", "sftp"], "屏蔽": ["shielded", "stp", "sftp"],
    "無遮蔽": ["unshielded", "utp", "u/utp"], "无屏蔽": ["unshielded", "utp", "u/utp"],
    "電阻": ["resistor"], "电阻": ["resistor"],
    "旁路": ["bypass"], "門": ["door"], "门": ["door"],
    "複合": ["combo"], "复合": ["combo"],
    "音效": ["audio"], "音頻": ["audio"], "音频": ["audio"],
    "光纖輸出": ["spdif", "optical"], "光纤输出": ["spdif", "optical"],
    "無鹵": ["hf", "lszh"], "无卤": ["hf", "lszh"],
    "高頻": ["si"], "高频": ["si"],
    "被動": ["passive"], "被动": ["passive"],
    "主動": ["active"], "主动": ["active"],

    "紅色": ["red"], "红色": ["red"], "紅": ["red"], "红": ["red"],
    "綠色": ["green"], "绿色": ["green"], "綠": ["green"], "绿": ["green"],
    "藍色": ["blue"], "蓝色": ["blue"], "藍": ["blue"], "蓝": ["blue"],
    "黑色": ["black"], "黑": ["black"],
    "白色": ["white"], "白": ["white"],
    "黃色": ["yellow"], "黄色": ["yellow"],
    "灰色": ["gray", "grey"], "橘色": ["orange", "ornge"], "橙色": ["orange"],
    "紫色": ["purple"], "粉色": ["pink"],

    "第三代": ["gen3"], "第四代": ["gen4"], "第五代": ["gen5"], "第六代": ["gen6"],
    "三代": ["gen3"], "四代": ["gen4"], "五代": ["gen5"], "六代": ["gen6"],
    "萬兆": ["10g"], "万兆": ["10g"], "千兆": ["1g"], "百兆": ["100m"],
}

_SPEED_RE = re.compile(
    r"([一二兩三四五六七八九十百千萬万零0-9\.]+)\s*([GgTt])(?:bps|BPS|B)?(?![A-Za-z0-9])")

_LEN_UNITS: list[tuple[str, Decimal, list[str], bool]] = [
    ("公釐", Decimal(1), ["mm"], True), ("毫米", Decimal(1), ["mm"], True),
    ("公分", Decimal(10), ["cm"], True), ("厘米", Decimal(10), ["cm"], True),
    ("公尺", Decimal(1000), ["m"], True), ("米", Decimal(1000), ["m"], True),
    ("英尺", Decimal("304.8"), ["ft", "feet"], False),
    ("呎", Decimal("304.8"), ["ft", "feet"], False),
    ("英吋", Decimal("25.4"), ["inch", "in"], False),
    ("吋", Decimal("25.4"), ["inch", "in"], False),
]

_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000, "萬": 10000, "万": 10000}


def cn_number(s: str) -> Decimal | None:
    s = s.strip()
    if not s:
        return None
    if re.fullmatch(r"[0-9]+(\.[0-9]+)?", s):
        return Decimal(s)

    m = re.match(r"^(.*?)[點点](.+)$", s)
    if m:
        head = cn_number(m.group(1) or "零")
        frac = "".join(str(_CN_DIGIT[c]) if c in _CN_DIGIT else
                       (c if c.isdigit() else "") for c in m.group(2))
        if head is None or not frac:
            return head
        return Decimal(f"{int(head)}.{frac}")

    total, section, last = 0, 0, None
    for ch in s:
        if ch in _CN_DIGIT:
            last = _CN_DIGIT[ch]
            section = last
        elif ch in _CN_UNIT:
            u = _CN_UNIT[ch]
            if u >= 10000:
                total = (total + (section or (1 if last is None else section))) * u
                section = 0
            else:
                section = (section or 1) * u
                total += section
                section = 0
            last = None
        else:
            return None
    return Decimal(total + section) if (total or section) else None


def _fmt_num(d: Decimal) -> str:
    d = d.normalize()
    return format(d, "f") if d == d.to_integral_value() else format(d, "f")


_NUM = "[0-9一二兩两三四五六七八九十百千萬万零點点.]+"

_PIN_RE = re.compile(
    rf"({_NUM})\s*(?:線位|线位|針|针|(?:pin|p)(?![A-Za-z0-9]))", re.I)


def expand_measures_grouped(q: str) -> tuple[list[list[str]], list[tuple[int, int]]]:
    flat, spans = expand_measures(q)
    groups: list[list[str]] = []
    for cn, mult, alts, emit_mm in _LEN_UNITS:
        for m in re.finditer(rf"({_NUM})\s*{re.escape(cn)}", q):
            n = cn_number(m.group(1))
            if n is None:
                continue
            g = [f"{_fmt_num(n)}{a}" for a in alts]
            mm = (n * mult).normalize()
            if emit_mm and mm == mm.to_integral_value():
                g.append(f"{_fmt_num(mm)}mm")
            groups.append(g)
    for m in _SPEED_RE.finditer(q):
        n = cn_number(m.group(1))
        if n is not None:
            groups.append([f"{_fmt_num(n)}{m.group(2).lower()}"])
    for m in re.finditer(_PIN_RE, q):
        n = cn_number(m.group(1))
        if n is not None:
            groups.append([f"{_fmt_num(n)}pin", f"{_fmt_num(n)}p"])
    for m in re.finditer(rf"({_NUM})\s*(排|列)", q):
        n = cn_number(m.group(1))
        if n is not None:
            groups.append([f"{_fmt_num(n)}x"])
    for m in re.finditer(rf"({_NUM})\s*分\s*({_NUM})", q):
        a, b = cn_number(m.group(1)), cn_number(m.group(2))
        if a is not None and b is not None:
            groups.append([f"{_fmt_num(a)}to{_fmt_num(b)}", "breakout", "splitter"])
    return groups, spans


def expand_groups(q: str) -> list[list[str]]:
    if not re.search(r"[一-鿿]", q):
        return []
    mgroups, spans = expand_measures_grouped(q)
    tgroups = expand_terms_grouped(q, spans)
    out, seen = [], set()
    for g in tgroups + mgroups:
        key = tuple(sorted(t.lower() for t in g))
        if g and key not in seen:
            seen.add(key)
            out.append(g)
    return out


def expand_measures(q: str) -> tuple[list[str], list[tuple[int, int]]]:
    out: list[str] = []
    spans: list[tuple[int, int]] = []

    def take(m: re.Match) -> None:
        spans.append((m.start(), m.end()))

    for cn, mult, alts, emit_mm in _LEN_UNITS:
        for m in re.finditer(rf"({_NUM})\s*{re.escape(cn)}", q):
            n = cn_number(m.group(1))
            if n is None:
                continue
            take(m)
            out += [f"{_fmt_num(n)}{a}" for a in alts]
            mm = (n * mult).normalize()
            if emit_mm and mm == mm.to_integral_value():
                out.append(f"{_fmt_num(mm)}mm")

    for m in _SPEED_RE.finditer(q):
        n = cn_number(m.group(1))
        if n is not None:
            take(m)
            out.append(f"{_fmt_num(n)}{m.group(2).lower()}")

    for m in _PIN_RE.finditer(q):
        n = cn_number(m.group(1))
        if n is not None:
            take(m)
            out += [f"{_fmt_num(n)}pin", f"{_fmt_num(n)}p"]
    for m in re.finditer(rf"({_NUM})\s*(排|列)", q):
        n = cn_number(m.group(1))
        if n is not None:
            take(m)
            out.append(f"{_fmt_num(n)}x")
    for m in re.finditer(rf"({_NUM})\s*分\s*({_NUM})", q):
        a, b = cn_number(m.group(1)), cn_number(m.group(2))
        if a is not None and b is not None:
            take(m)
            out += [f"{_fmt_num(a)}to{_fmt_num(b)}", "breakout", "splitter"]
    return out, spans


_TERMS_BY_LEN = sorted(TERMS, key=len, reverse=True)

_FALSE_FRIENDS = ("公司", "公差", "公制", "母公司", "線上", "线上", "排程", "排查")


def expand_terms_grouped(q: str, blocked: list[tuple[int, int]] | None = None
                         ) -> list[list[str]]:
    used: list[tuple[int, int]] = list(blocked or [])
    out: list[list[str]] = []
    for cn in _TERMS_BY_LEN:
        start = 0
        while True:
            pos = q.find(cn, start)
            if pos < 0:
                break
            end = pos + len(cn)
            if (not any(pos < ue and us < end for us, ue in used)
                    and not any(ff in q[max(0, pos - 2):end + 2] and cn in ff
                                for ff in _FALSE_FRIENDS)):
                used.append((pos, end))
                out.append(list(TERMS[cn]))
            start = end
    return out


def expand_terms(q: str, blocked: list[tuple[int, int]] | None = None) -> list[str]:
    return [t for g in expand_terms_grouped(q, blocked) for t in g]


def expand(q: str) -> list[str]:
    if not re.search(r"[一-鿿]", q):
        return []
    measures, spans = expand_measures(q)
    seen: set[str] = set()
    out: list[str] = []
    for t in expand_terms(q, spans) + measures:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


_NUMUNIT_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)([a-z/]+)$", re.I)


def tsq_fragment(term: str) -> str | None:
    t = term.strip().lower()
    if not t or not _TOKENish.fullmatch(t):
        return None
    m = _NUMUNIT_RE.match(t)
    if m:
        num, unit = m.group(1), m.group(2)
        if "/" in unit:
            return f"({num}<->{'<->'.join(p for p in unit.split('/') if p)})"
        return f"({t} | ({num}<->{unit}))"
    if "/" in t:
        parts = [p for p in t.split("/") if p]
        return f"({'<->'.join(parts)})" if len(parts) > 1 else None
    return t


_TOKENish = re.compile(r"[A-Za-z0-9][A-Za-z0-9./-]*")


def expand_tsq(terms: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        f = tsq_fragment(t)
        if f and f not in seen:
            seen.add(f)
            out.append(f)
    return out


def validate(conn) -> dict:
    targets = sorted({t.lower() for v in TERMS.values() for t in v})
    dead, alive = [], {}
    with conn.cursor() as cur:
        for t in targets:
            cur.execute(
                "[SQL 已於公開版移除]", (f"%{t}%",))
            (n,) = cur.fetchone()
            if n == 0:
                dead.append(t)
            else:
                alive[t] = n
    return {"total": len(targets), "dead": dead,
            "rare": sorted((k for k, v in alive.items() if v < 10), key=str),
            "alive": alive}
