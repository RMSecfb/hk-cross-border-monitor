"""規則引擎：對「全部股票的當日 1 分鐘 K 線」一次計算所有監控指標
輸入
  bars    : code, time, open, high, low, close, volume（time 為香港時間、不含時區）
  stats   : code, prev_close, ma, avg_vol, [shares]          ← eod.py 產生
  universe: code, name, vcm_pct
  profile : code, bucket, vol_per_min（可為 None）            ← eod.py 產生
輸出
  alerts  : time, code, name, rule, level, value, detail
"""
import numpy as np
import pandas as pd

LEVELS = ["紅", "橙", "黃"]
COLS = ["time", "code", "rule", "level", "value", "detail"]
AM, PM = (pd.Timedelta("9h30m"), pd.Timedelta("12h")), (pd.Timedelta("13h"), pd.Timedelta("16h"))
VCM_AM, VCM_PM = (pd.Timedelta("9h45m"), pd.Timedelta("12h")), (pd.Timedelta("13h15m"), pd.Timedelta("15h40m"))


def grade(values, th):
    """依黃/橙/紅閾值（取絕對值）回傳等級，未達回傳 None"""
    v = np.abs(np.asarray(values, dtype=float))
    return np.select([v >= th["紅"], v >= th["橙"], v >= th["黃"]], LEVELS, default=None)


def tod(t):
    """時間 → 當日時刻（Timedelta）"""
    return t - t.dt.normalize()


def minutes_elapsed(t):
    """持續交易時段已過分鐘數（全日 330 分鐘）"""
    m = tod(t)
    am = ((m.clip(AM[0], AM[1]) - AM[0]).dt.total_seconds() / 60)
    pm = ((m.clip(PM[0], PM[1]) - PM[0]).dt.total_seconds() / 60)
    return (am + pm + 1).clip(lower=1, upper=330)


