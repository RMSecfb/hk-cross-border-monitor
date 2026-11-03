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
VOLUME_RULES = ["成交量暴增", "3分鐘量暴增", "連續放量", "換手率異常", "冬眠股異動"]
# 停牌標記的適用範圍與回看窗格。
# 2026-11-03 更正：此標記原先宣稱「倍數失真」，但該前提不成立——封存只存 volume > 0 的列，
# bars_store._day_count 明寫「整日無量者不計入」，所以 avg1／avg3／avgcum 的分子與分母
# 都已排除沒成交的日子，停牌並不會壓低這三個指標的分母。實測也無劑量反應（無成交天數
# 最多的那一組倍數反而最低）。保留標記是因為「這檔剛從一段停牌回來」對判讀仍是有用的
# 背景，但措辭改為陳述事實，不再宣稱數字失真。窗格仍對齊各指標自己的平均天數。
HALT_TAG_RULES = {
    "short": ["3分鐘量暴增", "連續放量"],      # 分母取近 5 個交易日
    "long": ["成交量暴增"],                   # 分母取近 20 個交易日
}
# 近期分割的標記分成兩組，因為失真的來源不同（見規劃書九之六）。
#
# 第一組 SPLIT_TAG_RULES：Yahoo 的歷史價格已還原過分割（實測 11 檔中 10 檔），日線價量
# 因此是對的，但另外兩個分母沒有跟著還原：
#   ① 分母來自 1 分 K 封存的三條——封存存的是當時的未還原成交量，分割後的量與分割前不同
#      級距，同時點分母因而橫跨斷點；
#   ② 換手率異常——分母 shares.csv 是靜態股本，分割後尚未更新前，換手率會整個偏掉。
SPLIT_TAG_RULES = ["3分鐘量暴增", "連續放量", "成交量暴增", "換手率異常"]
# 第二組 SPLIT_UNADJ_RULES：少數分割 Yahoo 沒有回頭還原（1351 於 11/02 即是），序列留著
# 一個倍數斷點，於是連跨多日的價格／成交額分母本身就是錯的：
#   偏離均線——20 日 ma 橫跨斷點；冬眠股異動——avg_turnover 橫跨斷點。
# 即時漲跌幅、開盤價異常不在內：它們只比 prev_close，斷點隔天起就是乾淨的成交價。
# 只標記「經查證確實沒還原」的那幾檔（eod.recent_splits 以實際價格比對帳記錄比例），
# 不是把所有分割股一律標上——否則 11 檔裡有 10 檔會被誤標，標記就沒有意義了。
SPLIT_UNADJ_RULES = ["偏離均線", "冬眠股異動"]
# 兩條指標不在內，理由各不相同：
#   換手率異常——分子是日線成交量、分母是股本，完全不經過歷史均值，停牌不會造成失真。
#   冬眠股異動——冬眠股的零成交日，資料特徵與停牌完全相同（volume=0、OHLC 全同），
#     無從區分；而對這條指標而言，低均額不是「失真」，正是它要衡量的訊號本身。
#     2026-10-30 上線首日 7 筆中有 6 筆被誤標，標記等於失去意義（見規劃書九之三）。
# 兩者先前都被標上「倍數失真」，都是錯的。
# 分鐘級規則：同一批次內每檔只留最嚴重的一筆（原本每分鐘都判斷，一波拉抬會報出數十筆）
BATCH_MAX_RULES = ["3分鐘量暴增", "連續放量"]
LEVEL_RANK = {lv: i for i, lv in enumerate(LEVELS)}


def grade(values, th):
    """依黃/橙/紅閾值（取絕對值）回傳等級，未達回傳 None"""
    v = np.abs(np.asarray(values, dtype=float))
    return np.select([v >= th["紅"], v >= th["橙"], v >= th["黃"]], LEVELS, default=None)


