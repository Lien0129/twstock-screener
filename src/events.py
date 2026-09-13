"""事件行事曆：除權息、財報公告、月營收公告。

這一層不是選股因子，是「別在錯的時間點進場」的提醒。
除權息前後股價會跳空、財報公告前後波動放大——這些不是訊號失靈，是可預期的事件。
"""
import json
import os
from datetime import date, datetime, timedelta

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX_PATH = os.path.join(ROOT, "data", "events", "exdividend.csv")

# 台股財報法定公告期限（一般公司；金控與特殊產業另有規定）
FINANCIAL_DEADLINES = [(3, 31, "年報"), (5, 15, "第一季財報"),
                       (8, 14, "第二季財報"), (11, 14, "第三季財報")]
REVENUE_DAY = 10  # 每月 10 日前公告上月營收


def _roc_to_date(s: str):
    """115年09月07日 → date(2026, 9, 7)。已是西元格式也接受。"""
    s = str(s).strip()
    try:
        if "年" in s:
            y = int(s.split("年")[0]) + 1911
            m = int(s.split("年")[1].split("月")[0])
            d = int(s.split("月")[1].replace("日", ""))
            return date(y, m, d)
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def load_exdividends() -> dict[str, list[dict]]:
    """除權息預告表。欄位：code,date,type,cash,stock"""
    if not os.path.exists(EX_PATH):
        return {}
    df = pd.read_csv(EX_PATH, dtype={"code": str})
    out: dict[str, list[dict]] = {}
    for _, r in df.iterrows():
        d = _roc_to_date(r["date"])
        if not d:
            continue
        out.setdefault(str(r["code"]).zfill(4), []).append({
            "date": d, "type": str(r.get("type", "")),
            "cash": r.get("cash"), "stock": r.get("stock"),
        })
    return out


def _events_csv(name: str) -> pd.DataFrame:
    p = os.path.join(ROOT, "data", "events", name)
    if not os.path.exists(p):
        return pd.DataFrame()
    try:
        d = pd.read_csv(p, dtype={"code": str})
        d["code"] = d["code"].astype(str).str.zfill(4)
        return d
    except Exception:
        return pd.DataFrame()


def _parse_period(s: str) -> tuple[date | None, date | None]:
    """115/08/27～115/09/02 → (date, date)。"""
    s = str(s or "").replace("~", "～").strip()
    if "～" not in s:
        return None, None
    out = []
    for part in s.split("～")[:2]:
        try:
            y, m, d = part.strip().split("/")
            out.append(date(int(y) + 1911, int(m), int(d)))
        except Exception:
            out.append(None)
    return (out + [None, None])[:2]


def risk_flags(code: str, today: date) -> list[dict]:
    """處置 / 注意 / 現金增資——會改變「你能怎麼交易」的事實。

    這一類跟新聞不同：它不試圖預測方向，它告訴你停損可能失效。
    被處置期間是分盤交易（約每 2~25 分鐘才撮合一次）並且預收款券，
    想砍在某個價位不一定砍得掉。
    """
    out = []

    p = _events_csv("punish.csv")
    if not p.empty:
        for _, r in p[p["code"] == code].iterrows():
            a, b = _parse_period(r.get("period"))
            if b and b >= today:
                d = (a - today).days if a and a > today else 0
                out.append({
                    "type": "處置股", "date": str(a or today), "days": max(d, 0),
                    "note": f"{r.get('measure', '')}｜{r.get('reason', '')}"
                            f"｜期間 {r.get('period', '')}"
                            f"　分盤撮合且預收款券，停損不一定砍得掉",
                    "level": "warn"})

    n = _events_csv("notice.csv")
    if not n.empty:
        sub = n[n["code"] == code]
        if len(sub):
            r = sub.iloc[-1]
            out.append({"type": "注意股", "date": str(r.get("date", "")), "days": 0,
                        "note": f"{r.get('reason', '')}　連續達標會被處置",
                        "level": "warn"})

    c = _events_csv("capital.csv")
    if not c.empty:
        for _, r in c[c["code"] == code].iterrows():
            out.append({"type": "現金增資", "date": str(r.get("date", "")), "days": 0,
                        "note": f"認購率 {r.get('rate', '')}%　股本稀釋",
                        "level": "warn"})

    # 重大訊息：法定揭露，好壞都得公告——這是唯一能自動抓到利空的來源
    m = _events_csv("material.csv")
    if not m.empty:
        for _, r in m[m["code"] == code].sort_values("date").tail(6).iterrows():
            clause = str(r.get("clause", ""))
            if clause in CONFERENCE_CLAUSES:
                continue                      # 法說會走行事曆，不當風險旗標
            d = _roc_ymd(str(r.get("happened", ""))) or _roc_to_date(str(r.get("happened", "")))
            if d and (today - d).days > 30:
                continue
            label, text = describe_material(r)
            out.append({"type": label, "date": str(d or r.get("date", "")),
                        "days": max((d - today).days, 0) if d else 0,
                        "note": text, "level": MATERIAL_LEVEL.get(clause, "info")})
    return out


