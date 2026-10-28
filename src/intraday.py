"""盤中批次（GitHub Actions 每 30 分鐘執行）
1. 抓全部港股當日 1 分鐘 K 線（Yahoo，延遲約 15 分鐘）
2. 存檔（檔名含日期時分秒）
     data/snapshot/YYYY-MM-DD/snapshot_YYYYMMDD_HHMMSS.parquet  每檔一列的最新狀態
     data/alerts/alerts_new_YYYYMMDD_HHMMSS.csv                本次新增警報
     data/alerts/alerts_YYYYMMDD.csv                           當日累計警報
     data/status.json                                          最近一次執行摘要
用法：python src/intraday.py [--force]   （--force：非交易時段也執行，改用最近一個交易日的行情，測試用）
"""
import json
import sys

import numpy as np
import pandas as pd

import bars_store
from common import (SETTINGS, download, get_logger, latest_file, load_universe, load_yaml, now_hkt, path)
from rules import apply_cooldown, expected_cum, run_rules

log = get_logger("intraday")
ONCE = ["即時漲跌幅", "偏離均線", "成交量暴增", "換手率異常", "開盤價異常"]   # 全日型：同等級一天報一次


def load_stats(today, cfg):
    f = latest_file("stats", "stats_*.csv", before=f"{today:%Y%m%d}")
    if f is None:
        # 例如測試模式重跑「已有當日基準檔」的交易日：改用日線檔重建前一交易日基準
        from eod import load_daily, make_stats
        prev = load_daily(today - pd.Timedelta(days=1), 1)
        if prev.empty:
            raise SystemExit("找不到 stats 基準檔，請先執行盤後初始化：python src/eod.py --init")
        log.info(f"以日線重建 {prev['date'].max():%Y-%m-%d} 基準檔")
        return make_stats(cfg, prev["date"].max())
    log.info(f"使用基準檔 {f.name}")
    return pd.read_csv(f)


def alert_key(df):
    if df.empty:
        return pd.Series([], dtype=object)
    t = df["time"].astype(str).where(~df["rule"].isin(ONCE), "")
    parts = [df["code"], df["rule"], df["level"], t]
    return pd.Series(["|".join(map(str, row)) for row in zip(*parts)], index=df.index)


def make_snapshot(bars, uni, stats, baseline=None):
    b = bars.sort_values(["code", "time"])
    g = b.groupby("code")
    snap = g.agg(last_time=("time", "last"), open=("open", "first"), high=("high", "max"),
                 low=("low", "min"), price=("close", "last"), cum_volume=("volume", "sum")).reset_index()
    snap["turnover_hkd"] = (b["close"] * b["volume"]).groupby(b["code"]).sum().reindex(snap["code"]).values
    # 最近 5 分鐘漲跌（VCM 參考價邏輯：5 分鐘前最後成交價）
    ref_t = snap.set_index("code")["last_time"] - pd.Timedelta("5min")
    ref = b.merge(ref_t.rename("ref_t"), left_on="code", right_index=True)
    ref = ref[ref["time"] <= ref["ref_t"]].groupby("code")["close"].last()
    snap = snap.merge(uni[["code", "name", "board", "vcm_pct"]], on="code", how="left").merge(stats, on="code", how="left")
    snap["chg_pct"] = (snap["price"] / snap["prev_close"] - 1) * 100
    snap["chg_5m_pct"] = (snap["price"] / snap["code"].map(ref) - 1) * 100
    snap["vcm_usage_pct"] = (snap["chg_5m_pct"].abs() / snap["vcm_pct"] * 100).where(snap["vcm_pct"].notna())
    snap["ma_dev_pct"] = (snap["price"] / snap["ma"] - 1) * 100
    # 量比的分母與「成交量暴增」用同一套同時點基準，報表與儀表板才不會各算各的；
    # 沒有基準（歷史還沒累積夠）時留空，不用舊的時段內插法湊一個數字出來。
    if baseline is not None and not baseline.empty:
        m_cut = int((b["time"].dt.hour * 60 + b["time"].dt.minute).max())
        snap["vol_ratio"] = snap["cum_volume"] / expected_cum(baseline, snap["code"], m_cut)
    else:
        snap["vol_ratio"] = np.nan
    snap = snap.replace([np.inf, -np.inf], np.nan)
    return snap.round({"open": 3, "high": 3, "low": 3, "price": 3, "ma": 3, "avg_vol": 0, "avg_turnover": 0, "turnover_hkd": 0,
                       "chg_pct": 2, "chg_5m_pct": 2, "vcm_usage_pct": 1, "ma_dev_pct": 2, "vol_ratio": 2})


