"""階段 1（低風險過濾 + 標記）合成測試
情境：
 A 正常開盤跳空      → 開盤價異常 應報
 B 下午才首次成交    → 開盤價異常 不報（首根不在 09:30+5 分內）
 C 前日停牌（復牌）  → 價格類不報；量能類仍報但帶「復牌首日」備註
 D 距前一根 40 分鐘  → 1分鐘急漲急跌 不報
 E 連續分鐘急漲      → 1分鐘急漲急跌 應報
 F 除息日價格類      → 應報且帶「除息日」備註
 G 同時除息 + 復牌   → 兩個備註各自落在對應規則上
"""
import sys
import pandas as pd

sys.path.insert(0, "src")
from common import load_yaml                      # noqa: E402
from rules import run_rules                       # noqa: E402

DAY = pd.Timestamp("2026-10-27")
cfg = load_yaml("rules.yaml")


def bar(code, hhmm, close, vol, open_=None):
    t = DAY + pd.Timedelta(hours=int(hhmm[:2]), minutes=int(hhmm[3:]))
    o = close if open_ is None else open_
    return dict(code=code, time=t, open=o, high=max(o, close), low=min(o, close),
                close=close, volume=vol)


rows = []
# A 正常開盤跳空 +10%
rows += [bar(1, "09:30", 110, 500_000, open_=110), bar(1, "09:31", 110, 500_000)]
# B 下午才首次成交，價格 +10%
rows += [bar(2, "13:05", 110, 500_000, open_=110), bar(2, "13:06", 110, 500_000)]
# C 前日停牌，開盤 +10%、量能暴增
rows += [bar(3, "09:30", 110, 5_000_000, open_=110), bar(3, "09:31", 110, 5_000_000)]
# D 09:30 一根，之後 10:12 才有一根且 +6%
rows += [bar(4, "09:30", 100, 500_000), bar(4, "10:12", 106, 500_000)]
# E 連續分鐘，09:31 相對 09:30 +6%
rows += [bar(5, "09:30", 100, 500_000), bar(5, "09:31", 106, 500_000)]
# F 除息日，開盤 -10%
rows += [bar(6, "09:30", 90, 500_000, open_=90), bar(6, "09:31", 90, 500_000)]
# G 除息 + 前日停牌，開盤 -10% 且量能暴增
rows += [bar(7, "09:30", 90, 5_000_000, open_=90), bar(7, "09:31", 90, 5_000_000)]
bars = pd.DataFrame(rows)

codes = [1, 2, 3, 4, 5, 6, 7]
stats = pd.DataFrame({
    "code": codes,
    "prev_close": 100.0,
    "ma": 100.0,
    "avg_vol": [1_000_000] * 7,
    "avg_turnover": [1e9] * 7,          # 全部視為活躍股
    "halted": [False, False, True, False, False, False, True],
    "shares": [1e9] * 7,
})
uni = pd.DataFrame({"code": codes, "name": [f"測試{c}" for c in codes], "vcm_pct": 10.0})

a = run_rules(bars, stats, uni, cfg, profile=None, daily_close=None, ex_div_codes={6, 7})
got = {(int(r.code), r.rule): r for r in a.itertuples()}


def has(code, rule):
    return (code, rule) in got


def note(code, rule):
    return got[(code, rule)].note if has(code, rule) else None


checks = [
    ("A 正常開盤跳空 → 報",            has(1, "開盤價異常")),
    ("B 下午首根 → 不報開盤價異常",    not has(2, "開盤價異常")),
    ("C 停牌 → 不報開盤價異常",        not has(3, "開盤價異常")),
    ("C 停牌 → 不報即時漲跌幅",        not has(3, "即時漲跌幅")),
    ("C 停牌 → 成交量暴增仍報",        has(3, "成交量暴增")),
    ("C 停牌 → 量能帶復牌首日備註",    note(3, "成交量暴增") == "復牌首日（倍數失真）"),
    ("D 40 分鐘跳空 → 不報 1 分鐘",    not has(4, "1分鐘急漲急跌")),
    ("E 連續分鐘急漲 → 報 1 分鐘",     has(5, "1分鐘急漲急跌")),
    ("F 除息 → 開盤價異常帶備註",      note(6, "開盤價異常") == "除息日"),
    ("G 除息+停牌 → 價格類已排除",     not has(7, "開盤價異常")),
    ("G 除息+停牌 → 量能帶復牌備註",   note(7, "成交量暴增") == "復牌首日（倍數失真）"),
    ("note 欄位存在且無 NaN",          "note" in a.columns and a["note"].notna().all()),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  OK  " if ok else "  FAIL") + "  " + n)
print()
print(a[["code", "rule", "level", "note"]].to_string(index=False))
sys.exit(1 if bad else 0)
