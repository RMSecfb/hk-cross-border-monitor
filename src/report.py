"""盤後報告：預警列表（Excel「當前活躍預警列表」格式）與每日風險分數 / 連續上榜追蹤"""
import pandas as pd

from common import DATA, load_yaml, path

LEVEL_ORDER = {"紅": 0, "橙": 1, "黃": 2}

# 規則 → (rules.yaml 閾值鍵, 單位, 指標值格式)
RULE_FMT = {
    "即時漲跌幅": ("day_change", "%", "{v:+.2f}% vs 前收"),
    "1分鐘急漲急跌": ("chg_1m", "%", "{v:+.2f}%/1分"),
    "5分鐘急漲急跌": ("chg_5m", "%", "{v:+.2f}%/5分"),
    "偏離均線": ("ma_dev", "%", "{v:+.2f}% vs MA{ma}"),
    "開盤價異常": ("open_gap", "%", "{v:+.2f}% vs 前收"),
    "收市競價異常": ("cas_dev", "%", "{v:+.2f}% vs 16:00 價"),
    "成交量暴增": ("vol_ratio", "倍", "{v:.1f} 倍均量"),
    "3分鐘量暴增": ("vol_3m", "倍", "{v:.1f} 倍（3 分鐘）"),
    "連續放量": ("vol_streak", "倍", "每分鐘 ≥{v:.1f} 倍 × {n} 分"),
    "換手率異常": ("turnover", "%", "{v:.1f}%"),
    "VCM觸發(模擬)": (None, "%", "{v:+.2f}%/5分"),
    "VCM內部預警": (None, "%", "{v:+.2f}%/5分"),
}


def _threshold(rule, level, cfg, vcm_pct):
    key, unit, _ = RULE_FMT.get(rule, (None, "", ""))
    if rule == "VCM觸發(模擬)":
        return f"紅 ±{vcm_pct:.0f}%" if pd.notna(vcm_pct) else ""
    if rule == "VCM內部預警":
        return f"黃 ±{vcm_pct * cfg['vcm_early_ratio']:.0f}%" if pd.notna(vcm_pct) else ""
    th = cfg.get(key, {}).get(level)
    if th is None:
        return ""
    return f"{level} ±{th}%" if unit == "%" and rule not in ("換手率異常",) else f"{level} {th}{unit}"


def _value(rule, v, cfg):
    fmt = RULE_FMT.get(rule, (None, "", "{v}"))[2]
    try:
        return fmt.format(v=v, ma=cfg["ma_days"], n=cfg["vol_streak_minutes"])
    except (ValueError, TypeError):
        return str(v)


def _actions():
    acts = {}
    for grp in load_yaml("actions.yaml").values():
        for r in grp["rules"]:
            acts[r] = {lv: grp.get(lv, "") for lv in ("黃", "橙", "紅")}
    return acts


def risk_score(alerts, cfg):
    """每檔每日風險分數 = 各規則當日最高等級分數加總（上限 100）"""
    if alerts.empty:
        return pd.DataFrame(columns=["code", "name", "score", "紅", "橙", "黃", "rules"])
    w = cfg["score_weights"]
    top = (alerts.assign(o=alerts["level"].map(LEVEL_ORDER)).sort_values("o")
           .drop_duplicates(["code", "rule"]))                       # 每檔每規則取最高等級
    top["pts"] = top["level"].map(w)
    g = top.groupby("code")
    sc = pd.DataFrame({
        "name": g["name"].first(),
        "score": g["pts"].sum().clip(upper=100),
        "紅": g["level"].apply(lambda s: (s == "紅").sum()),
        "橙": g["level"].apply(lambda s: (s == "橙").sum()),
        "黃": g["level"].apply(lambda s: (s == "黃").sum()),
        "rules": g["rule"].apply(lambda s: "、".join(s)),
    }).reset_index()
    return sc.sort_values("score", ascending=False)


