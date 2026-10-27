"""回答的確定性事實核對：數字、編號、檔名都必須在檢索結果裡找得到。"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .material import PART_RE
from .product import PRODUCT_RE


@dataclass(frozen=True)
class Violation:
    code: str
    detail: str
    fix: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.detail}"


_PLACEHOLDER_RES = (
    (re.compile(r"(?<![A-Za-z0-9])[XxＸ]{1,3}\s*(USD|美元|RMB|人民幣|人民币|"
                r"元|塊|美金|NTD|台幣)"),
     "用 X 代替金額"),
    (re.compile(r"(?:為|是|＝|=|：|:)\s*[XxNn]{1,3}(?![A-Za-z0-9一-鿿])"),
     "用 X／N 代替數值"),
    (re.compile(r"[<＜【\[]\s*(數字|数字|金額|金额|價格|价格|待填|待補|待补|"
                r"某個|某数|number|amount|value|price|TBD|todo)\s*[>＞】\]]", re.I),
     "留了角括號佔位符"),
    (re.compile(r"(?<![A-Za-z0-9])(TBD|TODO|FIXME|XXX+|N/?A待|待確認數字)"
                r"(?![A-Za-z0-9])", re.I),
     "留了 TBD／TODO"),
    (re.compile(r"_{3,}|\{\{|\}\}"), "留了底線或樣板括號"),
)

_RFQ_RE = re.compile(
    r"(?<![A-Za-z0-9])(RF[QSD][-\s]?\d{6,13}(?:[-\s]?\d{1,3})?)(?![A-Za-z0-9])",
    re.I)
_FILE_RE = re.compile(r"([\w一-鿿][\w一-鿿 .()（）_-]{0,60}"
                      r"\.(?:pdf|xls|xlsx|xlsm|csv))", re.I)

_NUM_RE = re.compile(r"(?<![A-Za-z0-9._-])(\d[\d,]*(?:\.\d+)?)(?![\d]*[A-Za-z])")

_UNIT_NEAR_RE = re.compile(
    r"^\s*(USD|RMB|美元|人民幣|人民币|元|美金|NTD|台幣|%|％|"
    r"pcs|PCS|M(?![A-Za-z])|mm|MM|秒|小時|小时|hr|Hr|/pcs|/M|"
    r"份|筆|笔|張|张|項|项|欄|栏|列|家|款|種|种|顆|颗)", re.I)
_CURRENCY_BEFORE_RE = re.compile(
    r"(USD|RMB|美元|人民幣|人民币|NT\$|\$|單價|单价|總價|总价|成本|價格|价格|金額)"
    r"\s*$", re.I)

_NOTFOUND_RE = re.compile(
    r"(查無|查无|找不到|沒有找到|没有找到|無此|无此|不存在|沒有任何|没有任何|"
    r"查不到|一張都沒有|一筆都沒有|"
    r"no\s+(?:such|match|record|result)|not\s+found)", re.I)

_NOTFOUND_LOOSE_RE = re.compile(
    _NOTFOUND_RE.pattern[:-1] + r"|沒有|没有|未(?:匯入|录入|收錄)|尚未)", re.I)

_UNIVERSAL_RE = re.compile(
    r"(都|皆|全部|均|所有|每[一]?[張张筆笔份項项單单]?|任何|"
    r"一[張张筆笔份項项]?[都也])")
_COLLECTIVE_SUBJ_RE = re.compile(
    r"(檢索結果|检索结果|查詢結果|查询结果|這些|这些|那些|"
    r"這[幾几][張张筆笔份項项單单]|相關單|相关单|相關的?單|"
    r"上述|以上|下列|列出的?|清單裡?|清单里?|全部結果|整批)")


def _is_sweeping(clause: str) -> bool:
    return bool(_UNIVERSAL_RE.search(clause)
                or _COLLECTIVE_SUBJ_RE.search(clause))

_DOC_LINE_RE = re.compile(
    r"[-•]\s*((?:RFQ|RFS|RFD)[-\s]?\d{6,13}(?:[-\s]?\d{1,3})?)\s*[：:]([^\n]*)",
    re.I)

_NO_DOC_RE = re.compile(
    r"(?:沒有|没有|無|无|未|不)[\s*]{0,4}"
    r"(?:掛|挂|附上?|提供|含|帶|带|有|見到|见到|找到|查到)?"
    r"[\s*]{0,4}[^\n。；;]{0,14}?(?:附件|原檔|原档)")
_NO_DOC_REV_RE = re.compile(
    r"(?:附件|原檔|原档)[^\n。；;]{0,10}?(?:沒有|没有|無|无|未有)")
_HAS_DOC_NEARBY_RE = re.compile(r"有[^\n]{0,6}附件|\.(?:pdf|xlsx?|xlsm|csv)", re.I)

_EXCEPTION_RE = re.compile(r"(除了|除|其餘|其余|只有|僅|仅|唯一|"
                           r"之外|以外|另外|但|不過|不过)")


def _sweeping_no_doc(text: str) -> str | None:
    for clause in re.split(r"[\n。；;]", text or ""):
        if not _is_sweeping(clause):
            continue
        if _NO_DOC_RE.search(clause) or _NO_DOC_REV_RE.search(clause):
            return clause.strip()
    return None


def _ids_with_doc(text: str) -> set[str]:
    out: set[str] = set()
    for m in _DOC_LINE_RE.finditer(text or ""):
        if _FILE_RE.search(m.group(2)):
            out.add(re.sub(r"\s+", "", m.group(1)).upper())
    return out


def _blanket_denies_doc(text: str, rid: str) -> bool:
    for line in (text or "").splitlines():
        if rid in _ids(line) and _HAS_DOC_NEARBY_RE.search(line):
            return False
    return True


def _norm_num(s: str) -> str:
    s = s.replace(",", "")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _numbers(text: str) -> set[str]:
    return {_norm_num(m.group(1)) for m in _NUM_RE.finditer(text or "")}


def _data_numbers(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for m in _NUM_RE.finditer(text or ""):
        raw = m.group(1)
        head = raw.split(".")[0].replace(",", "")
        after = text[m.end():m.end() + 8]
        before = text[max(0, m.start() - 8):m.start()]
        looks_like_data = (
            "." in raw
            or len(head) >= 4
            or bool(_UNIT_NEAR_RE.match(after))
            or bool(_CURRENCY_BEFORE_RE.search(before))
        )
        if looks_like_data:
            out.append((_norm_num(raw), raw))
    return out


def _grounded_number(val: str, allowed: set[str]) -> bool:
    if val in allowed:
        return True
    if "." not in val:
        for a in allowed:
            if a.split(".")[0] == val:
                return True
            if "." in a:
                try:
                    if str(round(float(a))) == val:
                        return True
                except ValueError:
                    pass
        return False
    digits = len(val.split(".")[1])
    for a in allowed:
        if "." not in a:
            continue
        try:
            f = float(a)
        except ValueError:
            continue
        if f"{round(f, digits):.{digits}f}".rstrip("0").rstrip(".") == val:
            return True
        cut = a.split(".")
        if len(cut[1]) >= digits and f"{cut[0]}.{cut[1][:digits]}".rstrip("0").rstrip(".") == val:
            return True
    return False


def _ids(text: str) -> set[str]:
    out: set[str] = set()
    for m in _RFQ_RE.finditer(text or ""):
        out.add(re.sub(r"\s+", "", m.group(1)).upper())
    for m in PART_RE.finditer(text or ""):
        out.add(m.group(1).upper())
    for m in PRODUCT_RE.finditer(text or ""):
        out.add(m.group(1).upper())
    return out


def _files(text: str) -> set[str]:
    return {m.group(1).strip().lower() for m in _FILE_RE.finditer(text or "")}


def _file_grounded(name: str, allowed: set[str]) -> bool:
    n = re.sub(r"\s+", "", name)
    return any(n.endswith(a) or a.endswith(n)
               for a in (re.sub(r"\s+", "", x) for x in allowed))


def check(answer: str, context: str, question: str = "",
          has_data: bool | None = None,
          extra_allowed: tuple[str, ...] = ()) -> list[Violation]:
    text = (answer or "").strip()
    if not text:
        return [Violation("empty", "回答是空的",
                          "你沒有產出任何文字。請根據檢索結果直接把結論寫出來。")]

    haystack = "\n".join([context or "", question or "", *extra_allowed])
    out: list[Violation] = []

    for rx, why in _PLACEHOLDER_RES:
        m = rx.search(text)
        if m and m.group(0).strip() in haystack:
            continue
        if m:
            out.append(Violation(
                "placeholder", f"{why}：「{m.group(0).strip()}」",
                f"你的回答裡有「{m.group(0).strip()}」這種佔位符。"
                f"檢索結果裡有真正的數字就照抄，沒有就明說那一欄沒有資料，"
                f"**絕對不可以用 X／N／TBD 代替一個數字**。"))
            break

    allowed_ids = _ids(haystack)
    bad_ids = sorted(i for i in _ids(text) if i not in allowed_ids)
    if bad_ids:
        out.append(Violation(
            "ungrounded_id",
            "回答裡的編號／料號不在檢索結果裡：" + "、".join(bad_ids[:5]),
            f"你提到了 {'、'.join(bad_ids[:5])}，但檢索結果與使用者的問句裡"
            f"都沒有這些編號／料號。只能引用檢索結果裡真的出現過的編號，"
            f"不要自己組一個看起來合理的。"))

    allowed_files = _files(haystack)
    bad_files = sorted(f for f in _files(text)
                       if not _file_grounded(f, allowed_files))
    if bad_files:
        out.append(Violation(
            "ungrounded_id",
            "回答裡的檔名不在檢索結果裡：" + "、".join(bad_files[:3]),
            f"你提到了檔案 {'、'.join(bad_files[:3])}，檢索結果裡沒有這份附件。"
            f"附件檔名要照抄檢索結果給的那一個，連更新時間一起講。"))

    allowed_nums = _numbers(haystack)
    id_digits = "".join(re.findall(r"\d+", haystack))
    bad_nums = [raw for val, raw in _data_numbers(text)
                if not _grounded_number(val, allowed_nums)
                and not (val.isdigit() and len(val) >= 5 and val in id_digits)]
    if bad_nums:
        seen, uniq = set(), []
        for n in bad_nums:
            if n not in seen:
                seen.add(n)
                uniq.append(n)
        out.append(Violation(
            "ungrounded_number",
            "回答裡的數字在檢索結果裡找不到：" + "、".join(uniq[:6]),
            f"這幾個數字在檢索結果裡不存在：{'、'.join(uniq[:6])}。"
            f"每一個數字都必須是檢索結果裡照抄的（可以四捨五入，不可以換一個）。"
            f"算不出來或資料沒給的，就說那一項沒有資料。"))

    if has_data is True:
        head = text.lstrip().splitlines()[0][:80]
        if _NOTFOUND_RE.search(head):
            out.append(Violation(
                "contradiction",
                f"查到了資料，開頭卻說查無：「{head.splitlines()[0][:40]}」",
                "檢索結果裡**有**資料，你卻在開頭說查無。"
                "先講查到了什麼，再說明哪一部分沒有資料。"
                "使用者問的東西如果換了一種識別碼查到（例如產品料號不是 RFQ 編號），"
                "要說「這是產品料號，查到 N 張相關的單」，不是「查無編號」。"))
    elif has_data is False:
        if not _NOTFOUND_LOOSE_RE.search(text) and (allowed_ids & _ids(text)
                                                    or _data_numbers(text)):
            out.append(Violation(
                "contradiction",
                "什麼都沒查到，回答卻給了具體內容",
                "檢索結果是「查無」。請照實說查不到，並把檢索結果那句說明"
                "（已索引幾份、試過哪幾種比對）講完，不要自己補內容。"))

    if has_data is True:
        doc_ids = _ids_with_doc(context)
        clause = _sweeping_no_doc(text)
        if doc_ids and clause:
            contradicted = sorted(i for i in (doc_ids & _ids(text))
                                  if _blanket_denies_doc(text, i))
            if (not contradicted and not _files(text)
                    and not _EXCEPTION_RE.search(clause)):
                contradicted = sorted(doc_ids)
            if contradicted:
                out.append(Violation(
                    "contradiction",
                    "回答說都沒有成本分析附件，但檢索結果裡這幾筆其實有："
                    + "、".join(contradicted[:3]),
                    f"檢索結果裡 {'、'.join(contradicted[:3])} 其實有成本分析"
                    f"附件（檔名與更新時間都列在檢索結果裡），不可以說成"
                    f"「都沒有」。先講清楚哪一筆有附件，其餘沒有的再另外講，"
                    f"不要用「都沒有」概括帶過。"))

    return out


GENERIC_FIX: dict[str, str] = {
    "placeholder":
        "不可以用 X／N／TBD／<金額> 代替數字。檢索結果沒有那個值，"
        "就說「這一欄沒有資料」。",
    "ungrounded_number":
        "每一個數字都要是檢索結果裡照抄的（可以四捨五入，不可以換一個）。"
        "算不出來或資料沒給的，就說那一項沒有資料 —— 尤其不要自己加總或換算。",
    "ungrounded_id":
        "編號、料號、附件檔名只能引用檢索結果裡真的出現過的，"
        "而且要照抄原本的寫法。不要為了讓答案完整而組一個看起來合理的。",
    "contradiction":
        "檢索結果有資料就不要在開頭說查無。先講查到了什麼，"
        "再說明哪一部分沒有資料；查無的時候也只講查無，不要補內容。",
    "empty":
        "一定要產出文字。資料不足就說明缺哪一塊，不要交白卷。",
}


def generic_fix(v: Violation) -> str:
    return GENERIC_FIX.get(v.code, v.fix)


def summarize(violations: list[Violation]) -> str:
    if not violations:
        return ""
    lines = ["你上一版的回答沒有通過事實核對，請重寫。問題如下："]
    lines += [f"{i}. {v.fix}" for i, v in enumerate(violations, 1)]
    lines.append("重寫時的硬規則：只能使用「檢索結果」裡出現過的數字、編號、"
                 "檔名。檢索結果沒有的東西一律說「資料裡沒有」。"
                 "不要為了讓答案完整而補任何內容。")
    return "\n".join(lines)