def dual_grade(values, avg_turnover, cur_turnover, th, split, allow_b=True):
    """雙軌分級（成交量暴增、3 分鐘量暴增）

    A 軌＝熱門股（20 日均成交額 ≥ split）、B 軌＝冬眠股（< split）。
    兩軌的倍數門檻目前相同，差別只在「當下累計成交額」的絕對下限：冬眠股平時幾乎沒有成交，
    用倍數衡量會被極小的分母放大，故要求更高的實際成交金額才列入判定。
    th 若仍是舊的單軌格式（直接是 黃/橙/紅），則退回單軌。
    """
    if "A" not in th:
        return grade(values, th)
    cur = np.asarray(cur_turnover, dtype=float)
    lv = {}
    for k in ("A", "B"):
        t = th[k]
        lv[k] = np.where(cur >= t.get("min_turnover", 0), grade(values, t), None)
    is_a = np.asarray(pd.Series(avg_turnover).fillna(-1).values) >= split
    # 冬眠股（B 軌）只在該檔的「同時點分母」可用時才開啟：舊的時段內插分母對幾乎不交易的股票
    # 會把極小的基準放大成數百倍，先用舊公式開 B 軌等於自找雜訊（見規劃書 一、過渡期做法）。
    allow = np.broadcast_to(np.asarray(allow_b, dtype=bool), is_a.shape)
    return np.where(is_a, lv["A"], np.where(allow, lv["B"], None))


def expected_cum(baseline, codes, m_cut):
    """過去 20 日 09:30 至 m_cut 的平均累計量。

    基準檔只在「有成交的分鐘」留下一列，中間沒成交的分鐘累計量不變，
    故以 merge_asof 向前對齊，取 ≤ m_cut 的最後一筆。
    """
    bl = baseline.loc[baseline["avgcum"].notna(), ["code", "m", "avgcum"]].copy()
    if bl.empty:
        return np.full(len(codes), np.nan)
    bl = bl.astype({"m": "int64", "code": "int64"}).sort_values(["m", "code"])
    tgt = pd.DataFrame({"pos": np.arange(len(codes)),
                        "code": np.asarray(codes).astype("int64"),
                        "m": np.int64(m_cut)}).sort_values(["m", "code"])
    j = pd.merge_asof(tgt, bl, on="m", by="code", direction="backward")
    return j.sort_values("pos")["avgcum"].values


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


def emit(df, mask, rule, levels, value, detail):
    """把符合條件的列轉成警報表"""
    lv = np.asarray(levels, dtype=object)
    ok = np.asarray(mask, dtype=bool) & pd.notna(lv)
    if not ok.any():
        return pd.DataFrame(columns=COLS)
    det = np.asarray(detail)[ok] if np.ndim(detail) else detail   # 逐列說明文字
    return pd.DataFrame({"time": df["time"].values[ok], "code": df["code"].values[ok], "rule": rule,
                         "level": lv[ok], "value": np.round(np.asarray(value, dtype=float)[ok], 2),
                         "detail": det})