def score_history(before, n=10):
    """讀取 before（不含）之前最近 n 個交易日的風險分數 → 寬表 code × 日期"""
    files = sorted((DATA / "report").glob("score_*.csv"))
    files = [f for f in files if f.stem[-8:] < f"{before:%Y%m%d}"][-n:]
    if not files:
        return pd.DataFrame()
    parts = [pd.read_csv(f, usecols=["code", "score"]).assign(date=f.stem[-8:]) for f in files]
    return pd.concat(parts).pivot_table(index="code", columns="date", values="score", aggfunc="first")


def listing_stats(score_today, day, cfg, lookback=5):
    """每檔：連續上榜天數（含今日）、近 lookback 日上榜天數"""
    th = cfg["list_min_score"]
    hist = score_history(day, n=max(lookback, 20))
    today = score_today.set_index("code")["score"].rename(f"{day:%Y%m%d}")
    wide = pd.concat([hist, today], axis=1) if not hist.empty else today.to_frame()
    on = wide.fillna(0) >= th
    cols = list(on.columns)

    def streak(row):
        n = 0
        for c in reversed(cols):
            if row[c]:
                n += 1
            else:
                break
        return n
    return pd.DataFrame({"streak": on.apply(streak, axis=1),
                         "recent": on[cols[-lookback:]].sum(axis=1)}).reset_index().rename(columns={"index": "code"})


def build_report(alerts, uni, cfg, day, score):
    """預警列表：每檔 × 每規則一列，取當日最高等級（同等級取最早觸發時間）"""
    if alerts.empty:
        return pd.DataFrame()
    acts = _actions()
    a = alerts.merge(uni[["code", "vcm_pct"]], on="code", how="left")
    a["o"] = a["level"].map(LEVEL_ORDER)
    a["t"] = pd.to_datetime(a["time"])
    cnt = a.groupby(["code", "rule"]).size().rename("觸發次數")
    top = a.sort_values(["o", "t"]).drop_duplicates(["code", "rule"]).join(cnt, on=["code", "rule"])
    top = top.sort_values(["t", "o"]).reset_index(drop=True)

    if "streak" not in score:
        score = score.merge(listing_stats(score, day, cfg), on="code", how="left")
    ls = score.set_index("code")
    sc = ls["score"]
    rep = pd.DataFrame({
        "預警ID": [f"ALT-{day:%Y%m%d}-{i + 1:04d}" for i in range(len(top))],
        "時間": top["t"].dt.strftime("%H:%M:%S"),
        "等級": top["level"],
        "股票代碼": top["code"].map(lambda c: f"{int(c):05d}"),
        "股票名稱": top["name"],
        "監控類型": top["rule"],
        "指標值": [_value(r, v, cfg) for r, v in zip(top["rule"], top["value"])],
        "閾值": [_threshold(r, lv, cfg, p) for r, lv, p in zip(top["rule"], top["level"], top["vcm_pct"])],
        "當日觸發次數": top["觸發次數"].values,
        "風險分數": top["code"].map(sc).values,
        "連續上榜(日)": top["code"].map(ls["streak"]).fillna(0).astype(int).values,
        "近5日上榜(日)": top["code"].map(ls["recent"]).fillna(0).astype(int).values,
        "操作建議": [acts.get(r, {}).get(lv, "") for r, lv in zip(top["rule"], top["level"])],
    })
    return rep


def write_reports(alerts, uni, cfg, day):
    """產生並存檔：score_YYYYMMDD.csv、eod_report_YYYYMMDD.csv（預警列表）"""
    score = risk_score(alerts, cfg)
    ls = listing_stats(score, day, cfg)
    score = score.merge(ls, on="code", how="left")
    score.to_csv(path("report", f"score_{day:%Y%m%d}.csv"), index=False, encoding="utf-8-sig")
    rep = build_report(alerts, uni, cfg, day, score)
    rep.to_csv(path("report", f"eod_report_{day:%Y%m%d}.csv"), index=False, encoding="utf-8-sig")
    return rep, score
