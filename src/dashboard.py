"""把每日報告 + 回測結果組成單檔 HTML 儀表板。"""
import json
import os
import sys

import sys as _sys
# Windows 終端機預設 cp950，輸出重導到檔案時會因為 ✓ 這類字元整支掛掉。
# 這幾行必須在任何 print 之前執行。
for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "output")

# 回測結論——數字變了要一起改，別讓儀表板說謊
VERDICT = {
    "verdict_title": "IC 最高的因子加進去反而讓績效變差——這是這個專案目前最違反直覺的發現",
    "verdict": (
        "106 檔、技術面 + 籌碼面：總報酬 228.7%、最大回撤 -18.7%、Sharpe 2.13、勝率 45.2%；"
        "同期間等權重買進持有這 106 檔是 213.1% / -31.7% / 1.95。三項都贏。"
        "但把 IC 最高的營收年增（樣本內 0.104 / 樣本外 0.105，遠高於籌碼面的 0.05 級距）加進評分後，"
        "報酬掉到 158.7%、回撤惡化到 -23.6%，四種權重配置都一樣。"
        "原因大概是 IC 衡量整個橫斷面的排序能力，而這套策略只取前 5 名——高 IC 不代表在最極端那一端有用。"
        "所以基本面留在卡片上給人判斷，不進排序。當沖比率也是同樣情況（IC 0.051 但實測扣分）而未採用。"
        "另外必須說明：樣本外只有 82 筆交易，對交易報酬做 bootstrap 後各配置的信賴區間彼此重疊，"
        "所以「哪個配置比較好」在統計上其實分不太出來，選 A 是因為它同時最簡單、回撤最小。"
    ),
}

OOS_TABLE = [
    ["樣本內 24/08–25/08", "+10.0%｜回撤 -17.7%", "+18.1%｜回撤 -31.7%"],
    ["樣本外 25/08–26/08", "+156.7%｜回撤 -16.0%", "+175.1%｜回撤 -29.9%"],
]


def build(report_path=None, chart_path=None, out_path=None) -> str:
    report_path = report_path or os.path.join(OUT, "daily_report.json")
    chart_path = chart_path or os.path.join(OUT, "chart_data.json")
    out_path = out_path or os.path.join(OUT, "dashboard.html")

    with open(report_path, encoding="utf-8") as f:
        report = json.load(f)
    with open(chart_path, encoding="utf-8") as f:
        chart = json.load(f)

    report["chart"] = chart
    report["backtest"] = VERDICT
    report["oos"] = OOS_TABLE

    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html"),
              encoding="utf-8") as f:
        html = f.read()

    payload = json.dumps(report, ensure_ascii=False).replace("</", "<\\/")
    html = html.replace("__PAYLOAD__", payload)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


if __name__ == "__main__":
    p = build()
    print(f"{p}  ({os.path.getsize(p)/1024:.0f} KB)")