def run_rules(bars, stats, universe, cfg, daily_close=None, ex_div_codes=None,
              daily_volume=None, baseline=None):
    if bars.empty:
        return pd.DataFrame(columns=COLS + ["name"])
    b = bars.sort_values(["code", "time"]).reset_index(drop=True)
    b = b.merge(stats, on="code", how="left").merge(universe[["code", "vcm_pct"]], on="code", how="left")
    g = b.groupby("code", sort=False)
    out = []

    # ---- 同時點基準（規劃書 二、三、四）----
    # baseline 由盤後以過去 5／20 個交易日的 1 分 K 算好：
    #   avg1 該分鐘平均量、avg3 結束於該分鐘的 3 分鐘平均量、avgcum 09:30 起的平均累計量。
    # 比「同一個時點」才能扣掉該時點本來就會放量的特性（整點前、收盤前）。
    #
    # 沒有基準、或某檔自己的歷史不足時，該檔的量能指標**不計算**，不退回舊的 30 分鐘時段內插法。
    # 舊分母正是 3 分鐘量暴增 98% 觸發率的來源（規劃書 二、三），用它湊數只會讓報告混進
    # 沒人能判讀的警報；寧可暫時不報，也不要報不可信的數字。
    b["bucket"] = bucket(b["time"])
    use_tp = baseline is not None and not baseline.empty
    if use_tp:
        b["mday"] = (b["time"].dt.hour * 60 + b["time"].dt.minute).astype("int32")
        bl = baseline.rename(columns={"m": "mday"})
        bl["mday"] = bl["mday"].astype("int32")
        cols = ["code", "mday", "avg1", "avg3"] + [c for c in ("n_short",) if c in bl]
        b = b.merge(bl[cols], on=["code", "mday"], how="left")
        if "n_short" not in b:
            b["n_short"] = 0
    else:
        b["avg1"] = b["avg3"] = np.nan
        b["n_short"] = 0
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
    # 量能類不排除復牌股，改在 note 標記（見檔案開頭 HALT_TAG_RULES）

    # ---- 1. 1 分鐘急漲急跌（對上一筆成交分鐘）----
    # 「上一根」未必是一分鐘前：冷門股可能隔數十分鐘才有下一筆成交，該漲跌幅實際涵蓋很長一段時間。
    prev_t = g["time"].shift(1)
    gap1_ok = (b["time"] - prev_t).dt.total_seconds().le(cfg["max_prev_gap_min"] * 60)
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
        cfg["max_ref_gap_5m_min"] * 60).fillna(False).values
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
    gb3 = bt.groupby("code", sort=False)["volume"]
    vol3 = gb3.rolling("3min").sum().values
    cnt3 = gb3.rolling("3min").count().values          # 這 3 分鐘實際有幾根 K 線
    cnt15 = gb3.rolling("15min").count().values        # 近 15 分鐘有成交的分鐘數
    split = cfg["pool_split_turnover"]
    # 逐檔閘門：該檔自己累積的天數不足（新上市、長期停牌、或不在 1 分 K 收錄範圍）就不計算
    min_s = cfg["min_hist_days"]
    tp3 = (b["avg3"].notna() & (b["n_short"] >= min_s)) if use_tp else pd.Series(False, index=b.index)

    v3 = pd.Series(vol3, index=b.index)
    r_hist = v3 / b["avg3"].replace(0, np.nan) if use_tp else pd.Series(np.nan, index=b.index)
    # 今日開盤以來每 3 分鐘的均量（盤中自然只算到當下）：用來判斷「相對今天自己」有多突出
    # shift(3)：排除當下這個窗格與它重疊的兩根，否則爆量會把自己的比較基準一起拉高
    # （開盤後不久的爆量尤其明顯：樣本少，一波大量就佔掉均值的一半）。
    # min_periods=3 表示開盤約 5 分鐘後才開始判定；再嚴就會讓開盤後十分鐘變成盲區。
    cum3 = v3.groupby(b["code"]).transform(lambda x: x.shift(3).expanding(min_periods=3).mean())
    r_today = v3 / cum3
    # 這 3 分鐘「每一分鐘」都要放量：取窗格內「該分鐘量 ÷ 該分鐘歷史均量」的最小值。
    # 正常股票的爆量多半是一根尖峰夾在兩根平靜分鐘之間，這條過不了；要持續下單才過得了。
    r1m = b["volume"] / b["avg1"].replace(0, np.nan) if use_tp else pd.Series(np.nan, index=b.index)
    permin = b.assign(_r=r1m).set_index("time").groupby("code", sort=False)["_r"].rolling("3min").min().values

    # 先逐分鐘驗門檻，再從通過的分鐘裡挑最嚴重的一筆。
    # 舊版相反：先挑「成交量最大」那一分鐘再驗門檻，等於整天只抽驗一根窗格，
    # 10/28 實測漏掉 82% 本來就符合條件的標的（真正的異常常不在量最大的那一分鐘）。
    gate = cfg["vol_3m_gate"]
    lv3 = dual_grade(r_today, avg_to, b["turnover_hkd"], cfg["vol_3m"], split)
    # 流動性閘門：近 15 分鐘每分鐘都有成交。但開盤與午休後的前 15 分鐘往前看會跨過休市，
    # 不可能湊滿 15 根，若直接套用會讓 09:30-09:45 與 13:00-13:15 變成盲區——
    # 而那正是最活躍的時段（10/28 的 03800 真實異常就發生在 13:06）。改為以該時段已開盤的分鐘數為上限。
    tmin = tod(b["time"]).dt.total_seconds() / 60
    elapsed = np.where(tmin < 720, tmin - 570 + 1, tmin - 780 + 1)
    need15 = np.minimum(gate["traded_min"], np.maximum(elapsed, 1))
    ok3 = (tp3 & (cnt3 >= 3) & (cnt15 >= need15)
           & (r_hist >= gate["hist_min"]) & (r_today >= gate["today_min"])
           & (pd.Series(permin, index=b.index) >= gate["permin_min"])
           & pd.notna(pd.Series(lv3, index=b.index))).fillna(False)
    best3 = pd.Series(False, index=b.index)
    if ok3.any():
        cand = pd.DataFrame({"code": b.loc[ok3, "code"],
                             "o": pd.Series(lv3, index=b.index)[ok3].map(LEVEL_RANK),
                             "v": r_today[ok3]})
        best3.loc[cand.sort_values(["o", "v"], ascending=[True, False])
                      .drop_duplicates("code").index] = True
    det3 = ("最近 3 分鐘量 = 今日均量的 " + r_today.round(1).astype(str)
            + " 倍、過去 5 日同時點的 " + r_hist.round(0).astype(str) + " 倍").values
    out.append(emit(b, best3, "3分鐘量暴增", lv3, r_today, det3))

    n = cfg["vol_streak_minutes"]
    den1 = b["avg1"].replace(0, np.nan) if use_tp else pd.Series(np.nan, index=b.index)
    r1 = (b["volume"] / den1).fillna(0)
    bt = b.assign(r1=r1).set_index("time").groupby("code", sort=False)["r1"]
    streak_min = bt.rolling(f"{n}min").min().values
    streak_cnt = bt.rolling(f"{n}min").count().values
    streak_lv = np.where(streak_cnt >= n, grade(streak_min, cfg["vol_streak"]), None)
    out.append(emit(b, active & tp3, "連續放量", streak_lv, streak_min,
                    f"連續 {n} 分鐘每分鐘量 / 過去 5 日同時點平均"))

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

    if use_tp:
        exp_cum = expected_cum(baseline, last["code"], b["mday"].max())
        n_long = last["code"].map(baseline.drop_duplicates("code").set_index("code")["n_long"]) \
            if "n_long" in baseline else pd.Series(0, index=last.index)
        ok_cum = pd.notna(exp_cum) & (exp_cum > 0) & (n_long.fillna(0).values >= cfg["min_hist_days_cum"])
    else:
        exp_cum = np.full(len(last), np.nan)
        ok_cum = np.zeros(len(last), dtype=bool)
    vr = last["cum_vol"] / exp_cum
    last_avg_to = last["avg_turnover"] if "avg_turnover" in last else pd.Series(np.nan, index=last.index)
    lvr = dual_grade(vr, last_avg_to, last["turnover_hkd"], cfg["vol_ratio"], cfg["pool_split_turnover"])
    fin = vr.notna().values & np.isfinite(vr).values
    out.append(emit(last, fin & ok_cum, "成交量暴增", lvr, vr,
                    "今日累計量 / 過去 20 日同時點平均累計量"))

    # 換手率異常：只在盤後計算。分子改用日線成交量——1 分 K 加總相對日線量灌水約 2 倍，
    # 盤中又只有部分時段的量，換算出的換手率沒有可比性（見規劃書 5.3、5.4）。
    if daily_volume is not None and "shares" in last and last["shares"].notna().any():
        day_vol = last["code"].map(daily_volume)
        tr = day_vol / last["shares"] * 100
        out.append(emit(last, tr.notna(), "換手率異常", grade(tr, cfg["turnover"]), tr, "日線成交量 / 在港股數"))

    # 冬眠股異動：只在盤後計算，且刻意不經過 1 分 K——連 last（由 bars 衍生）都不用，
    # 因為最冷門的股票當天可能一根 1 分 K 都沒有，用 bars 當底會把它們直接漏掉。
    # 封存收錄門檻取消後（2026-10-29），冬眠股仍要累積 5／20 個交易日才拿得到同時點分母，
    # 「剛從冬眠轉活躍」的那 1～4 週正是最可疑的時間窗，量能三指標必定靜音；日線對全名單
    # 永遠是滿的，用它補這段空窗。範圍限定在冬眠線以下，與成交量暴增不重疊。
    dm = cfg.get("dormant_move")
    if dm and daily_volume is not None and daily_close is not None and "avg_turnover" in stats:
        dd = universe[["code"]].drop_duplicates().merge(
            stats[["code", "avg_turnover"]].drop_duplicates("code"), on="code", how="left")
        dd["time"] = b["time"].max()
        cur_to = dd["code"].map(daily_volume) * dd["code"].map(daily_close)
        base_to_d = dd["avg_turnover"]
        # 近乎零成交者的均額會把倍數放大到無意義（實測 2362 的 20 日均額為 0），故設下限。
        # 下限只影響倍數大小與分級，不影響是否入選；detail 會標明已套用。
        floored = base_to_d.fillna(0) < dm["base_floor"]
        ratio_d = cur_to / base_to_d.clip(lower=dm["base_floor"])
        ok_dm = ((base_to_d < dm["max_avg_turnover"]) & (cur_to >= dm["min_turnover"])
                 & ratio_d.notna()).fillna(False)
        det_dm = ("當日成交額 " + (cur_to / 1e4).round(1).astype(str) + " 萬 / 20 日均額 "
                  + (base_to_d / 1e4).round(2).astype(str) + " 萬"
                  + np.where(floored, "（均額低於下限，以 1 萬計）", "")).values
        out.append(emit(dd, ok_dm, "冬眠股異動", grade(ratio_d, dm), ratio_d, det_dm))

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
    # 停牌：量能類只標記不排除，作為判讀背景（不是數值失真警告，理由見檔案開頭 HALT_TAG_RULES）。
    # 價格類的假跳空無判讀價值，已在各規則以 halted 排除——價格類只看「最近一個交易日有無成交」
    # 是對的：隔天起 prev_close 已是真實成交價，不再有假跳空。
    halt_cols = [("short", "halt_run_5", 5), ("long", "halt_run_20", 20)]
    if any(c in stats for _, c, _ in halt_cols):
        for span, col, n in halt_cols:
            if col not in stats:
                continue
            codes = set(stats.loc[stats[col].fillna(False).astype(bool), "code"])
            if codes:
                tags.append((f"近 {n} 個交易日內曾停牌",
                             alerts["code"].isin(codes) & alerts["rule"].isin(HALT_TAG_RULES[span])))
    elif "halted" in stats:      # 舊格式基準檔沒有新欄位，退回原本的「復牌首日」
        halted_codes = set(stats.loc[stats["halted"].fillna(False).astype(bool), "code"])
        if halted_codes:
            tags.append(("復牌首日",
                         alerts["code"].isin(halted_codes) & alerts["rule"].isin(VOLUME_RULES)))
    # 近期分割：價量已還原，但 1 分 K 封存與 shares.csv 仍是分割前的級距（見 SPLIT_TAG_RULES）
    if "split_recent" in stats:
        sp = set(stats.loc[stats["split_recent"].fillna(False).astype(bool), "code"])
        if sp:
            tags.append(("近期曾股份分割（分母未還原）",
                         alerts["code"].isin(sp) & alerts["rule"].isin(SPLIT_TAG_RULES)))
    # 分割未還原：連 ma / avg_turnover 都橫跨斷點，價格類也失真（見 SPLIT_UNADJ_RULES）
    if "split_unadjusted" in stats:
        su = set(stats.loc[stats["split_unadjusted"].fillna(False).astype(bool), "code"])
        if su:
            tags.append(("近期分割且歷史價未還原（均線／均額橫跨斷點）",
                         alerts["code"].isin(su) & alerts["rule"].isin(SPLIT_UNADJ_RULES)))
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
