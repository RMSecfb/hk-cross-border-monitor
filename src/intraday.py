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

from common import (SETTINGS, download, get_logger, latest_file, load_universe, load_yaml, now_hkt, path)
from rules import apply_cooldown, minutes_elapsed, run_rules

log = get_logger("intraday")
ONCE = ["即時漲跌幅", "偏離均線", "成交量暴增", "換手率異常", "開盤價異常"]   # 全日型：同等級一天報一次


def load_stats(today):
    f = latest_file("stats", "stats_*.csv", before=f"{today:%Y%m%d}") or latest_file("stats", "stats_*.csv")
    if f is None:
        raise SystemExit("找不到 stats 基準檔，請先執行盤後初始化：python src/eod.py --init")
    log.info(f"使用基準檔 {f.name}")
    return pd.read_csv(f)


def load_profile():
    f = path("profile", "profile.csv")
    return pd.read_csv(f, dtype={"bucket": str}) if f.exists() else None


def alert_key(df):
    t = df["time"].astype(str).where(~df["rule"].isin(ONCE), "")
    return df["code"].astype(str) + "|" + df["rule"] + "|" + df["level"] + "|" + t


def make_snapshot(bars, uni, stats):
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
    snap["vol_ratio"] = snap["cum_volume"] / (snap["avg_vol"] * minutes_elapsed(snap["last_time"]) / 330)
    snap = snap.replace([np.inf, -np.inf], np.nan)
    return snap.round({"open": 3, "high": 3, "low": 3, "price": 3, "ma": 3, "avg_vol": 0, "turnover_hkd": 0,
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
    stats, profile = load_stats(today), load_profile()
    cfg = load_yaml("rules.yaml")

    raw = download(uni["yahoo"].tolist(), "1d", "1m", log)
    raw = raw[raw["time"].dt.normalize() == today]
    bars = raw.merge(uni[["yahoo", "code"]], left_on="ticker", right_on="yahoo").drop(columns=["ticker", "yahoo"])
    got = bars["code"].nunique()
    log.info(f"取得 {got}/{len(uni)} 檔、{len(bars)} 根 1 分鐘 K 線")
    if bars.empty:
        return

    # ---- 快照 ----
    day = f"{today:%Y-%m-%d}"
    snap = make_snapshot(bars, uni, stats)
    snap.to_parquet(path("snapshot", day, f"snapshot_{stamp}.parquet"), index=False)

    # ---- 監控規則 ----
    alerts = apply_cooldown(run_rules(bars, stats, uni, cfg, profile), cfg["cooldown_min"])
    all_f = path("alerts", f"alerts_{today:%Y%m%d}.csv")
    old = pd.read_csv(all_f, parse_dates=["time"]) if all_f.exists() else alerts.iloc[0:0]
    new = alerts[~alert_key(alerts).isin(set(alert_key(old)))].assign(found_at=stamp)
    pd.concat([old, new]).to_csv(all_f, index=False, encoding="utf-8-sig")
    if not new.empty:
        new.to_csv(path("alerts", f"alerts_new_{stamp}.csv"), index=False, encoding="utf-8-sig")
    lv = new["level"].value_counts().to_dict()

    status = {"run_at": f"{now:%Y-%m-%d %H:%M:%S}", "trade_date": day, "data_until": f"{bars['time'].max():%H:%M}",
              "stocks_ok": int(got), "stocks_total": int(len(uni)),
              "missing": sorted(map(int, set(uni["code"]) - set(bars["code"])))[:500],
              "new_alerts": {k: int(lv.get(k, 0)) for k in ["紅", "橙", "黃"]},
              "snapshot_file": f"snapshot/{day}/snapshot_{stamp}.parquet"}
    path("status.json").write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info(f"新增警報 {len(new)} 筆：紅 {lv.get('紅', 0)}、橙 {lv.get('橙', 0)}、黃 {lv.get('黃', 0)}")


if __name__ == "__main__":
    main(force="--force" in sys.argv)
