"""新聞旗標。

**這一層無法回測。** 沒有免費的歷史新聞情緒資料庫，所以沒辦法證明它對報酬有沒有幫助。
因此它刻意不進入評分：只標記、不加分、不排序。用途是讓你在看到一檔技術面漂亮的股票時，
知道它昨天剛被爆出什麼事——這是人做決策時需要的資訊，不是量化因子。

資料由每日執行的流程用網路搜尋寫入 data/news/news_flags.json，格式：

{
  "scanned_at": "2026-08-24 21:00",
  "flags": {
    "2603": [
      {"level": "warn", "text": "傳出美國線運價鬆動", "source": "https://..."},
      {"level": "info", "text": "8/28 法說會", "source": "https://..."}
    ]
  }
}

level：warn = 需要留意的負面或不確定事件；info = 中性或行程提醒；good = 正面消息。
刻意不用「利多／利空」這種帶方向判斷的字眼，因為判斷方向的是你不是程式。
"""
import json
import os
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWS_PATH = os.path.join(ROOT, "data", "news", "news_flags.json")

STALE_HOURS = 30  # 超過這個時間就標記為過期，避免拿昨天的新聞當今天的判斷


def load_flags() -> dict:
    if not os.path.exists(NEWS_PATH):
        return {"scanned_at": None, "flags": {}, "stale": True}
    try:
        with open(NEWS_PATH, encoding="utf-8") as f:
            d = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"scanned_at": None, "flags": {}, "stale": True}

    stale = True
    if d.get("scanned_at"):
        try:
            age = datetime.now() - datetime.strptime(d["scanned_at"], "%Y-%m-%d %H:%M")
            stale = age.total_seconds() > STALE_HOURS * 3600
        except ValueError:
            pass
    d["stale"] = stale
    d.setdefault("flags", {})
    return d


def for_code(code: str, flags: dict) -> list[dict]:
    return flags.get("flags", {}).get(str(code).zfill(4), [])


def bias_summary(flags: dict) -> dict:
    """統計這次掃描的利多／利空比例。

    為什麼要把這個數字放上介面：觀察池是用成交值挑的（都是漲上來的股票），
    財經媒體對漲的股票寫的就是多頭稿。所以「利多很多」不代表這些股票好，
    只代表新聞來源本身有方向偏誤。把比例攤開，你才不會把它當成獨立的第二意見。
    """
    good = bad = neutral = 0
    for items in flags.get("flags", {}).values():
        for it in items:
            d = it.get("direction", "")
            if d == "利多":
                good += 1
            elif d == "利空":
                bad += 1
            else:
                neutral += 1
    return {"利多": good, "利空": bad, "中性": neutral, "合計": good + bad + neutral}


def save_flags(flags: dict[str, list[dict]], scanned_at: str | None = None) -> str:
    os.makedirs(os.path.dirname(NEWS_PATH), exist_ok=True)
    payload = {
        "scanned_at": scanned_at or datetime.now().strftime("%Y-%m-%d %H:%M"),
        "flags": flags,
    }
    with open(NEWS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return NEWS_PATH