def bucket(t):
    """30 分鐘時段代號，如 0930、1000 … 1530"""
    m = tod(t).dt.total_seconds() // 60
    b = (m // 30) * 30
    return (b // 60).astype(int).astype(str).str.zfill(2) + (b % 60).astype(int).astype(str).str.zfill(2)


def expected_volume(codes, times, profile, avg_vol):
    """到某時點為止的「正常累計成交量」
    有 profile（由 1 分鐘資料累積的各時段均量）時，依時段加總，反映開收盤量大、午盤量小的分佈；
    沒有時退回 20 日均量 × 已交易時間比例。兩者來源一致才可比（Yahoo 1 分鐘量加總與日線量常不一致）。"""
    codes, times = pd.Series(codes).reset_index(drop=True), pd.Series(times).reset_index(drop=True)
    fallback = pd.Series(avg_vol).reset_index(drop=True) * minutes_elapsed(times).values / 330
    if profile is None or profile.empty:
        return fallback.values
    pv = profile.pivot_table(index="code", columns="bucket", values="vol_per_min", aggfunc="sum").fillna(0) * 30
    pv = pv.reindex(columns=sorted(pv.columns))
    cum = pv.cumsum(axis=1)
    cur_b = bucket(times)
    m = tod(times).dt.total_seconds() / 60
    frac = ((m % 30) + 1) / 30
    out = []
    for c, b, f in zip(codes, cur_b, frac):
        if c not in pv.index or b not in pv.columns:
            out.append(np.nan)
            continue
        i = pv.columns.get_loc(b)
        before = cum.loc[c].iat[i - 1] if i > 0 else 0.0
        out.append(before + pv.loc[c].iat[i] * min(f, 1.0))
    exp = pd.Series(out, dtype=float)
    return exp.where(exp > 0).fillna(fallback).values


def emit(df, mask, rule, levels, value, detail):
    """把符合條件的列轉成警報表"""
    lv = np.asarray(levels, dtype=object)
    ok = np.asarray(mask, dtype=bool) & pd.notna(lv)
    if not ok.any():
        return pd.DataFrame(columns=COLS)
    return pd.DataFrame({"time": df["time"].values[ok], "code": df["code"].values[ok], "rule": rule,
                         "level": lv[ok], "value": np.round(np.asarray(value, dtype=float)[ok], 2),
                         "detail": detail})


def run_rules(bars, stats, universe, cfg, profile=None, daily_close=None):
    if bars.empty:
        return pd.DataFrame(columns=COLS + ["name"])
    b = bars.sort_values(["code", "time"]).reset_index(drop=True)
    b = b.merge(stats, on="code", how="left").merge(universe[["code", "vcm_pct"]], on="code", how="left")
    g = b.groupby("code", sort=False)
    out = []

    # ---- 基準量：歷史同時段每分鐘均量，無則 20 日均量/330 ----
    # 分鐘級量能規則（3 分鐘量暴增、連續放量）只用 1 分鐘資料累積的基準；累積未滿 min_profile_days 前不判斷，
    # 因為以日線均量平均分攤到每分鐘，會把開收盤的正常大量誤判為暴增。
    b["bucket"] = bucket(b["time"])
    if profile is not None and not profile.empty:
        prof = profile.copy()
        if "n" in prof:
            prof = prof[prof["n"] >= cfg.get("min_profile_days", 5)]
        b = b.merge(prof[["code", "bucket", "vol_per_min"]], on=["code", "bucket"], how="left")
    else:
        b["vol_per_min"] = np.nan
    b["base_min"] = b["vol_per_min"].replace(0, np.nan)
    g = b.groupby("code", sort=False)

    # ---- 冷門股過濾（當日成交額過低者不做短時指標）；活躍股 = 20 日平均成交額達門檻 ----
    b["turnover_hkd"] = (b["close"] * b["volume"]).groupby(b["code"]).transform("sum")
    liquid = b["turnover_hkd"] >= cfg["min_turnover_hkd"]
    avg_to = b["avg_turnover"] if "avg_turnover" in b else pd.Series(np.inf, index=b.index)
    active = liquid & (avg_to >= cfg["active_min_avg_turnover_hkd"])

    # ---- 1. 1 分鐘急漲急跌（對上一筆成交分鐘）----
    chg1 = (b["close"] / g["close"].shift(1) - 1) * 100
    out.append(emit(b, liquid, "1分鐘急漲急跌", grade(chg1, cfg["chg_1m"]), chg1, "vs 上一分鐘"))

    # ---- 2. 5 分鐘急漲急跌 & VCM：參考價 = 5 分鐘前最後成交價（同一時段內）----
    ref = b[["code", "time", "close"]].rename(columns={"time": "ref_time", "close": "ref_px"})
    ref["rt"] = ref["ref_time"]
    probe = b[["code", "time"]].assign(ref_time=b["time"] - pd.Timedelta("5min"))
    probe = pd.merge_asof(probe.sort_values("ref_time"), ref.sort_values("ref_time"),
                          on="ref_time", by="code", direction="backward",
                          suffixes=("", "_r")).sort_values(["code", "time"]).reset_index(drop=True)
    b["ref_px"] = probe["ref_px"].values
    m = tod(b["time"])
    rt = tod(pd.Series(probe["rt"].values))
    same_session = ((m < AM[1]) | (rt.values >= PM[0])) & probe["rt"].notna().values  # 午市不沿用早市參考價
    chg5 = (b["close"] / b["ref_px"] - 1) * 100
    out.append(emit(b, liquid & same_session, "5分鐘急漲急跌", grade(chg5, cfg["chg_5m"]), chg5, "vs 5 分鐘前"))

    in_vcm = (((m >= VCM_AM[0]) & (m < VCM_AM[1])) | ((m >= VCM_PM[0]) & (m < VCM_PM[1]))) & \
             b["vcm_pct"].notna() & same_session
    dev = chg5.abs()
    vcm_lv = np.where(dev >= b["vcm_pct"], "紅", None)
    out.append(emit(b, in_vcm, "VCM觸發(模擬)", vcm_lv, chg5, "偏離參考價達 VCM 門檻"))
    early_lv = np.where((dev >= b["vcm_pct"] * cfg["vcm_early_ratio"]) & (dev < b["vcm_pct"]), "黃", None)
    out.append(emit(b, in_vcm, "VCM內部預警", early_lv, chg5, f"達門檻 {cfg['vcm_early_ratio']:.0%}"))

    # ---- 3. 3 分鐘量暴增、連續放量（以時間視窗計算）----
    bt = b.set_index("time")
    vol3 = bt.groupby("code", sort=False)["volume"].rolling("3min").sum().values
    r3 = vol3 / (3 * b["base_min"])
    out.append(emit(b, active, "3分鐘量暴增", grade(r3, cfg["vol_3m"]), r3, "3 分鐘量 / 同時段均量"))

    n = cfg["vol_streak_minutes"]
    r1 = (b["volume"] / b["base_min"]).fillna(0)
    bt = b.assign(r1=r1).set_index("time").groupby("code", sort=False)["r1"]
    streak_min = bt.rolling(f"{n}min").min().values
    streak_cnt = bt.rolling(f"{n}min").count().values
    streak_lv = np.where(streak_cnt >= n, grade(streak_min, cfg["vol_streak"]), None)
    out.append(emit(b, active, "連續放量", streak_lv, streak_min, f"連續 {n} 分鐘每分鐘量倍數"))

    # ---- 4. 全日型指標（每檔取最後一筆）----
    g = b.groupby("code", sort=False)
    last = g.tail(1).copy()
    first = g.head(1).set_index("code")
    last["cum_vol"] = g["volume"].sum().reindex(last["code"]).values
    last["elapsed"] = minutes_elapsed(last["time"]).values
    px = last["close"] if daily_close is None else last["code"].map(daily_close).fillna(last["close"])

    chg = (px / last["prev_close"] - 1) * 100
    out.append(emit(last, chg.notna(), "即時漲跌幅", grade(chg, cfg["day_change"]), chg, "vs 前收"))

    madev = (px / last["ma"] - 1) * 100
    last_active = (last["avg_turnover"] >= cfg["active_min_avg_turnover_hkd"]) if "avg_turnover" in last else True
    out.append(emit(last, madev.notna() & last_active, "偏離均線", grade(madev, cfg["ma_dev"]), madev, f"vs MA{cfg['ma_days']}"))

    vr = last["cum_vol"] / expected_volume(last["code"], last["time"], profile, last["avg_vol"])
    out.append(emit(last, vr.notna() & np.isfinite(vr), "成交量暴增", grade(vr, cfg["vol_ratio"]), vr,
                    "累計量 / 同時點均量"))

    if "shares" in last and last["shares"].notna().any():
        tr = last["cum_vol"] / last["shares"] * 100
        out.append(emit(last, tr.notna(), "換手率異常", grade(tr, cfg["turnover"]), tr, "成交量 / 股本"))

    gap = (first["open"] / first["prev_close"] - 1) * 100
    f = first.reset_index()
    out.append(emit(f, gap.notna().values, "開盤價異常", grade(gap.values, cfg["open_gap"]), gap.values, "開盤 vs 前收"))

    # ---- 5. 收市競價（只在盤後提供 daily_close 時計算）----
    if daily_close is not None:
        cts = b[tod(b["time"]) < PM[1]].groupby("code").tail(1).copy()
        cas = (cts["code"].map(daily_close) / cts["close"] - 1) * 100
        cts["time"] = cts["time"].dt.normalize() + pd.Timedelta("16h10m")
        out.append(emit(cts, cas.notna(), "收市競價異常", grade(cas, cfg["cas_dev"]), cas,
                        "收市價 vs 16:00 前最後價"))

    alerts = pd.concat([o for o in out if not o.empty], ignore_index=True) if any(
        not o.empty for o in out) else pd.DataFrame(columns=COLS)
    alerts = alerts.merge(universe[["code", "name"]], on="code", how="left")
    return alerts.sort_values(["time", "code"]).reset_index(drop=True)


def apply_cooldown(alerts, cooldown_min):
    """同一股票、同一規則、同一等級，冷卻期內只留第一筆"""
    if alerts.empty:
        return alerts
    keep, last = [], {}
    for row in alerts.sort_values("time").itertuples(index=False):
        k = (row.code, row.rule, row.level)
        cd = pd.Timedelta(minutes=cooldown_min.get(row.rule, 5))
        if k not in last or row.time - last[k] >= cd:
            keep.append(row)
            last[k] = row.time
    return pd.DataFrame(keep, columns=alerts.columns).reset_index(drop=True)
