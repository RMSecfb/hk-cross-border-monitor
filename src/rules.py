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

# 規則分組：供「只加註不排除」的標記使用
PRICE_RULES = ["即時漲跌幅", "開盤價異常", "1分鐘急漲急跌", "5分鐘急漲急跌", "偏離均線"]
VOLUME_RULES = ["成交量暴增", "3分鐘量暴增", "連續放量", "換手率異常"]
# 分鐘級規則：同一批次內每檔只留最嚴重的一筆（原本每分鐘都判斷，一波拉抬會報出數十筆）
BATCH_MAX_RULES = ["3分鐘量暴增", "連續放量"]
LEVEL_RANK = {lv: i for i, lv in enumerate(LEVELS)}


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


def run_rules(bars, stats, universe, cfg, profile=None, daily_close=None, ex_div_codes=None,
              daily_volume=None):
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

    # ---- 停牌排除：前一交易日成交量 0 且 OHLC 全同者，復牌跳空非交易異常 ----
    # 全市場每日有 22–29% 的股票處於停牌狀態；實測 10/27 開盤價異常 226 檔中有 71 檔（31%）屬此類。
    not_halted = ~b["halted"].fillna(False).astype(bool) if "halted" in b else pd.Series(True, index=b.index)
    tradable = liquid & not_halted      # 價格類用：停牌排除
    # 量能類（成交量暴增 / 3 分鐘量暴增 / 連續放量 / 換手率）不排除復牌股，改在 note 標記「復牌首日」

    # ---- 1. 1 分鐘急漲急跌（對上一筆成交分鐘）----
    # 「上一根」未必是一分鐘前：冷門股可能隔數十分鐘才有下一筆成交，該漲跌幅實際涵蓋很長一段時間。
    prev_t = g["time"].shift(1)
    gap1_ok = (b["time"] - prev_t).dt.total_seconds().le(cfg.get("max_prev_gap_min", 3) * 60)
    chg1 = (b["close"] / g["close"].shift(1) - 1) * 100
    out.append(emit(b, tradable & gap1_ok.fillna(False), "1分鐘急漲急跌",
                    grade(chg1, cfg["chg_1m"]), chg1, "vs 上一分鐘"))

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
    # 參考價距今過久者不計算（同 1 分鐘規則的理由）：找不到 5 分鐘前的價時會回退到更早的成交
    gap5_ok = (b["time"] - pd.Series(probe["rt"].values)).dt.total_seconds().le(
        (5 + cfg.get("max_prev_gap_min", 3)) * 60).fillna(False).values
    chg5 = (b["close"] / b["ref_px"] - 1) * 100
    out.append(emit(b, tradable & same_session & gap5_ok, "5分鐘急漲急跌",
                    grade(chg5, cfg["chg_5m"]), chg5, "vs 5 分鐘前"))

    in_vcm = (((m >= VCM_AM[0]) & (m < VCM_AM[1])) | ((m >= VCM_PM[0]) & (m < VCM_PM[1]))) & \
             b["vcm_pct"].notna() & same_session
    # 原「VCM觸發(模擬)」與「VCM內部預警」合併為單一規則，改以「佔該股 VCM 門檻的比例」分三級。
    # 紅燈承接原觸發位置；不再用「模擬」一詞，避免被誤認為交易所實際觸發紀錄。
    vcm_ratio = chg5.abs() / b["vcm_pct"]
    out.append(emit(b, in_vcm, "VCM預警", grade(vcm_ratio, cfg["vcm_ratio"]), chg5,
                    "偏離參考價佔 VCM 門檻比例"))

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

    # 全日型指標的過濾：流動性與停牌分開，各指標依其適用條件組合
    last_liquid = last["turnover_hkd"] >= cfg["min_turnover_hkd"]
    last_active = (last["avg_turnover"] >= cfg["active_min_avg_turnover_hkd"]) if "avg_turnover" in last \
        else pd.Series(True, index=last.index)
    last_not_halted = ~last["halted"].fillna(False).astype(bool) if "halted" in last \
        else pd.Series(True, index=last.index)

    chg = (px / last["prev_close"] - 1) * 100
    out.append(emit(last, chg.notna() & last_liquid & last_not_halted, "即時漲跌幅",
                    grade(chg, cfg["day_change"]), chg, "vs 前收"))

    madev = (px / last["ma"] - 1) * 100
    out.append(emit(last, madev.notna() & last_active & last_not_halted, "偏離均線",
                    grade(madev, cfg["ma_dev"]), madev, f"vs MA{cfg['ma_days']}"))

    vr = last["cum_vol"] / expected_volume(last["code"], last["time"], profile, last["avg_vol"])
    out.append(emit(last, vr.notna() & np.isfinite(vr) & last_liquid, "成交量暴增",
                    grade(vr, cfg["vol_ratio"]), vr, "累計量 / 同時點均量"))

    # 換手率異常：只在盤後計算。分子改用日線成交量——1 分 K 加總相對日線量灌水約 2 倍，
    # 盤中又只有部分時段的量，換算出的換手率沒有可比性（見規劃書 5.3、5.4）。
    if daily_volume is not None and "shares" in last and last["shares"].notna().any():
        day_vol = last["code"].map(daily_volume)
        tr = day_vol / last["shares"] * 100
        out.append(emit(last, tr.notna(), "換手率異常", grade(tr, cfg["turnover"]), tr, "日線成交量 / 在港股數"))

    # 開盤價異常：first 是「資料中的第一根 K 線」，對下午才首次成交的冷門股並非開盤價。
    # 實測 10/27 的 230 筆中，真正在 09:30 開盤者僅 13%，下午才首次成交者達 42 筆
    # （全日成交額中位數僅 1,310 元）。故限定第一根 K 線須落在開盤後數分鐘內。
    f = first.reset_index()
    open_win = tod(f["time"]).le(AM[0] + pd.Timedelta(minutes=cfg.get("open_window_min", 5)))
    f_liquid = f["turnover_hkd"] >= cfg["min_turnover_hkd"]
    f_ok = open_win & f_liquid & (~f["halted"].fillna(False).astype(bool) if "halted" in f else True)
    gap = (first["open"] / first["prev_close"] - 1) * 100
    out.append(emit(f, (gap.notna().values & f_ok.values), "開盤價異常",
                    grade(gap.values, cfg["open_gap"]), gap.values, "開盤 vs 前收"))

    # ---- 5. 收市競價異常（只在盤後提供 daily_close 時計算）----
    # 三閥門設計，見規劃書六。重點：分子分母一律取 1 分 K，不用「日線量 − 1 分 K 累計量」，
    # 該減法混用兩個不相容來源（比值 0.28~6.49 倍），實測 38~64% 的股票會算出負的 CAS 量。
    if daily_close is not None:
        t_all = tod(b["time"])
        day_b, cas_b = b[t_all < PM[1]], b[t_all >= PM[1]]
        cts = day_b.groupby("code").tail(1).copy() if not day_b.empty else day_b
        if not cts.empty:
            cas_pct = (cts["code"].map(daily_close) / cts["close"] - 1) * 100
            dev = cas_pct.abs().values

            # 閥門二：CAS 實質金額與占全日量比例（同源，不會出現負數）
            amt = (cas_b["close"] * cas_b["volume"]).groupby(cas_b["code"]).sum()
            cas_amt = cts["code"].map(amt).fillna(0).values
            cas_ratio = (cts["code"].map(cas_b.groupby("code")["volume"].sum())
                         / cts["code"].map(b.groupby("code")["volume"].sum()) * 100).fillna(0).values

            # 閥門三：依 20 日均成交額分流。一般股只看偏離度，冷門股的橙／紅另需金額或占比條件
            split = cfg["cas_pool_split_turnover"]
            avg_to_c = cts["avg_turnover"] if "avg_turnover" in cts else pd.Series(np.nan, index=cts.index)
            main = (avg_to_c >= split).fillna(False).values
            th_m, th_t = cfg["cas_dev_main"], cfg["cas_dev_thin"]
            lv_m = np.select([dev >= th_m["紅"], dev >= th_m["橙"], dev >= th_m["黃"]], LEVELS, default=None)
            lv_t = np.select(
                [(dev >= th_t["紅"]) & ((cas_amt >= th_t["紅_金額"]) | (cas_ratio >= th_t["紅_占比"])),
                 (dev >= th_t["橙"]) & (cas_amt >= th_t["橙_金額"]),
                 dev >= th_t["黃"]], LEVELS, default=None)
            lv = np.where(main, lv_m, lv_t)

            # 閥門一：持續交易最後一筆距 16:00 超過門檻 → 低頻交易的自然跳空，降一級（黃→不通報）
            stale = ((PM[1] - tod(cts["time"])).dt.total_seconds().values / 60
                     > cfg["cas_max_stale_min"])
            lv = np.where(stale, pd.Series(lv).map({"紅": "橙", "橙": "黃"}).values, lv)

            # CAS 委託價受限於參考價 ±5%，偏離超過此值在結構上不可能，判為資料異常不給等級
            lv = np.where(dev > cfg["cas_max_dev"], None, lv)

            cts = cts.copy()
            cts["time"] = cts["time"].dt.normalize() + pd.Timedelta("16h10m")
            out.append(emit(cts, cas_pct.notna().values, "收市競價異常", lv, cas_pct,
                            "收市價 vs 16:00 前最後價"))

    alerts = pd.concat([o for o in out if not o.empty], ignore_index=True) if any(
        not o.empty for o in out) else pd.DataFrame(columns=COLS)
    alerts = alerts.merge(universe[["code", "name"]], on="code", how="left")

    # ---- 每批次每檔取最嚴重一筆（僅分鐘級量能規則）----
    per_batch = alerts["rule"].isin(BATCH_MAX_RULES)
    if per_batch.any():
        sel = alerts[per_batch]
        keep = (sel.assign(_o=sel["level"].map(LEVEL_RANK), _v=sel["value"].abs())
                   .sort_values(["_o", "_v"], ascending=[True, False])
                   .drop_duplicates(["code", "rule"]).index)
        alerts = alerts[~per_batch | alerts.index.isin(keep)].reset_index(drop=True)

    # ---- 標記：只加註、不改變判定，供人工判讀 ----
    tags = []
    # 除息：前收未還原權息，除息日股價自然下跌，價格類指標可能誤判
    if ex_div_codes:
        tags.append(("除息日",
                     alerts["code"].isin(set(ex_div_codes)) & alerts["rule"].isin(PRICE_RULES)))
    # 復牌首日：停牌期間 Yahoo 的成交量為 0，會把 20 日均量壓低，復牌當天的正常量除以被壓低的
    # 基準，倍數自然失真（10/26 實測中位數 8 倍、最高 347 倍）。但復牌放量本身值得注意，
    # 所以量能類只標記不排除；價格類的假跳空無判讀價值，已在各規則以 halted 排除。
    if "halted" in stats:
        halted_codes = set(stats.loc[stats["halted"].fillna(False).astype(bool), "code"])
        if halted_codes:
            tags.append(("復牌首日（倍數失真）",
                         alerts["code"].isin(halted_codes) & alerts["rule"].isin(VOLUME_RULES)))
    # 股本來源：取不到在港股數而退回 A+H 合計者，換手率的分母偏大、數值偏低
    if "shares_src" in stats:
        impl = set(stats.loc[stats["shares_src"] == "implied", "code"])
        if impl:
            tags.append(("股本為A+H合計（換手率偏低）",
                         alerts["code"].isin(impl) & (alerts["rule"] == "換手率異常")))
    alerts["note"] = ""
    for label, m in tags:
        sep = np.where(alerts.loc[m, "note"] == "", "", "；")
        alerts.loc[m, "note"] = alerts.loc[m, "note"] + sep + label
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
