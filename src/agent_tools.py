"""工具定義（Anthropic tool schema）、system prompt 與工具呼叫的分派。"""
from __future__ import annotations

from typing import Any

from . import estimate as est
from . import fields, material, product, retrieval, timefilter

TOOLS: list[dict[str, Any]] = [
    {
        "name": "lookup_material",
        "description": (
            "【成本報價詢問】用**材料料號**查它在歷史成本分析表裡的單價，"
            "例『MAT5120734 多少錢』。料號長相是 2~4 個大寫字母 + 6~9 位數字"
            "（MAT5120734、TRM5203391、HSG5146608）。"
            "回傳每一次出現的原幣單價、原幣別、匯率、供應商、規格說明、用量、"
            "MOQ、耗損，以及那份成本分析表屬於哪張 RFQ。"
            "\n★ 這支跟 lookup_rfq 是**兩件不同的事**，不要互相代換："
            "這支回的是單一材料的單價（來自附件 PDF 的材料明細），"
            "lookup_rfq 回的是整張 RFQ 表單的彙總成本欄（綠框／黃框）。"
            "\n★ 查不到就是查不到（found=false）。不要改用 search_rfq 去撈"
            "看起來相近的 RFQ 當答案 —— 料號是識別碼，沒有『差不多』這回事。"
            "要看同系列料號請改用 find_similar_material，而且要先問過使用者。"
            "\n★ 每一列都帶 check_status：ok/repaired 表示這列通過表格自己的"
            "算式驗算（單價×用量×(1+耗損)=總價），數字可信；"
            "rescaled 是程式推測過小數點位置；unverified 表示驗不過，"
            "**不可以當報價講**，要明說這筆沒認準。"
            "\n★ 每一列還帶來源附件的版本：src_file_name（PLM 上的原始檔名）與"
            "doc_modified_on（檔案更新時間，UTC）。同一個檔名在庫裡常有好幾個版本，"
            "講單價時要連檔名與更新時間一起講。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "part_no": {"type": "string",
                            "description": "材料料號，例 MAT5120734"},
            },
            "required": ["part_no"],
        },
    },
    {
        "name": "find_similar_material",
        "description": (
            "料號只打一半、或想看同一族的料時用。例『MAT51207 開頭的有哪些』。"
            "★ 這支只查**元件／材料**層（MAT/WIR/TRM/HSG… 這些子件），"
            "而且是**前綴比對**不是相似度計算。**成品線材料號（AX52K8D61930-3 這類產品料號）"
            "要找規格相近的替代品，請改用 partrag__find_similar_parts** ——"
            "那支才是語料層的相似度檢索，兩支的語料與演算法都不一樣。"
            "★ 只有使用者明確要求『類似／相近／同系列』時才呼叫。"
            "lookup_material 查不到時**不要**自動改用這支 —— 使用者問的是"
            "某個特定料號的價格，回一堆別的料號不是答案。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prefix": {"type": "string", "description": "料號片段，至少 3 碼"},
            },
            "required": ["prefix"],
        },
    },
    {
        "name": "search_material_spec",
        "description": (
            "用**規格描述**找材料，例『30AWG 90ohm 的線材』『PA66+30%GF 的膠殼』。"
            "回傳符合的材料明細列與其單價。這支是模糊比對，"
            "跟 lookup_material 的精確查號分開，才不會讓查號意外落進模糊比對。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "spec": {"type": "string", "description": "規格或品名關鍵字"},
                "limit": {"type": "integer", "description": "回傳幾筆，預設 20"},
            },
            "required": ["spec"],
        },
    },
    {
        "name": "find_cost_doc_by_part_name",
        "description": (
            "用**成本分析表表頭那一格**（Part Name／ACME P/N／Cus. P/N）反查"
            "那份附件，例把整串"
            "『(CUS)_REV03_X9-A00012345_MCIO 124P TO THINLINE SAS "
            "8I+4I/L400MM/30AWG/85OHM』貼進來。"
            "回傳那幾份附件的表頭原文、材料明細、人工明細與 A/B/合計。"
            "\n★ 這支跟 search_rfq 是**兩件不同的事**，不要互相代換："
            "這支查的是**附件 PDF 表頭印的品名**，search_rfq 查的是 PLM "
            "表單上另外有人打的規格描述（spec_desc）。同一顆產品兩邊常常只有"
            "一半一樣，所以問附件上的品名時用這支，不要拿 search_rfq 的結果"
            "回答 —— 那會回一批規格相近但不是使用者問的那一份的單。"
            "\n★ 這支有一段是三連字元相似度比對，因為三成附件的表頭是 OCR "
            "讀出來的（'124P TO' 讀成 '124PTO'、'THINLINE' 讀成 'THINUNE'）。"
            "回傳的 matched_by 會講明是哪一段命中的、docs[0].sim 是相似度。"
            "**字面上有出入是正常的**，不要因此跟使用者說規格不一致或"
            "建議他重新查詢。"
            "\n★ ambiguous=true 表示第一名沒有明顯領先第二名（同系列的品名"
            "只差一個線長）。這時要把候選都列給使用者確認，不要替他挑一份。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "part_name": {"type": "string",
                              "description": "附件表頭上的 Part Name／"
                                             "ACME P/N／Cus. P/N，原樣貼進來"},
                "limit": {"type": "integer", "description": "回傳幾份，預設 10"},
            },
            "required": ["part_name"],
        },
    },
    {
        "name": "rfq_material_detail",
        "description": (
            "一張 RFQ 底下**成本分析表附件**的逐項材料明細（BOM 層），"
            "以及那份附件的彙總（A/B/C/D Total、管銷、合計）。"
            "★ 跟 lookup_rfq 不同：那支回表單上的綠框／黃框欄位，"
            "這支回附件 PDF 裡的材料逐項。同一張單可能只有其中一邊有資料。"
            "使用者問「這張單的材料明細／用了哪些料」時用這支。"
            "\n★ 一張 RFQ 可能掛好幾份附件（docs 會有多列，最新的排最前面），"
            "而且**檔名可能一樣、版本不同**。每一列都帶 src_file_name 與"
            "doc_modified_on（檔案更新時間），回答時要用它們指明是哪一份，"
            "不要把不同份的數字混在一起。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "rfq_no": {"type": "string",
                           "description": "完整編號，例 RFQ-20260511108-001"},
            },
            "required": ["rfq_no"],
        },
    },
    {
        "name": "rfq_labor_detail",
        "description": (
            "一張 RFQ 底下**成本分析表附件的 B.人工成本**：四列明細"
            "（SMT／組裝(Assembly)／委外成本／其它）各自的費用，"
            "以及組裝那一列的工時參數 —— 預估工站、預計組立總工時(Sec)、PPH、"
            "工時成本(?/Hr) 與它的幣別、人工效率 %。"
            "使用者問「人工成本怎麼算出來的」「組立總工時多少」「工時費率多少」"
            "時用這支。"
            "\n★ 這跟 lookup_rfq 回的表單人工成本欄（labor_cost）**不是同一件事**："
            "那是表單上的一格彙總，這裡是附件那張表的逐列內容與算式輸入，"
            "兩者不可以互相代換。"
            "\n★ check_status 一定要看：ok 表示『費用 = 工時成本 × 組立總工時 "
            "÷ 3600 ÷ 人工效率』或『費用 = 工時成本 ÷ PPH』對得上；"
            "mismatch 表示對不上，那一列的數字不可以當報價；na 是參數不齊。"
            "\n★ cost 是 null 代表表上那一格**空白**（例如這顆料不用過爐），"
            "不是 0。這兩件事不可以合併講。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "rfq_no": {"type": "string",
                           "description": "完整編號，例 RFQ-20260527052-001"},
            },
            "required": ["rfq_no"],
        },
    },
    {
        "name": "lookup_product",
        "description": (
            "【產品料號查歷史】用**產品料號**查它歷史上開過哪些 RFQ/S/D、"
            "每張單的表單成本欄填了什麼、哪幾張掛得出成本分析附件。"
            "產品料號長相是字母開頭、9~15 個英數、接 '-' 再 1~3 碼"
            "（AX52K8D61930-3、AX618RT20573-7、AXC310542796-1L）。"
            "\n★ 這是**第三種識別碼**，跟另外兩種都不一樣，不要互相代換："
            "lookup_rfq 吃的是 RFQ-20220816010-001 這種單號；"
            "lookup_material 吃的是 MAT5120734 這種材料料號；"
            "這一支吃的是**產品本身**的料號（PLM 表單上的「產品料號」欄）。"
            "\n★ 使用者給產品料號時，**絕對不要回「查無編號」** —— 它本來就"
            "不是單號。這支查的是 表單的 part_no 欄，查得到就直接回。"
            "\n★ 回傳的 cost 會把成本欄分成三種：有值／填 0／從來沒有人填。"
            "這三種要分開講，合併成「沒有成本資料」是錯的（0 是有效值）。"
            "\n★ 查不到時 attempts 會列出試過的五種比對方式"
            "（完全相同／別名欄位／開頭／包含／去分隔符），"
            "回答時要說出來已經試過這些，讓使用者知道不是隨便查一下就放棄。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "part_no": {"type": "string",
                            "description": "產品料號，例 AX52K8D61930-3"},
                "kind": {"type": "string",
                         "description": "只看某個單型（RFQ／RFS／RFD），選填"},
                "latest": {"type": "boolean",
                           "description": "只要最新的那一張，選填"},
                "want_doc": {"type": "boolean",
                             "description": "配 latest 用：優先挑真的掛得出"
                                            "成本分析附件的那一張，選填"},
            },
            "required": ["part_no"],
        },
    },
    {
        "name": "lookup_rfq",
        "description": (
            "用 RFQ/S/D 編號查出該筆的完整成本區塊（表單上的綠框『原幣成本欄位』"
            "與黃框『USD欄位(自動計算)』）與基本資料。編號格式如 "
            "RFQ-20220816010-001（末段流水號是 3 碼，不是 4 碼）。"
            "查得到就回傳全部欄位，並附上成本區塊文字（綠框、黃框各一段條列）。"
            "注意：欄位值 0 與未填是兩件事，回傳會分別呈現為 0 與『未填』。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "rfq_no": {"type": "string",
                           "description": "完整編號，例 RFQ-20220816010-001"},
            },
            "required": ["rfq_no"],
        },
    },
    {
        "name": "search_rfq",
        "description": (
            "用自然語言條件反查 RFQ/S/D 編號。支援：規格描述、客戶名、料號、"
            "母單號，以及成本數值條件（例『材料成本大於 100 的 RFQ』、"
            "『標準工時大於 3000 的』、『年需求量超過 100000 的』）。"
            "數值條件會被轉成 SQL 硬性過濾，回傳的每一筆都保證符合條件。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "自然語言查詢"},
                "limit": {"type": "integer", "description": "回傳幾筆，預設 8"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "estimate_cost",
        "description": (
            "給一段新產品規格描述，回傳歷史上類似料的成本區間（p25/中位/p75）、"
            "信心等級與佐證案例。用途是給 BOM 建構與成本預估一個起點，"
            "以及回答『這類東西以前大概多少錢』。"
            "★ 這不是報價。回傳的 confidence 是 high/medium/low，"
            "實測準確率分別為『2 倍內 84.0%／69.6%／53.2%』，"
            "中位誤差 1.24／1.68／1.91 倍。"
            "只有 high 才適合當參考，medium 與 low 必須改走完整 BOM 估算。"
            "務必把 confidence 與 recommendation 一起轉達給使用者，不要只講數字。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "spec": {"type": "string",
                         "description": "產品規格描述，例 "
                                        "'MCIO 8X Straight to MCIO 8X Right Angle L=450mm'"},
                "customer": {"type": "string",
                             "description": "客戶名（選填）。有給的話會額外撈同客戶"
                                            "的歷史案例，實測同客戶在場時較準。"},
                "k": {"type": "integer", "description": "取幾個相似案例，預設 8"},
            },
            "required": ["spec"],
        },
    },
    {
        "name": "sanity_check_cost",
        "description": (
            "合理性檢核。下游（成本預估 Agent / 製程工時 Agent）由 BOM 由下而上"
            "算出成本之後，用這支對照歷史上類似料的區間，抓出明顯離譜的數字。"
            "例如『這顆料材料成本算出 300 美元、但歷史上同類都在 2~8 美元』。"
            "回傳 ok=false 時代表有欄位超出歷史範圍，flags 會說明哪一欄、差多少。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "spec": {"type": "string", "description": "產品規格描述"},
                "proposed": {
                    "type": "object",
                    "description": "下游算出來的成本，鍵用欄位名，例 "
                                   "{\"mat_cost_usd\": 12.5, \"labor_cost_usd\": 0.8}",
                    "additionalProperties": {"type": "number"},
                },
            },
            "required": ["spec", "proposed"],
        },
    },
]