def main(force=False):
    now = now_hkt()
    today, stamp = now.normalize().tz_localize(None), f"{now:%Y%m%d_%H%M%S}"
    hm = now.strftime("%H%M")
    if not force and (now.weekday() >= 5 or not ("0930" <= hm <= "1700")):
        log.info("非交易時段，結束")
        return

    probe = download([SETTINGS["market_probe"]], "1d", "1m", log)
    if probe.empty:
        log.info("抓不到指標股行情，結束")
        return
    if probe["time"].max().normalize() != today:
        if not force:
            log.info("今日無行情（假期或尚未開市），結束")
            return
        today = probe["time"].max().normalize()
        log.info(f"測試模式：使用 {today:%Y-%m-%d} 的行情")

    uni = load_universe()
    cfg = load_yaml("rules.yaml")
    stats = load_stats(today, cfg)

    raw = download(uni["yahoo"].tolist(), "1d", "1m", log)
    raw = raw[raw["time"].dt.normalize() == today]
    bars = raw.merge(uni[["yahoo", "code"]], left_on="ticker", right_on="yahoo").drop(columns=["ticker", "yahoo"])
    got = bars["code"].nunique()
    log.info(f"取得 {got}/{len(uni)} 檔、{len(bars)} 根 1 分鐘 K 線")
    if bars.empty:
        return

    # ---- 同時點基準：量能三指標的分母，取不到就不計算那三個指標 ----
    base = None
    try:
        base = bars_store.fetch_baseline(before=today)
    except Exception as e:
        log.warning(f"同時點基準取得失敗：{e}")
    if base is None or base.empty:
        log.warning("沒有同時點基準，本批次不計算成交量暴增／3分鐘量暴增／連續放量。"
                    "歷史累積中屬正常；若已累積多日仍出現，請檢查 Release 的 baseline_*.parquet")
        n_base_days = 0
    else:
        n_base_days = int(base["n_long"].max()) if "n_long" in base else -1

    # ---- 快照 ----
    day = f"{today:%Y-%m-%d}"
    snap = make_snapshot(bars, uni, stats, base)
    snap.to_parquet(path("snapshot", day, f"snapshot_{stamp}.parquet"), index=False)

    # ---- 監控規則 ----
    alerts = apply_cooldown(run_rules(bars, stats, uni, cfg, baseline=base), cfg["cooldown_min"])
    all_f = path("alerts", f"alerts_{today:%Y%m%d}.csv")
    old = pd.read_csv(all_f, parse_dates=["time"]) if all_f.exists() else alerts.iloc[0:0]
    new = alerts[~alert_key(alerts).isin(set(alert_key(old)))].assign(found_at=stamp)
    merged = pd.concat([old, new], ignore_index=True)
    if "note" in merged:                       # 舊版本寫的檔沒有 note 欄，補空字串避免顯示 NaN
        merged["note"] = merged["note"].fillna("")
    merged.to_csv(all_f, index=False, encoding="utf-8-sig")
    if not new.empty:
        new.to_csv(path("alerts", f"alerts_new_{stamp}.csv"), index=False, encoding="utf-8-sig")
    lv = new["level"].value_counts().to_dict()

    # 資料品質：1 分 K 加總 ÷ 同期間的正常成交量（只記錄，不影響判定）。
    # 分母必須按已交易時間等比縮放——盤中只過了部分交易時段，拿整天均量比會恆小，
    # 開盤半小時算出 0.06 是正常的，卻會被誤讀成「資料不可信」。
    try:
        from rules import minutes_elapsed
        el = float(minutes_elapsed(pd.Series([bars["time"].max()])).iloc[0])
        scale = min(max(el / 330, 0.01), 1.0)
        q = bars_store.quality(bars, stats.set_index("code")["avg_vol"] * scale) \
            if "avg_vol" in stats else {}
        if q:
            q["elapsed_min"] = round(el)
            q["note"] = (f"1分K加總 ÷（20日均量 × 已交易 {el:.0f}/330 分）；"
                         "偏離 1 很遠代表 Yahoo 分鐘資料可信度低")
    except Exception:
        q = {}

    status = {"run_at": f"{now:%Y-%m-%d %H:%M:%S}", "trade_date": day, "data_until": f"{bars['time'].max():%H:%M}",
              "stocks_ok": int(got), "stocks_total": int(len(uni)),
              "missing": sorted(map(int, set(uni["code"]) - set(bars["code"])))[:500],
              "new_alerts": {k: int(lv.get(k, 0)) for k in ["紅", "橙", "黃"]},
              "data_quality": q,
              # 量能三指標是否有分母可用；0 代表本批次完全不計算，儀表板要能看得出來
              "baseline_days": n_base_days,
              "volume_rules": "計算中" if n_base_days else "未計算（同時點基準尚未累積）",
              "snapshot_file": f"snapshot/{day}/snapshot_{stamp}.parquet"}
    path("status.json").write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info(f"新增警報 {len(new)} 筆：紅 {lv.get('紅', 0)}、橙 {lv.get('橙', 0)}、黃 {lv.get('黃', 0)}")


if __name__ == "__main__":
    main(force="--force" in sys.argv)
