"""時間條件解析（最近／區間／相對時間／民國年…），輸出硬性過濾與排序。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

FIELD_CREATED = "created_on"
FIELD_MODIFIED = "modified_on"

_CN_NUM = {"一": 1, "兩": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
           "半": 0}


@dataclass(frozen=True)
class TimeFilter:
    since: datetime | None = None
    until: datetime | None = None
    order: str | None = None
    want_one: bool = False
    field: str = FIELD_CREATED
    label: str = ""
    matched: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.since or self.until or self.order)

    @property
    def has_range(self) -> bool:
        return bool(self.since or self.until)

    def as_dict(self) -> dict:
        return {
            "since": self.since.isoformat() if self.since else None,
            "until": self.until.isoformat() if self.until else None,
            "order": self.order, "want_one": self.want_one,
            "field": self.field, "label": self.label,
            "matched": list(self.matched),
        }


_MONTH_TOK = r"(?:1[0-2]|0?[1-9]|十[一二]|十|[一二三四五六七八九]|元)"
_DAY_TOK = r"(?:3[01]|[12]\d|0?[1-9]|[一二三四五六七八九十]{1,3})"
_LIST_SEP = "、,，和跟及與与或"
_RANGE_SEP = "~～－—–─到至"

_MONTH_ELIDED_RE = re.compile(
    rf"(?<![0-9A-Za-z])({_MONTH_TOK})\s*([{_LIST_SEP}{_RANGE_SEP}])\s*"
    rf"(?={_MONTH_TOK}\s*月)")

_YMD_RE = re.compile(
    r"(?<![0-9A-Za-z/\-])(20\d{2})[-/](\d{1,2})(?:[-/](\d{1,2}))?"
    r"(?![0-9A-Za-z/\-])")
_CJK_DATE_RE = re.compile(rf"({_MONTH_TOK})\s*月\s*({_DAY_TOK})\s*[日號号]")
_MONTH_RE = re.compile(
    r"(?:(?<![0-9A-Za-z])(1[0-2]|0?[1-9])|(十[一二]|十|[一二三四五六七八九]|元))"
    r"\s*月份?")

_YEAR_NUM_RE = re.compile(
    r"(?<![0-9A-Za-z/\-])(?:(20\d{2})\s*年|"
    rf"(20\d{{2}})(?![0-9A-Za-z/\-])\s*(?=的|[Qq上下第]|{_MONTH_TOK}\s*月))")
_AMOUNT_HEAD_RE = re.compile(
    r"(大於|大于|小於|小于|超過|超过|低於|低于|不到|少於|多於|高於|"
    r"以上|以下|[<>=＞＜])\s*$")
_YEAR_EN_RE = re.compile(
    r"(?:in|of|during|year|since|from|after|before|until)\s+(20\d{2})"
    r"(?![0-9A-Za-z/\-])", re.I)
_ROC_YEAR_RE = re.compile(r"民國\s*(\d{2,3})\s*年")

_QUARTER_RE = re.compile(
    r"(?:第\s*([1-4一二三四])\s*季(?:度)?|(?<![A-Za-z])Q\s*([1-4])(?![0-9]))", re.I)
_HALF_RE = re.compile(r"(上|下)\s*半年")
_YEAR_PART_RE = re.compile(r"年\s*(初|中|底|末)")
_YEAR_PART_MONTHS = {"初": (1, 3), "中": (5, 8), "底": (10, 12), "末": (10, 12)}

_SPAN_CN_RE = re.compile(
    r"(?:最近|近|過去|过去|過往|這|这|(?<![年月])前)\s*"
    r"(\d{1,3}|[一兩二三四五六七八九十半]{1,3})\s*"
    r"(天|日|週|周|星期|個月|个月|月|季|年)(?!\s*(?:以前|之前|前))")
_SPAN_EN_RE = re.compile(
    r"(?:past|last|recent|within(?:\s+the)?(?:\s+last)?)\s+"
    r"(\d{1,3})\s*(day|week|month|quarter|year)s?", re.I)
_AGO_RE = re.compile(
    r"(?<![0-9A-Za-z])(\d{1,3}|[一兩二三四五六七八九十半]{1,3})\s*"
    r"(天|日|週|周|星期|個月|个月|月|季|年)\s*(?:以前|之前|前)")

_AFTER_TAIL_RE = re.compile(
    r"^\s*(以後|之後|以后|以来|以來|起|後|后|開始|开始|onwards?)", re.I)
_AFTER_HEAD_RE = re.compile(r"(自|從|从|after|since|from)\s*$", re.I)
_BEFORE_TAIL_RE = re.compile(
    r"^\s*(以前|之前|以往|前(?![\d一二三四五六七八九十半兩]))", re.I)
_BEFORE_HEAD_RE = re.compile(r"(截至|直到|before|until|up\s*to|prior\s+to)\s*$", re.I)

_THIS_YEAR_RE = re.compile(r"(今年|本年度?|this\s+year)", re.I)
_LAST_YEAR_RE = re.compile(r"(去年|上年度?|舊年|last\s+year)", re.I)
_YEAR_B4_LAST_RE = re.compile(r"(前年)")
_THIS_MONTH_RE = re.compile(r"(本月|當月|这个?月|這個?月|this\s+month)", re.I)
_LAST_MONTH_RE = re.compile(r"(上個?月|上月|前一個?月|last\s+month)", re.I)
_THIS_WEEK_RE = re.compile(r"(本週|本周|這週|这周|這個?禮拜|这个?礼拜|this\s+week)", re.I)
_LAST_WEEK_RE = re.compile(r"(上週|上周|上個?禮拜|上个?礼拜|last\s+week)", re.I)
_TODAY_RE = re.compile(r"(今天|今日|today)", re.I)
_YESTERDAY_RE = re.compile(r"(昨天|昨日|yesterday)", re.I)
_B4_YESTERDAY_RE = re.compile(r"(前天|前日)")

_NEWEST_RE = re.compile(
    r"(最近|最新|最後|最末|近期|新的?一|latest|most\s+recent|newest|"
    r"(?<![A-Za-z])last(?![A-Za-z])|(?<![A-Za-z])recent(?![A-Za-z]))", re.I)
_OLDEST_RE = re.compile(
    r"(最早|最舊|最旧|最初|第一(?:份|張|张|筆|笔|次|個|个|張單)|"
    r"oldest|earliest|(?<![A-Za-z])first(?![A-Za-z]))", re.I)

_ONE_RE = re.compile(
    r"((?<!哪)一\s*(?:份|張|张|筆|笔|次|個|个|條|条|單|单)|"
    r"(?:那|這|这|該|该)\s*(?:一\s*)?(?:份|張|张|筆|笔|次|個|个|條|条|單|单)|"
    r"單一|唯一|only\s+one|single|just\s+one|the\s+latest\s+one)", re.I)


def _int(tok: str) -> int:
    if tok.isdigit():
        return int(tok)
    if tok == "半":
        return 0
    if tok in _CN_NUM:
        return _CN_NUM[tok]
    if tok.startswith("十"):
        return 10 + _CN_NUM.get(tok[1:], 0)
    if tok.endswith("十"):
        return _CN_NUM.get(tok[:-1], 1) * 10
    if "十" in tok:
        a, b = tok.split("十", 1)
        return _CN_NUM.get(a, 1) * 10 + _CN_NUM.get(b, 0)
    return 0


def _span_days(n: int, unit: str, half: bool) -> int:
    per = {"天": 1, "日": 1, "週": 7, "周": 7, "星期": 7,
           "個月": 30, "个月": 30, "月": 30, "季": 91, "年": 365,
           "day": 1, "week": 7, "month": 30, "quarter": 91, "year": 365}
    d = per.get(unit, 0)
    if half:
        return {"年": 180, "季": 45, "個月": 15, "个月": 15, "月": 15,
                "週": 3, "周": 3, "星期": 3}.get(unit, max(1, d // 2))
    return d * max(1, n)


def _utc(y: int, m: int = 1, d: int = 1) -> datetime:
    return datetime(y, m, d, tzinfo=timezone.utc)


def _month_end(y: int, m: int) -> datetime:
    return _utc(y + 1, 1) if m == 12 else _utc(y, m + 1)


def _month_int(tok: str) -> int:
    if tok.isdigit():
        return int(tok)
    return {"元": 1, "十": 10, "十一": 11, "十二": 12}.get(tok, _CN_NUM.get(tok, 0))


def _blank(s: str, start: int, end: int) -> str:
    return s[:start] + " " * (end - start) + s[end:]


def _expand_months(text: str) -> str:
    for _ in range(12):
        new = _MONTH_ELIDED_RE.sub(r"\1月\2", text, count=1)
        if new == text:
            break
        text = new
    return text


def _years_in(text: str, now: datetime) -> list[tuple[int, int, int]]:
    out: list[tuple[int, int, int]] = []
    for m in _ROC_YEAR_RE.finditer(text):
        out.append((m.start(), m.end(), 1911 + int(m.group(1))))
    for m in _YEAR_NUM_RE.finditer(text):
        if m.group(2) and _AMOUNT_HEAD_RE.search(text[max(0, m.start() - 6):m.start()]):
            continue
        out.append((m.start(), m.end(), int(m.group(1) or m.group(2))))
    for m in _YEAR_EN_RE.finditer(text):
        out.append((m.start(1), m.end(1), int(m.group(1))))
    for rx, delta in ((_THIS_YEAR_RE, 0), (_LAST_YEAR_RE, -1), (_YEAR_B4_LAST_RE, -2)):
        for m in rx.finditer(text):
            out.append((m.start(), m.end(), now.year + delta))
    out.sort()
    keep: list[tuple[int, int, int]] = []
    for s, e, y in out:
        if keep and s < keep[-1][1]:
            continue
        keep.append((s, e, y))
    return keep


def _year_at(pos: int, years: list, now: datetime) -> int:
    prev = [y for (s, _e, y) in years if s <= pos]
    if prev:
        return prev[-1]
    if years:
        return years[0][2]
    return now.year


def _periods(text: str, now: datetime):
    rest = text
    periods: list[tuple[datetime, datetime, str]] = []
    spans: list[tuple[int, int, str]] = []
    years = _years_in(text, now)

    def take(m, s, u, label):
        nonlocal rest
        periods.append((s, u, label))
        spans.append((m.start(), m.end(), m.group(0)))
        rest = _blank(rest, m.start(), m.end())

    for m in _YMD_RE.finditer(text):
        y, mo = int(m.group(1)), int(m.group(2))
        day = int(m.group(3)) if m.group(3) else None
        if not 1 <= mo <= 12:
            continue
        if day and 1 <= day <= 31:
            take(m, _utc(y, mo, day), _utc(y, mo, day) + timedelta(days=1),
                 f"{y}-{mo:02d}-{day:02d} 當天")
        else:
            take(m, _utc(y, mo), _month_end(y, mo), f"{y} 年 {mo} 月")

    for m in _CJK_DATE_RE.finditer(rest):
        mo, day = _month_int(m.group(1)), _int(m.group(2))
        if 1 <= mo <= 12 and 1 <= day <= 31:
            y = _year_at(m.start(), years, now)
            take(m, _utc(y, mo, day), _utc(y, mo, day) + timedelta(days=1),
                 f"{y}-{mo:02d}-{day:02d} 當天")

    for m in _QUARTER_RE.finditer(rest):
        q = _int(m.group(1) or m.group(2))
        if 1 <= q <= 4:
            y = _year_at(m.start(), years, now)
            take(m, _utc(y, q * 3 - 2), _month_end(y, q * 3), f"{y} 年 第 {q} 季")
    for m in _HALF_RE.finditer(rest):
        y = _year_at(m.start(), years, now)
        lo, hi = (1, 6) if m.group(1) == "上" else (7, 12)
        take(m, _utc(y, lo), _month_end(y, hi), f"{y} 年{m.group(1)}半年")
    for m in _YEAR_PART_RE.finditer(rest):
        y = _year_at(m.start(), years, now)
        lo, hi = _YEAR_PART_MONTHS[m.group(1)]
        take(m, _utc(y, lo), _month_end(y, hi), f"{y} 年{m.group(1)}（{lo}-{hi} 月）")

    for m in _MONTH_RE.finditer(rest):
        mo = _month_int(m.group(1) or m.group(2))
        if 1 <= mo <= 12:
            y = _year_at(m.start(), years, now)
            take(m, _utc(y, mo), _month_end(y, mo), f"{y} 年 {mo} 月")

    if not periods:
        for s, e, y in years:
            periods.append((_utc(y), _utc(y + 1), f"{y} 年"))
            spans.append((s, e, text[s:e]))
            rest = _blank(rest, s, e)
    else:
        for s, e, _y in years:
            rest = _blank(rest, s, e)

    if not periods:
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        monday = today - timedelta(days=today.weekday())
        cases = [
            (_THIS_MONTH_RE, lambda: (_utc(now.year, now.month),
                                      _month_end(now.year, now.month),
                                      f"本月（{now.year}-{now.month:02d}）")),
            (_LAST_MONTH_RE, lambda: (
                _utc(*_prev_month(now)), _month_end(*_prev_month(now)),
                "上個月（%d-%02d）" % _prev_month(now))),
            (_THIS_WEEK_RE, lambda: (monday, monday + timedelta(days=7), "本週")),
            (_LAST_WEEK_RE, lambda: (monday - timedelta(days=7), monday, "上週")),
            (_TODAY_RE, lambda: (today, today + timedelta(days=1), "今天")),
            (_YESTERDAY_RE, lambda: (today - timedelta(days=1), today, "昨天")),
            (_B4_YESTERDAY_RE, lambda: (today - timedelta(days=2),
                                        today - timedelta(days=1), "前天")),
        ]
        for rx, mk in cases:
            m = rx.search(rest)
            if m:
                s, u, label = mk()
                take(m, s, u, label)
                break

    return periods, spans, rest


def _prev_month(now: datetime) -> tuple[int, int]:
    return (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)


def parse(q: str, now: datetime | None = None) -> TimeFilter | None:
    text = (q or "").strip()
    if not text:
        return None
    now = now or datetime.now(timezone.utc)

    norm = _expand_months(text)

    since: datetime | None = None
    until: datetime | None = None
    bits: list[str] = []
    matched: list[str] = []
    spans: list[tuple[int, int, str]] = []
    open_ok = True

    rest = norm

    m = _SPAN_CN_RE.search(rest) or _SPAN_EN_RE.search(rest)
    ago = None if m else _AGO_RE.search(rest)
    if m:
        tok, unit = m.group(1), m.group(2)
        days = _span_days(_int(tok), unit, half=(tok == "半"))
        if days:
            since = now - timedelta(days=days)
            bits.append(f"最近 {days} 天內")
            matched.append(m.group(0))
            rest = _blank(rest, m.start(), m.end())
            open_ok = False
    elif ago:
        days = _span_days(_int(ago.group(1)), ago.group(2),
                          half=(ago.group(1) == "半"))
        if days:
            until = now - timedelta(days=days)
            bits.append(f"{days} 天以前")
            matched.append(ago.group(0))
            rest = _blank(rest, ago.start(), ago.end())
            open_ok = False

    if since is None and until is None:
        periods, spans, rest = _periods(rest, now)
        if periods:
            periods.sort()
            since = periods[0][0]
            until = max(p[1] for p in periods)
            labels = [p[2] for p in periods]
            bits.append(labels[0] if len(labels) == 1
                        else f"{labels[0]} 到 {labels[-1]}")
            matched.extend(s[2] for s in spans)

    if open_ok and (since or until) and spans:
        s0, e0, _raw = max(spans, key=lambda t: t[1])
        tail, head = norm[e0:e0 + 6], norm[max(0, s0 - 8):s0]
        if _AFTER_TAIL_RE.search(tail) or _AFTER_HEAD_RE.search(head):
            until = None
            bits[-1] = bits[-1] + "起（含）之後"
        elif _BEFORE_TAIL_RE.search(tail) or _BEFORE_HEAD_RE.search(head):
            until, since = since, None
            bits[-1] = bits[-1] + "之前"

    order = None
    want_one = False
    if _NEWEST_RE.search(rest):
        order = "desc"
        matched.append(_NEWEST_RE.search(rest).group(0))
        want_one = bool(_ONE_RE.search(norm))
        bits.append("最新的排前面" + ("，只取 1 筆" if want_one else ""))
    elif _OLDEST_RE.search(rest):
        order = "asc"
        matched.append(_OLDEST_RE.search(rest).group(0))
        want_one = bool(_ONE_RE.search(norm)) or "第一" in rest
        bits.append("最舊的排前面" + ("，只取 1 筆" if want_one else ""))

    if not (since or until or order):
        return None

    fld = FIELD_CREATED
    if re.search(r"(更新|修改|改過|异动|異動|updated|modified)", norm):
        fld = FIELD_MODIFIED
        bits.append("依最後修改時間")
    return TimeFilter(
        since=since, until=until, order=order, want_one=want_one, field=fld,
        label="；".join(bits), matched=tuple(matched))


def sql(tf: TimeFilter | None, col_prefix: str = "") -> tuple[list[str], list]:
    if not tf or not tf.has_range:
        return [], []
    col = f"{col_prefix}{tf.field}"
    where, args = [], []
    if tf.since:
        where.append("[SQL 已於公開版移除]")
        args.append(tf.since)
    if tf.until:
        where.append("[SQL 已於公開版移除]")
        args.append(tf.until)
    return where, args


def order_by(tf: TimeFilter | None, col_prefix: str = "") -> str:
    if not tf or not tf.order:
        return ""
    col = f"{col_prefix}{tf.field}"
    direction = "DESC" if tf.order == "desc" else "ASC"
    return f"{col} {direction} NULLS LAST"


def sort_rows(rows: list[dict], tf: TimeFilter | None) -> list[dict]:
    if not tf or not tf.order:
        return rows
    fld = tf.field
    far_past = datetime.min.replace(tzinfo=timezone.utc)
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    miss = far_past if tf.order == "desc" else far_future
    return sorted(rows, key=lambda r: r.get(fld) or miss,
                  reverse=(tf.order == "desc"))


def describe(tf) -> list[list[str]]:
    if not tf:
        return []
    if isinstance(tf, dict):
        label, field_ = tf.get("label") or "", tf.get("field") or FIELD_CREATED
    else:
        label, field_ = tf.label, tf.field
    if not label:
        return []
    fld = "建檔時間" if field_ == FIELD_CREATED else "最後修改時間"
    return [["時間條件", f"{label}（依{fld}）"]]
