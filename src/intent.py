"""問句分流（發生在檢索之前）：識別碼、時間條件、估價、自我說明；ROUTES 也是能力說明的來源。"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .material import PART_RE
from .product import PRODUCT_RE, candidates as _product_candidates
from . import timefilter

KIND_MATERIAL = "material"
KIND_PRODUCT = "product"
KIND_RFQ = "rfq"
KIND_SEARCH = "search"
KIND_ESTIMATE = "estimate"
KIND_CAPABILITIES = "capabilities"
KIND_ARCHITECTURE = "architecture"

_HELP_RE = re.compile(
    r"(你可以做什麼|你能做什麼|能做什麼|可以做什麼|會做什麼|"
    r"有什麼功能|有哪些功能|功能有哪些|怎麼用|如何使用|使用說明|"
    r"what\s+can\s+you\s+do|what\s+do\s+you\s+do|your\s+capabilit|"
    r"(?<![A-Za-z0-9])(?:help|usage)(?![A-Za-z0-9]))", re.I)

_ARCH_RE = re.compile(
    r"(技術架構|技术架构|系統架構|系统架构|架構說明|架构说明|架構文件|架构文件|"
    r"架構圖|架构图|檢索機制|检索机制|資料流程|资料流程|技術文件|技术文件|"
    r"technical\s*architecture|system\s*architecture|architecture\s*doc)", re.I)

_RFQ_RE = re.compile(
    r"(?<![A-Za-z0-9])(RF[QSD])[-\s]?(\d{6,13})(?:[-\s]?(\d{1,3}))?(?![A-Za-z0-9])",
    re.I)

_FORM_RE = re.compile(
    r"(綠框|绿框|黃框|黄框"
    r"|原幣成本|原币成本|原幣欄位|表單欄位|表单欄位|成本欄位|成本栏位"
    r"|USD\s*欄位|USD\s*栏位|美元欄位"
    r"|green\s*box|yellow\s*box"
    r"|original\s*currency\s*cost|local\s*currency\s*cost"
    r"|usd\s*(field|column)s?"
    r"|form\s*cost\s*(field|column)s?)",
    re.I)

_LIST_RE = re.compile(
    r"(有哪些|哪一些|列出|列一下|清單|清单|全部|所有|相關|相关|關聯|关联|"
    r"類似|类似|相似|附件|檔案|文件|幾筆|几笔"
    r"|list|all\s|which|show\s*(me\s*)?all)", re.I)

_SIMILAR_RE = re.compile(
    r"(類似|类似|相似|相近|同系列|同一族|開頭|开头"
    r"|similar|related\s*part|same\s*(family|series)|starts?\s*with)", re.I)

_ESTIMATE_RE = re.compile(
    r"(大概.{0,4}多少錢?|大約.{0,4}多少錢?|差不多.{0,4}多少錢?|"
    r"這類.{0,6}多少錢?|這種.{0,6}多少錢?|類似.{0,6}多少錢?|"
    r"成本區間|價格區間|价格区间|報價區間|價格帶|行情|"
    r"估價|估算|估個|估一下|估.{0,3}(?:區間|價格|成本|多少)|"
    r"抓.{0,3}(?:區間|價格|成本)|"
    r"ballpark|price\s*range|cost\s*range|"
    r"rough(?:ly)?\s*(?:cost|price|estimate)|"
    r"how\s*much.{0,20}(?:usually|typically|roughly|ballpark))", re.I)

_EST_HEAD_RE = re.compile(
    r"^(?:[\s,，、。．？?！!:：~～-]|"
    r"請問|想請問|我想問|想問|幫我|麻煩|一下|"
    r"estimate|roughly|ballpark|"
    r"(?:the\s+)?(?:cost|price)\s+(?:of|for)\s+(?:an?\s+|the\s+)?)+", re.I)
_EST_TAIL_RE = re.compile(
    r"(?:這類|這種|這樣的?|那類|那種|類似的?|像這樣|"
    r"以前|過去|之前|歷史上|通常|一般|大概|大約|差不多|"
    r"成本|價格|價錢|報價|多少錢|錢|"
    r"的|[\s,，、。．？?！!:：~～-])+$")

_MAT_DETAIL_RE = re.compile(
    r"(材料明細|材料明细|材料清單|材料清单|材料列表|用料|物料|BOM|"
    r"哪些材料|哪些料|用了.{0,4}料|逐項材料|逐项材料|材料組成"
    r"|material\s*(breakdown|list|detail)s?|bill\s*of\s*materials"
    r"|materials?\s*used|what\s*materials)", re.I)

_COST_DOC_RE = re.compile(
    r"(成本分析|成本报价|成本報價|報價分析|报价分析|"
    r"cost\s*(analysis|sheet|breakdown\s*sheet)|costing\s*sheet)", re.I)

_LABOR_RE = re.compile(
    r"(人工成本|人力成本|人工明細|人工明细|人工費|人工费|"
    r"工時成本|工时成本|預估工站|预估工站|預計工站|組立總工時|组立总工时|"
    r"PPH|人工效率|人力效率|組裝|组装|委外|過爐|过炉|"
    r"labor\s*cost|labour\s*cost|assembly\s*(cost|labor|labour)|"
    r"man[\s-]*hour|工時|工时"
    r"|(?<![A-Za-z0-9])SMT(?![A-Za-z0-9]))", re.I)

_COST_DOC_HEADER_RE = re.compile(
    r"(Part\s*Name|PartName|品名|產品名稱|产品名称|"
    r"報價原因|报价原因|量[產产]日期|"
    r"Cus\s*\.?\s*P\s*/\s*N|ACME\s*(?:P\s*/\s*N|Part\s*No)|"
    r"客[戶户]料號|客[戶户]料号|表頭|表头)", re.I)

_LABOR_DETAIL_RE = re.compile(
    r"(人工明細|人工明细|預估工站|预估工站|預計工站|工站|"
    r"組立總工時|组立总工时|PPH|人工效率|人力效率|工時成本|工时成本|"
    r"labor\s*(breakdown|detail)s?|man[\s-]*hour)", re.I)

_KIND_RE = re.compile(r"(?<![A-Za-z0-9])(RFQ|RFS|RFD)(?![A-Za-z0-9])", re.I)


def estimate_spec(q: str) -> str:
    text = (q or "").strip()
    m = _ESTIMATE_RE.search(text)
    if not m:
        return _est_trim(text)
    return _est_trim(text[:m.start()]) or _est_trim(text[m.end():])


def _est_trim(s: str) -> str:
    return _EST_TAIL_RE.sub("", _EST_HEAD_RE.sub("", (s or "").strip())).strip()


_PN_TAIL_RE = re.compile(
    r"(?:成本分析表?|成本明細|成本明细|材料明細|材料明细|人工成本|成本|報價|报价|"
    r"Part\s*Name|PartName|品名|表頭|表头|附件|原檔|原档|PDF|明細|明细|"
    r"是什麼|是什么|是多少|有哪些|多少錢|多少钱|查得到嗎|查得到吗|"
    r"這份|这份|那份|這張|这张|的|之|"
    r"[\s,，、。．？?！!:：~～-])+$", re.I)
_PN_HEAD_RE = re.compile(
    r"^(?:請問|请问|想請問|我想問|想問|幫我|帮我|麻煩|麻烦|給我|给我|查一下|查|"
    r"[\s,，、。．？?！!:：~～-])+", re.I)


def part_name_query(q: str) -> str:
    return _PN_TAIL_RE.sub("", _PN_HEAD_RE.sub("", (q or "").strip())).strip()


def parse(q: str, now=None) -> dict:
    text = q or ""

    rfq_nos: list[str] = []
    for m in _RFQ_RE.finditer(text):
        kind, mid, seq = m.group(1).upper(), m.group(2), m.group(3)
        rfq_nos.append(f"{kind}-{mid}-{seq.zfill(3)}" if seq else f"{kind}-{mid}")

    rest = _RFQ_RE.sub(" ", text)
    product_nos = _product_candidates(rest)
    rest_no_prod = PRODUCT_RE.sub(" ", rest)
    material_nos = [m.group(1).upper() for m in PART_RE.finditer(rest_no_prod)]

    want_form = bool(_FORM_RE.search(text))
    want_cost_doc = bool(_COST_DOC_RE.search(text))
    want_labor = bool(_LABOR_RE.search(text))
    want_header = bool(_COST_DOC_HEADER_RE.search(text))
    want_material_detail = (bool(_MAT_DETAIL_RE.search(text)) or want_cost_doc
                            or bool(_LABOR_DETAIL_RE.search(text))
                            or want_header
                            or (want_labor and not want_form))
    tf = timefilter.parse(text, now)
    km = _KIND_RE.search(rest)
    rfq_kind = km.group(1).upper() if km else None

    no_id = not material_nos and not rfq_nos and not product_nos
    if _ARCH_RE.search(text) and no_id:
        kind = KIND_ARCHITECTURE
    elif _HELP_RE.search(text) and no_id:
        kind = KIND_CAPABILITIES
    elif _ESTIMATE_RE.search(text) and no_id:
        kind = KIND_ESTIMATE
    elif material_nos and not rfq_nos:
        kind = KIND_MATERIAL
    elif material_nos and rfq_nos and not want_material_detail and not want_form:
        kind = KIND_MATERIAL
    elif rfq_nos:
        kind = KIND_RFQ
    elif product_nos:
        kind = KIND_PRODUCT
    else:
        kind = KIND_SEARCH

    return {
        "kind": kind,
        "material_nos": _dedupe(material_nos),
        "product_nos": _dedupe(product_nos),
        "rfq_nos": _dedupe(rfq_nos),
        "want_form": want_form,
        "want_list": bool(_LIST_RE.search(text)),
        "want_similar": bool(_SIMILAR_RE.search(text)),
        "estimate_spec": estimate_spec(text),
        "want_material_detail": want_material_detail,
        "want_labor": want_labor,
        "want_header": want_header,
        "want_cost_doc": want_cost_doc,
        "time": tf,
        "rfq_kind": rfq_kind,
    }


def _dedupe(xs: list[str]) -> list[str]:
    seen: set[str] = set()
    return [x for x in xs if not (x in seen or seen.add(x))]


@dataclass(frozen=True)
class Route:
    kind: str
    title: str
    what: str
    trigger: str
    examples: tuple[tuple[str, str], ...] = ()


ROUTES: tuple[Route, ...] = (
    Route(
        kind=KIND_MATERIAL,
        title="料號查價",
        what="用材料料號查它在歷史成本分析表裡的原幣單價、供應商、規格、"
             "用量、MOQ、耗損、總價、交期，以及出自哪張 RFQ 的哪份附件。"
             "查不到就明說查不到，不會拿相近的東西充數。",
        trigger="問句裡出現材料料號（2~4 個大寫字母 + 6~9 位數字，例 MAT5120734）。"
                "同時出現 RFQ 編號時也走這裡 —— 除非明講要那張單的材料明細或"
                "綠框黃框",
        examples=(
            ("料號查價",
             "MAT5120734 這個料號，歷史成本分析表裡的原幣單價、供應商跟 MOQ 各是多少？"),
            ("價格有變動的料號",
             "WIR5187320 這顆料號，不同批次報價的單價有沒有出現過落差？"),
            ("查不到的料號", "MAT9999999 這個料號在歷史成本分析表裡查得到嗎？"),
            ("查有某料號的所有成本分析表",
             "找出有MAT5120734的所有成本分析表"),
        ),
    ),
    Route(
        kind=KIND_PRODUCT,
        title="產品料號查歷史",
        what="用**產品料號**（AX52K8D61930-3 這種）查它歷史上開過哪些單、"
             "每張單的表單成本欄填了什麼、哪幾張掛得出成本分析附件。"
             "成本欄的「填 0」與「沒有人填」會分開數 —— 這兩件事在畫面上"
             "長得一樣，意思相反。查不到就明說查不到，並列出試過哪五種比對。",
        trigger="問句裡出現產品料號（開頭是字母、9~15 個英數、接 '-' 再 1~3 碼，"
                "例 AX52K8D61930-3）。可以加時間條件：「最近一份」「2025 年的」",
        examples=(
            ("產品料號查歷史",
             "查AX52K8D61930-3這個產品料號,過去開過的每一張單"),
            ("查不到的產品料號",
             "AX9Z9ZZ99999-9 這個產品料號，是不是從來沒有開過任何一張單？"),
        ),
    ),
    Route(
        kind=KIND_RFQ,
        title="附件材料明細（BOM 層）",
        what="一張 RFQ 底下成本分析附件的逐項材料，欄位與原始 PDF 一致，"
             "並附上 A/B/C/D Total 等彙總與原檔連結。",
        trigger="問句有 RFQ/S/D 編號，且提到材料明細／用料／BOM"
                "（material breakdown、bill of materials 也可以）",
        examples=(
            ("單子的材料明細",
             "RFQ-20260609122-001 這張單，材料明細裡每一項的用量、單價、供應商分別是什麼？"),
            ("材料最多的單",
             "RFQ-20260511101-001 這張單用的料號很多，材料明細可以整份列出來嗎？"),
            ("English",
             "Can you give the full material breakdown, with quantities and "
             "unit costs, for RFQ-20260507024-001?"),
        ),
    ),
    Route(
        kind=KIND_RFQ,
        title="附件人工成本（B 段與工時參數）",
        what="成本分析附件 B.人工成本 的四列明細（SMT／組裝(Assembly)／委外成本／"
             "其它）各自的費用，加上組裝那一列印出來的全部算式輸入："
             "預估工站、預計組立總工時(Sec)、PPH、工時成本(?/Hr) 與幣別、"
             "人工效率 %。每一列都標了驗算結果 ——"
             "「費用 = 工時成本 × 組立總工時 ÷ 3600 ÷ 人工效率」對不對得上。"
             "費用空白與費用 0 分開講（空白通常是那道工序不做，不是不收錢）。",
        trigger="問句有 RFQ/S/D 編號，且提到人工成本／人工明細／工時／工站／PPH／"
                "人工效率（labor cost、man-hour 也可以）",
        examples=(
            ("附件的人工成本",
             "RFQ-20260527052-001 這張單，成本分析表的人工成本是怎麼算出來的？"
             "工站、組立總工時、PPH、工時費率跟人工效率各是多少？"),
            ("工時參數",
             "RFQ-20260602087-001 的預估工站跟預計組立總工時(Sec)是多少？"
             "人工成本四列各自的費用也列出來"),
        ),
    ),
    Route(
        kind=KIND_RFQ,
        title="表單彙總成本欄（綠框／黃框）",
        what="PLM RFQ 表單上的 24 個彙總成本欄：綠框「原幣成本欄位」與"
             "黃框「USD欄位(自動計算)」。這是整張單的彙總，"
             "跟料號查價的單一材料單價是兩件事，不能互相代換。",
        trigger="問句要**明講**原幣成本／USD 欄位／綠框／黃框"
                "（original currency cost、USD field、green box 也可以）",
        examples=(
            ("綠框黃框（要明講）",
             "RFQ-20260507024-001 這張單的原幣成本欄位（綠框）跟 USD 欄位（黃框），"
             "24 個彙總數字要怎麼對照？"),
            ("English",
             "Can you break down both the original currency cost fields "
             "(green box) and the USD fields (yellow box) for "
             "RFQ-20260507024-001?"),
        ),
    ),
    Route(
        kind=KIND_RFQ,
        title="附件表頭（Part Name／ACME P/N／報價原因）",
        what="成本分析附件**表頭那一區塊**印的每一格：EQ NO.、Rev、報價原因、"
             "日期、Customer code、Cus. P/N、ACME P/N、Part Name、Sales、"
             "製造廠、需求量、量產日期。"
             "★ 這幾格只印在附件 PDF 上，PLM 表單根本沒有 —— 尤其 Part Name，"
             "它帶著客戶專案代號與連接器型別，跟表單的規格描述常常只有一半一樣。"
             "抽不到的格子會明講抽不到（PDF 上本來空著、還是這份的 OCR 讀不到）。",
        trigger="問句有 RFQ/S/D 編號，且提到 Part Name／品名／報價原因／"
                "量產日期／Cus. P/N／ACME P/N／表頭",
        examples=(
            ("附件的 Part Name",
             "RFQ-20260612031-001 這份成本分析表的 Part Name 是什麼？"),
        ),
    ),
    Route(
        kind=KIND_SEARCH,
        title="用附件表頭的 Part Name 反查那份成本分析表",
        what="把成本分析附件表頭那格 Part Name 整串貼進來，直接找到**那一份**"
             "附件，回它的表頭原文、逐項材料、人工明細與 A/B/合計。"
             "五段升級梯（表頭品名完全相同／ACME P/N／Cus. P/N／包含／"
             "三連字元相似度），回覆會講明是哪一段命中、相似度多少。"
             "★ 保留相似度那一段是因為三成附件沒有文字層、表頭是 OCR 讀的"
             "（'124P TO' 讀成 '124PTO'、'THINLINE' 讀成 'THINUNE'），"
             "要求字面完全相同等於宣告掃描件一律查不到。"
             "第一名沒有明顯領先時不會替使用者挑一份，會把候選列出來。",
        trigger="問句沒有 RFQ 編號也沒有料號，但整句話跟某份附件表頭的 "
                "Part Name 對得上（比對是確定性的，對不上就照原本的檢索走）",
        examples=(
            ("貼整串 Part Name 查成本分析",
             "(CUS)_REV03_X9-A00012345_MCIO 124P TO THINLINE SAS "
             "8I+4I/L400MM/30AWG/85OHM 的成本分析?"),
        ),
    ),
    Route(
        kind=KIND_SEARCH,
        title="描述與條件反查",
        what="用規格描述、客戶名、料號片段反查 RFQ 編號，"
             "或用成本數值條件過濾（條件會轉成 SQL 硬性過濾，回傳的每筆都符合）。"
             "時間條件同樣是硬性過濾，寫法不限："
             "「2026年5月」「2026 的五、六月」「5月到7月」「第二季」「Q2」"
             "「上半年」「年初」「民國114年」「今年／去年／上個月／本週」"
             "「近三個月」「2025 年之後」「三個月前」「5月7日」都認得，"
             "查詢條件卡片會寫出系統把它理解成哪一天到哪一天。"
             "每張卡都會講出它的資料來源：有成本分析附件就掛原檔連結，"
             "沒有就直接標「無附件原檔」，不會留白讓人自己猜。",
        trigger="問句沒有指名料號或編號、也沒有問區間時的預設路徑",
        examples=(
            ("規格描述",
             "有沒有做過 50 ohm single-ended cable 這種規格的線材？相近的 RFQ 都列出來"),
            ("時間區間",
             "幫我把 2026 年 5 月到 6 月之間開立、有掛成本分析附件的單子都列出來"),
            ("最近一份RFQ成本分析",
             "列出2026六月的RFQ成本分析表"),
            ("季／半年",
             "2026 上半年開立的所有單子都列出來，不管有沒有掛得出成本分析附件"),
        ),
    ),
    Route(
        kind=KIND_ESTIMATE,
        title="新規格估價區間",
        what="給一段還沒做過的規格，回歷史上類似料的成本區間"
             "（p25／中位／p75，逐欄）、信心等級與佐證案例。"
             "★ 是參考不是報價：只有高信心那一區實測 84.0% 落在實際值 2 倍內，"
             "中低信心一律要走完整 BOM 估算，回覆裡會明講。",
        trigger="問句沒有料號也沒有編號，而且問的是「大概多少錢」「成本區間」"
                "「這類以前多少」（ballpark、price range 也可以）",
        examples=(
            ("新規格估區間",
             "MCIO 8X Straight to MCIO 8X Right Angle，線長 450mm、雙軸遮蔽、"
             "鍍金端子，這類規格以前大概多少錢？"),
            ("English",
             "ballpark cost of a 50 ohm single-ended coax cable, L=300mm, "
             "with SMA connectors on both ends"),
        ),
    ),
    Route(
        kind=KIND_CAPABILITIES,
        title="能力說明",
        what="列出目前實際可用的問法、工具與資料覆蓋量。"
             "內容是執行期組出來的，新增功能會自己出現。",
        trigger="問「你可以做什麼」「有什麼功能」「what can you do」"
                "（問句裡有料號或編號時不算，那是在問那筆資料）",
        examples=(("你可以做什麼", "你可以做什麼?"),),
    ),
    Route(
        kind=KIND_ARCHITECTURE,
        title="技術架構說明文件",
        what="八通道檢索逐路的目的／方法／觸發條件／範例（附自我驗證）、"
             "RRF 融合公式、硬性過濾與跨單型去重、料號線與編號線為什麼不能"
             "互相代換、估價信心等級怎麼量出來的、附件解析三層與算式對帳、"
             "附件版本與路徑鍵、排程五個 stage（含即時狀態）、勾選功能、"
             "健康檢查、ASCII 視覺化架構圖、技術選型與功能總覽。"
             "給的是工程師看的系統設計文件，不是使用者的問法清單。",
        trigger="問「技術架構」「系統架構」「檢索機制」「資料流程」"
                "（問句裡有料號或編號時不算）",
        examples=(("技術架構說明文件", "技術架構說明文件"),),
    ),
)


def examples() -> list[dict[str, str]]:
    return [{"label": label, "prompt": prompt}
            for r in ROUTES for label, prompt in r.examples]