# 重大訊息「符合條款」對照。只列已經實際驗證過內容的，沒把握的就不硬猜。
CLAUSE_LABEL = {
    "第6款": "董監事異動",
    "第8款": "重要人員異動",
    "第26款": "法人說明會",
    "第35款": "重大訊息",
    "第12款": "法人說明會",
    "第14款": "董事會決議股利",
    "第19款": "重訊補充說明",
    "第20款": "取得或處分資產",
    "第51款": "財務業務資訊公告",
}
CONFERENCE_CLAUSES = {"第12款", "第26款"}
# 哪些要標成橘色警示：補充說明通常是事件延燒，資產處分與董監異動只是資訊
MATERIAL_LEVEL = {"第19款": "warn"}


def _field(detail: str, *names: str) -> str:
    """從重訊的說明欄裡挖出某個欄位。

    說明長這樣：「1.事實發生日:115/08/31 2.發生緣由:補充說明… 3.因應措施:…」
    API 在這些條款類型下**不給主旨**（實測全是空的），內容只在說明裡，
    所以得自己切。
    """
    t = str(detail or "")
    for n in names:
        i = t.find(n)
        if i < 0:
            continue
        seg = t[i + len(n):]
        # 切到下一個「數字.」編號為止
        import re
        mm = re.search(r"\s\d{1,2}\.[^\d]", seg)
        return (seg[:mm.start()] if mm else seg)[:120].strip(" :：|")
    return ""


def describe_material(r) -> tuple[str, str]:
    """把一筆重訊變成「標籤 + 一句白話」。"""
    clause = str(r.get("clause", ""))
    detail = str(r.get("detail", ""))
    subj = str(r.get("subject", "") or "").strip()
    label = CLAUSE_LABEL.get(clause, f"重大訊息{clause}")
    if subj and subj.lower() != "nan":
        return label, subj[:110]
    txt = (_field(detail, "發生緣由") or _field(detail, "標的物之名稱")
           or _field(detail, "新任者姓名") or _field(detail, "法人名稱")
           or _field(detail, "發放股利種類及金額") or detail[:110])
    # 重訊的說明欄常常整段夾著表單樣板（「（請輸入發言人、代理發言人…）」）
    # 和換行。原樣塞進卡片會變成一坨無法閱讀的字，這裡壓成單行並砍掉括號說明。
    import re
    txt = re.sub(r"（請輸入[^）]*）", "", txt)
    txt = re.sub(r"\s+", " ", txt).strip(" :：|、")
    return label, txt[:100]


