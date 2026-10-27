"""階段 3（量能雙軌 A/B）合成測試"""
import io
import sys
import pandas as pd

sys.path.insert(0, "src")
from common import load_yaml                 # noqa: E402
from rules import run_rules                  # noqa: E402

DAY = pd.Timestamp("2026-10-27")
cfg = load_yaml("rules.yaml")
checks = []


def bar(c, hm, px, v):
    t = DAY + pd.Timedelta(hours=int(hm[:2]), minutes=int(hm[3:]))
    return dict(code=c, time=t, open=px, high=px, low=px, close=px, volume=v)


# 每檔兩根，股價 100 元 → 當下累計成交額 = 100 × 總量
# avg_vol = 1e6 → expected_volume 會依時間比例縮放，量只要夠大一定超過紅燈倍數
specs = [
    # (代碼, 20日均成交額, 總量, 期望是否報成交量暴增, 說明)
    (51, 5e8,  40_000, True,  "A 軌（均額 5 億）成交額 400 萬 ≥ 100 萬 → 判定"),
    (52, 5e8,   5_000, False, "A 軌 成交額 50 萬 < 100 萬 → 不判定"),
    (53, 1e6,  40_000, False, "B 軌（均額 100 萬）成交額 400 萬 < 500 萬 → 不判定"),
    (54, 1e6,  80_000, True,  "B 軌 成交額 800 萬 ≥ 500 萬 → 判定"),
    (55, None, 80_000, True,  "均額缺值 → 歸 B 軌，成交額 800 萬 → 判定"),
    (56, None, 40_000, False, "均額缺值 → 歸 B 軌，成交額 400 萬 → 不判定"),
]
rows = []
for c, _, v, _, _ in specs:
    rows += [bar(c, "10:00", 100, v // 2), bar(c, "10:01", 100, v - v // 2)]
bars = pd.DataFrame(rows)
codes = [s[0] for s in specs]
uni = pd.DataFrame({"code": codes, "name": "X", "vcm_pct": [None] * len(codes)})
stats = pd.DataFrame({"code": codes, "prev_close": 100.0, "ma": 100.0, "avg_vol": 1e3,          # 均量刻意設小，確保倍數遠超紅燈，測的是成交額下限而非倍數
                      "avg_turnover": [s[1] for s in specs], "halted": False})
a = run_rules(bars, stats, uni, cfg)
got = set(a.loc[a["rule"] == "成交量暴增", "code"])
for c, _, _, want, desc in specs:
    checks.append((f"{desc} → {'報' if want else '不報'}", (c in got) == want))

# 舊的單軌格式仍可運作（向後相容）
old = dict(cfg)
old["vol_ratio"] = {"黃": 3, "橙": 5, "紅": 10}
a2 = run_rules(bars, stats, uni, old)
checks.append(("舊單軌格式仍可運作（全部符合倍數者都報）",
               set(a2.loc[a2["rule"] == "成交量暴增", "code"]) == set(codes)))

# ruamel 巢狀寫回不會破壞註解與其他鍵
from ruamel.yaml import YAML                 # noqa: E402
ry = YAML()
doc = ry.load(open("config/rules.yaml", encoding="utf-8").read())
node = doc
for part in "vol_ratio.A".split("."):
    node = node[part]
node["黃"] = 4
buf = io.StringIO()
ry.dump(doc, buf)
out = buf.getvalue()
checks += [
    ("巢狀參數寫回正確", "黃: 4" in out),
    ("寫回後保留 min_turnover", "min_turnover: 1000000" in out),
    ("寫回後保留註解", "pool_split_turnover" in out and "#" in out),
]

bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  OK  " if ok else "  FAIL") + "  " + n)
sys.exit(1 if bad else 0)