def dispatch(name: str, args: dict) -> dict:
    try:
        if name == "lookup_material":
            return material.lookup(args["part_no"])

        if name == "find_similar_material":
            return material.find_similar(args["prefix"])

        if name == "search_material_spec":
            return material.search_spec(args["spec"],
                                        limit=int(args.get("limit", 20)))

        if name == "find_cost_doc_by_part_name":
            res = material.by_part_name(args["part_name"],
                                        limit=int(args.get("limit", 10)))
            if not res.get("found"):
                res["coverage"] = material.coverage()
            return res

        if name == "rfq_material_detail":
            return material.by_rfq(args["rfq_no"])

        if name == "rfq_labor_detail":
            res = material.labor_by_rfq(args["rfq_no"])
            if not res.get("found"):
                res["coverage"] = material.labor_coverage()
            return res

        if name == "lookup_product":
            tf = (timefilter.TimeFilter(order="desc", want_one=True,
                                        label="最新的一張")
                  if args.get("latest") else None)
            return product.lookup(args["part_no"], tf,
                                  kind=args.get("kind"),
                                  want_doc=bool(args.get("want_doc")))

        if name == "lookup_rfq":
            row = retrieval.by_number(args["rfq_no"])
            if row is None:
                return {"found": False, "rfq_no": args["rfq_no"],
                        "hint": "查無此編號。可改用 search_rfq 以部分編號或描述搜尋。"}
            return {"found": True, "row": row,
                    "cost_block": fields.render_cost_lines(row)}

        if name == "search_rfq":
            return retrieval.search(args["query"], limit=int(args.get("limit", 8)))

        if name == "estimate_cost":
            return est.estimate(args["spec"], k=int(args.get("k", est.K)),
                                customer=args.get("customer"))

        if name == "sanity_check_cost":
            return est.sanity_check(args["spec"], args["proposed"])

        return {"error": f"未知的工具：{name}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


SYSTEM_PROMPT = """你是 OEM/ODM 新產品估價流程裡的「產品估價 Agent」。

════════════════════════════════════════════════════════════
第 0 條：不知道就說不知道。這一條的優先權高於所有其他規則。
════════════════════════════════════════════════════════════

* 你講的每一個**數字**、每一個**編號／料號**、每一個**附件檔名**，
  都必須在「檢索結果」或工具回傳的資料裡真的出現過。可以四捨五入，
  不可以換一個。找不到就說「資料裡沒有這一項」。
* **絕對不可以**用 X／N／TBD／<金額> 這種佔位符代替一個數字。
  一句「這一欄沒有資料」勝過一個看起來合理的數字。
* 覆蓋率、已索引份數、總筆數這類統計值尤其不可以憑印象講。
* 工具回 found=false／{"ok": false} 時，那就是答案：說查不到，
  並把它給的說明（試過哪幾種比對、已索引多少份）講完。
  **不要**改用別的工具去撈「看起來相近」的東西充數。
* 你的回答會被系統自動對照檢索結果核對；編出來的數字與編號會被退件重寫。

════════════════════════════════════════════════════════════
第 1 條：先分清楚使用者給的是**哪一種識別碼**，三種不能互相代換
════════════════════════════════════════════════════════════

【A】綠框／黃框 —— RFQ 表單上的彙總成本欄
     來源：PLM 的 RFQ/S/D 表單
     長相：一張單一組數字，材料成本／人工成本／製造成本…一組彙總欄
     識別碼：RFQ-20220816010-001 這種**單號**
     工具：lookup_rfq、search_rfq
     問法：「RFQ-xxx 的成本？」「材料成本大於 100 的有哪些？」

【B】材料料號查價 —— 成本分析表附件裡的逐項材料
     來源：掛在 RFQ 底下的成本分析 PDF，逐項材料的單價
     長相：一份幾十列，每列是一顆料的料號／供應商／規格／原幣單價
     識別碼：MAT5120734 這種**材料料號**（2~4 個大寫字母 + 6~9 位數字）
     工具：lookup_material、find_similar_material、search_material_spec
     問法：「MAT5120734 多少錢？」「這顆料以前跟誰買、多少錢？」

【C】產品料號查歷史 —— 這顆**產品**開過哪些單
     來源：PLM 表單的「產品料號」欄（表單的 part_no 欄）
     長相：一顆產品對到好幾張單，每張單自己一組表單成本欄
     識別碼：AX52K8D61930-3、AX618RT20573-7、AXC310542796-1L
             （字母開頭、9~15 個英數、接 '-' 再 1~3 碼）
     工具：lookup_product
     問法：「AX52K8D61930-3 過去成本」「這顆料最近一份 RFQ 的成本分析」

★ 使用者給【C】產品料號時，**絕對不要說「查無編號」**。
  它本來就不是單號 —— 它是產品自己的料號，用 lookup_product 查得到。
  講「查無編號 AX52K8D61930-3」然後又列出幾張單的成本，是自相矛盾，
  使用者會以為系統壞了。（這是實際發生過的錯誤。）

★ 使用者問一個**材料料號**的價格時：只用 B 的工具回答。
  絕對不要改去查 RFQ 編號、也不要因為查不到就回一堆語意相近的 RFQ 紀錄。
  查不到就只講一句「查無此料號」，並說明目前索引了多少份成本分析表 ——
  使用者分不出「這顆料不存在」與「那份 PDF 還沒匯入」，你要幫他分。

★ 問句有時間條件時：系統已經照建檔時間排序／過濾好了，檢索結果會註明。
  **第一筆就是答案**，不要自己從清單裡挑一筆說它是最新的。
  檢索結果沒有註明排序時，不可以宣稱任何一筆是「最新的」。
  認得的寫法涵蓋「最近一份」「2025 年的」「近三個月」「2026 的五、六月」
  「5月到7月」「第二季／Q2」「上半年」「年初」「民國114年」「上個月」
  「本週」「三個月前」「5月7日」。有區間時**回傳的每一筆都已經落在區間內**，
  所以不要再自己過濾、也不要說「其中某幾筆可能不在範圍」。

★ 兩邊的數字**不可以互相代換**：A 是整張單的彙總，B 是單一材料的單價。
  「這張單的材料成本是 4.00 美元」與「這顆線材一米 1.20 人民幣」
  是兩件事，不要用其中一個回答另一個的問題。

★ 沒有人問關聯檔案／類似料號時，不要主動列出來。
  使用者要的是他問的那一筆，不是一份清單。

★ 成本欄的「填 0」與「從來沒有人填」是**兩件不同的事**，一定要分開講。
  0 是有效值（這張單的成本確實記成零，RFS 服務單常常如此）；
  未填表示沒有人做過成本，數字要去別的地方找。
  合併成一句「沒有成本資料」，等於把一個結論講成一次查詢失敗。

其餘你能做的事：
  - 給新規格找歷史上的類似料與成本區間（estimate_cost）
  - 對下游算出來的成本做合理性檢核（sanity_check_cost）
  - 查一張 RFQ 底下附件的完整材料明細（rfq_material_detail）

你必須遵守的事實與限制：

* 料號查價的每一列都帶 check_status，講價格時必須連著講：
    ok / repaired  通過表格自己的算式驗算（單價×用量×(1+耗損)=總價），可信
    rescaled       程式推測過小數點位置，要說明這是推測值
    unverified     驗不過。**不可以當報價講**，要明說「這筆沒認準」
  成本分析表有一部分是掃描件，靠 OCR 讀出來的，會認錯數字。
  寧可說「這筆沒認準」，也不要給一個看起來合理的錯價格。

* 引用到成本分析附件時，**檔名後面一定要接檔案更新時間**（context 裡每一份附件都
  帶了，格式是「更新 2026-06-26 09:47」或「下載 2026-08-05 10:18」）。
  同一個檔名在庫裡常常有好幾個版本（「成本分析.pdf」這種名字會有很多不同檔案共用），
  只講檔名使用者無法判斷是哪一版，而版本錯了單價就錯了。
  同一張 RFQ 有兩份以上附件時，每一份的數字要分開講，不要合併成一組。
  「下載」與「更新」不可以互換講：「下載」只是我們抓檔的時間，不是版本時間。

* 料號的原幣單價與原幣別（currency）才是原始輸入。
  換算後的 unit_price 要連 fx_rate 一起講；匯率是 1 的模板換算欄印著 (USD)
  但值其實是人民幣，只講 unit_price 會報錯幣別。

* 成本欄位的 0 與「未填」是兩件不同的事。0 是有效值（表示該項確實為零），
  「未填」表示從來沒有人填。不要把「未填」講成 0，也不要把 0 講成沒有資料。

* 只有少數表單填了成本明細。你查不到成本，多半是因為那筆本來就沒填，
  不是系統壞了。

* estimate_cost 回傳的是**參考區間，不是報價**。務必連同 confidence 與
  recommendation 一起講。confidence 是 medium 或 low 時，要明確告訴使用者
  這個區間不可靠、需要走完整的 BOM 估算流程，不要只把中位數講出來讓人誤以為那是價格。

* 這份資料**算不出毛利**。成本明細與報價單價分屬兩群幾乎不重疊的紀錄
  （幾乎沒有同時具備兩者的紀錄）。有人問「以前這類產品賺多少」時要直說查不到，
  不要用成本去推價格。

* 幣別要講清楚。欄位名有 _usd 後綴的是美元，沒有的是原幣（原幣別看 currency 欄）。

回答用繁體中文。數字要標單位與幣別。有引用到具體案例時附上 RFQ 編號讓人可以自己去查。

輸出格式（一律遵守，不要寫成一大段）：

* 條列式。每一項自己一行、開頭用「- 」，一行只講一件事。
* 人工成本（附件 B 段）講大綱就好：B Total、組裝那一列的算式
  （費用 = 工時成本 × 組立總工時 ÷ 3600 ÷ 人工效率）、對帳過不過、
  哪幾列的費用是空白的。逐列明細在畫面卡片上，不要覆述。
  費用空白的列要說「沒填」而不是 0 —— 那一格在表上是空的，代表那道工序不做。
* 講成本時，綠框「原幣成本欄位」與黃框「USD欄位(自動計算)」要**分成兩段**，
  各自先寫一行段落標題再往下條列。絕對不要把原幣與 USD 的數字寫在同一行或同一句。
* 使用者問的編號查不到時：第一行單獨講「查無編號 XXX」，再另起一段列出相近的編號，
  並明說那是相近編號、不是他問的那一筆。
* 原幣幣別看 currency 欄，該欄是空的就只說「原幣」，不要自己假設是 USD。
"""
