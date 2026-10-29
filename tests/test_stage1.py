"""階段 1（低風險過濾 + 標記）合成測試
情境：
 A 正常開盤跳空      → 開盤價異常 應報
 B 下午才首次成交    → 開盤價異常 不報（首根不在 09:30+5 分內）
 C 前日停牌（復牌）  → 價格類不報；量能類仍報但帶「復牌未滿 N 個交易日」備註
 D 距前一根 40 分鐘  → 1分鐘急漲急跌 不報
 E 連續分鐘急漲      → 1分鐘急漲急跌 應報
 F 除息日價格類      → 應報且帶「除息日」備註
 G 同時除息 + 復牌   → 兩個備註各自落在對應規則上
"""
import sys
import pandas as pd

sys.path.insert(0, "src")
from common import load_yaml                      # noqa: E402
from rules import HALT_TAG_RULES, run_rules                       # noqa: E402

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
    # halted = 前一交易日停牌，價格類據此排除（隔天起 prev_close 已是真實價，不再有假跳空）
    "halted": [False, False, True, False, False, False, True],
    # halted_5 / halted_20 = 近 N 個交易日內曾停牌，量能類據此標記（窗格對齊各指標的平均天數）
    "halted_5": [False, False, True, False, False, False, True],
    "halted_20": [False, False, True, False, False, False, True],
    "shares": [1e9] * 7,
})
uni = pd.DataFrame({"code": codes, "name": [f"測試{c}" for c in codes], "vcm_pct": 10.0})

# 量能指標需要同時點基準才會計算（歷史不足就不報，不退回舊分母），故供一份合成基準
base = pd.DataFrame([{"code": c, "m": m, "avg1": 1000.0, "avg3": 3000.0, "avgcum": 10000.0,
                      "n_short": 5, "n_long": 20} for c in codes for m in (570, 571)])
a = run_rules(bars, stats, uni, cfg, daily_close=None, ex_div_codes={6, 7}, baseline=base)
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
    ("C 停牌 → 量能帶復牌備註",        "復牌未滿 20 個交易日" in note(3, "成交量暴增")),
    ("D 40 分鐘跳空 → 不報 1 分鐘",    not has(4, "1分鐘急漲急跌")),
    ("E 連續分鐘急漲 → 報 1 分鐘",     has(5, "1分鐘急漲急跌")),
    ("F 除息 → 開盤價異常帶備註",      note(6, "開盤價異常") == "除息日"),
    ("G 除息+停牌 → 價格類已排除",     not has(7, "開盤價異常")),
    ("G 除息+停牌 → 量能帶復牌備註",   "復牌未滿 20 個交易日" in note(7, "成交量暴增")),
    ("note 欄位存在且無 NaN",          "note" in a.columns and a["note"].notna().all()),
]
# ---- 復牌標記的窗格與適用範圍（2026-10-29 更正）----
# 5 號近 5 日曾停牌但近 20 日的旗標為 False（合成情境，僅為驗證兩個窗格各自獨立作用）
# 供日線資料，讓換手率異常也會產生警報，才驗得到「它不該被標記」
dvol = pd.Series(3e8, index=codes)      # 3e8 / 1e9 股 = 30% 換手率 → 橙燈
dclose = pd.Series(100.0, index=codes)
stats_w = stats.copy()
stats_w["halted"] = False
stats_w["halted_5"] = [False] * 4 + [True] + [False] * 2
stats_w["halted_20"] = [False] * 5 + [True] + [False]
aw = run_rules(bars, stats_w, uni, cfg, daily_close=dclose, daily_volume=dvol,
               ex_div_codes={6, 7}, baseline=base)


def note_w(c, r):
    m = aw[(aw["code"] == c) & (aw["rule"] == r)]
    return m["note"].iloc[0] if len(m) else "<無此警報>"


checks += [
    # 短窗格的正向行為在 test_stage4 驗（連續放量在此 fixture 的兩根 K 線下不會觸發）
    ("映射表：short 組 = 分母取 5 日的兩條指標",
     set(HALT_TAG_RULES["short"]) == {"3分鐘量暴增", "連續放量"}),
    ("映射表：long 組 = 分母取 20 日的兩條指標",
     set(HALT_TAG_RULES["long"]) == {"成交量暴增", "冬眠股異動"}),
    ("映射表：換手率異常不在任何一組",
     "換手率異常" not in HALT_TAG_RULES["short"] + HALT_TAG_RULES["long"]),
    ("halted_5 不套在 20 日分母的指標上（成交量暴增）",
     "復牌" not in note_w(5, "成交量暴增")),
    ("halted_20 只套在 20 日分母的指標上（成交量暴增）",
     "復牌未滿 20 個交易日" in note_w(6, "成交量暴增")),
    ("halted_20 不套在 5 日分母的指標上（連續放量）",
     "復牌" not in note_w(6, "連續放量")),
    ("換手率異常不再被標記（分母是股本，不經歷史均值，不會失真）",
     all("復牌" not in n for n in aw.loc[aw["rule"] == "換手率異常", "note"])),
]

# 舊格式基準檔（只有 halted、沒有新欄位）須自動退回原本的「復牌首日」行為
stats_old = stats.drop(columns=["halted_5", "halted_20"])
ao = run_rules(bars, stats_old, uni, cfg, daily_close=dclose, daily_volume=dvol,
               ex_div_codes={6, 7}, baseline=base)
m_old = ao[(ao["code"] == 3) & (ao["rule"] == "成交量暴增")]
checks.append(("舊格式基準檔自動退回「復牌首日」，不會靜默失去標記",
               len(m_old) == 1 and m_old["note"].iloc[0] == "復牌首日（倍數失真）"))

bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  OK  " if ok else "  FAIL") + "  " + n)
print()
print(a[["code", "rule", "level", "note"]].to_string(index=False))
sys.exit(1 if bad else 0)
