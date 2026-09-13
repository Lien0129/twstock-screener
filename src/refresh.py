"""一行指令重算全部輸出：資料檢查 → 訊號 → 圖表資料 → 儀表板。

    python3 src/refresh.py            # 資料有問題就停下來
    python3 src/refresh.py --force    # 資料有問題也照跑（儀表板會顯示警告橫幅）

本機每晚由 daily_update.py 自動呼叫；雲端則是在使用者說「更新」時，
把他電腦上的資料搬過來後跑這一支，再發布 Artifact。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

import sys as _sys
# Windows 終端機預設 cp950，輸出重導到檔案時會因為 ✓ 這類字元整支掛掉。
# 這幾行必須在任何 print 之前執行。
for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


SRC = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SRC)
sys.path.insert(0, SRC)


def run(script: str) -> bool:
    print(f"\n▸ {script}")
    r = subprocess.run([sys.executable, os.path.join(SRC, script)],
                       cwd=ROOT, capture_output=True, text=True)
    # signals.py 會把整份報告倒到 stdout，只留最後兩行當進度即可
    lines = [l for l in (r.stdout or "").strip().splitlines() if l.strip()][-2:]
    for l in lines:
        print("  " + l.strip())
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        print(f"  ✗ 失敗：{err[-1] if err else '未知錯誤'}")
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="資料有問題也照跑")
    args = ap.parse_args()

    t0 = time.time()
    import verify_data
    problems = verify_data.check()
    if problems and not args.force:
        print("\n資料有問題，先不重算。確定要跑就加 --force，儀表板會標明資料狀態。")
        return 1

    for script in ("chart_data.py", "signals.py", "dashboard.py"):
        if not run(script):
            print("\n中止。")
            return 1

    import json
    with open(os.path.join(ROOT, "output", "daily_report.json"), encoding="utf-8") as f:
        rep = json.load(f)
    print("\n" + "─" * 58)
    print(f"收盤資料 {rep['data_date']}　訊號適用於 {rep['訊號適用日']} 開盤")
    for sleeve, label in (("swing", "波段池"), ("position", "長線池")):
        picks = rep["picks"][sleeve]
        if picks:
            top = "、".join(f"{p['code']} {p['name']}({p['score']:+.2f})" for p in picks[:3])
            print(f"{label} {len(picks)} 檔：{top}")
        else:
            print(f"{label} 今天沒有符合條件的標的")
    hold = rep.get("holdings") or []
    if hold:
        print("持股：" + "、".join(f"{h['code']} {h.get('name','')} → {h['建議']}" for h in hold))
    for w in rep.get("資料警告", []):
        print("⚠️  " + w)
    print(f"\noutput/dashboard.html 已更新（{int(time.time()-t0)} 秒）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