def investor_conferences_from_material(code: str, today: date,
                                       horizon_days: int = 30) -> list[dict]:
    """法說會。用**符合條款第12款**判斷，不是字串比對主旨——
    主旨在這個端點是空的，字串比對永遠match不到（第一版就是這樣壞的）。"""
    m = _events_csv("material.csv")
    if m.empty:
        return []
    out = []
    for _, r in m[(m["code"] == code)
                  & (m["clause"].astype(str).isin(CONFERENCE_CLAUSES))].iterrows():
        d = _roc_ymd(str(r.get("happened", "")))
        if not d:
            continue
        days = (d - today).days
        if -1 <= days <= horizon_days:
            detail = str(r.get("detail", ""))
            when = _field(detail, "召開法人說明會之時間")
            where = _field(detail, "召開法人說明會之地點")
            gist = _field(detail, "法人說明會擇要訊息")
            note = "　".join(x for x in (when, where, gist[:70]) if x)
            out.append({"type": "法說會", "date": str(d), "days": max(days, 0),
                        "note": (note or "") + "　展望公布日，前後跳空風險升高",
                        "level": "warn" if days <= 3 else "info"})
    return out


def _roc_ymd(s: str) -> date | None:
    """1151013 → date(2026,10,13)。重大訊息與股東會端點用這種格式。"""
    s = str(s or "").strip()
    if len(s) != 7 or not s.isdigit():
        return None
    try:
        return date(int(s[:3]) + 1911, int(s[3:5]), int(s[5:7]))
    except ValueError:
        return None


def investor_conferences(code: str, today: date, horizon_days: int = 30) -> list[dict]:
    """法說會。從重大訊息裡撈——法說會依規定要用重大訊息公告。

    為什麼要標：法說會是可預期的跳空風險日。公司在會上給的展望
    通常當天或隔天就反映在股價上，而且雙向都可能。
    """
    m = _events_csv("material.csv")
    if m.empty:
        return []
    out = []
    for _, r in m[m["code"] == code].iterrows():
        subj = str(r.get("subject", ""))
        if "法人說明會" not in subj and "法說" not in subj:
            continue
        # 主旨或說明裡通常帶日期，優先用事實發生日
        d = _roc_ymd(str(r.get("happened", ""))) or _roc_to_date(str(r.get("happened", "")))
        if not d:
            continue
        days = (d - today).days
        if 0 <= days <= horizon_days:
            out.append({"type": "法說會", "date": str(d), "days": days,
                        "note": f"{subj[:70]}　展望公布日，前後跳空風險升高",
                        "level": "warn" if days <= 3 else "info"})
    return out


def upcoming(code: str, today: date, horizon_days: int = 21,
             ex: dict | None = None) -> list[dict]:
    """回傳未來 N 天內的事件，近的排前面。"""
    ex = ex if ex is not None else load_exdividends()
    events = list(risk_flags(code, today)) + investor_conferences_from_material(code, today)

    for e in ex.get(code, []):
        days = (e["date"] - today).days
        if 0 <= days <= horizon_days:
            label = "除息" if "息" in e["type"] else ("除權" if "權" in e["type"] else "除權息")
            note = f"現金股利 {e['cash']}" if pd.notna(e.get("cash")) else ""
            events.append({"type": label, "date": str(e["date"]), "days": days,
                           "note": note, "level": "info"})

    for m, d, label in FINANCIAL_DEADLINES:
        for year in (today.year, today.year + 1):
            dd = date(year, m, d)
            days = (dd - today).days
            if 0 <= days <= 14:
                events.append({"type": f"{label}公告期限", "date": str(dd), "days": days,
                               "note": "公告前後波動通常放大", "level": "warn" if days <= 7 else "info"})

    # 月營收對每一檔都一樣，只在很接近時才提，不然每天都在洗版
    for year, month in ((today.year, today.month), (today.year + (today.month == 12),
                                                    today.month % 12 + 1)):
        dd = date(year, month, REVENUE_DAY)
        days = (dd - today).days
        if 0 <= days <= 5:
            events.append({"type": "月營收公告期限", "date": str(dd), "days": days,
                           "note": "上月營收將於此日前公告", "level": "info"})

    return sorted(events, key=lambda x: x["days"])
