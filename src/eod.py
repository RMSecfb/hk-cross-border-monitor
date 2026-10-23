"""盤後批次（GitHub Actions 每個交易日 16:42 執行）
1. 更新監控名單（HKEX 證券名單 + VCM 名單）→ config/universe.csv
2. 日線：每天一個檔 data/daily/YYYY/daily_YYYYMMDD.parquet（只新增、不覆寫，repo 不會快速膨脹）
3. 以收盤資料重跑全部規則（含收市競價）→ data/report/eod_alerts_YYYYMMDD.csv、eod_summary_YYYYMMDD.csv
   ※ 盤中批次若當天失敗，以這份盤後報告為準。
4. 明日盤中基準檔：data/stats/stats_YYYYMMDD.csv、data/profile/profile.csv（各時段每分鐘均量，近 20 日指數平均）
5. 刪除超過保留天數的盤中快照
用法：python src/eod.py           一般盤後
      python src/eod.py --init    第一次使用：下載約半年日線並建立基準檔（任何時間都可執行）
"""
import shutil
import sys

import pandas as pd

import universe
from common import CONFIG, DATA, SETTINGS, download, get_logger, load_universe, load_yaml, now_hkt, path
from rules import apply_cooldown, bucket, run_rules

log = get_logger("eod")


def fetch_daily(uni, period):
    d = download(uni["yahoo"].tolist(), period, "1d", log)
    d = d.merge(uni[["yahoo", "code"]], left_on="ticker", right_on="yahoo").drop(columns=["ticker", "yahoo"])
    d["date"] = d["time"].dt.normalize()
    return d.drop(columns="time")


def save_daily(d, upto):
    """依日期各存一檔；upto 之後（未收市）的資料不存"""
    d = d[d["date"] <= upto]
    for dt, part in d.groupby("date"):
        part.to_parquet(path("daily", f"{dt:%Y}", f"daily_{dt:%Y%m%d}.parquet"), index=False)
    return d["date"].nunique()


def load_daily(upto, n):
    files = sorted((DATA / "daily").glob("*/daily_*.parquet"))
    files = [f for f in files if f.stem[-8:] <= f"{upto:%Y%m%d}"][-n:]
    return pd.concat([pd.read_parquet(f) for f in files]) if files else pd.DataFrame()


def make_stats(cfg, asof):
    d = load_daily(asof, SETTINGS["history_days"]).sort_values(["code", "date"])
    g = d.groupby("code")
    n_ma, n_vol = cfg["ma_days"], cfg["vol_avg_days"]
    stats = pd.DataFrame({
        "prev_close": g["close"].last(),
        "ma": g["close"].apply(lambda s: s.tail(n_ma).mean() if len(s) >= n_ma else None),
        "avg_vol": g["volume"].apply(lambda s: s.tail(n_vol).mean()),
    }).reset_index()
    shares = CONFIG / "shares.csv"
    if shares.exists():
        stats = stats.merge(pd.read_csv(shares)[["code", "shares"]], on="code", how="left")
    stats.to_csv(path("stats", f"stats_{asof:%Y%m%d}.csv"), index=False)
    log.info(f"基準檔 stats_{asof:%Y%m%d}.csv：{len(stats)} 檔")
    return stats


def update_profile(bars, alpha=1 / 20):
    """各 30 分鐘時段每分鐘均量，以指數平均累積（約等於近 20 日平均），不需保存歷史分鐘資料"""
    b = bars.assign(bucket=bucket(bars["time"]))
    today = (b.groupby(["code", "bucket"])["volume"].sum() / 30).rename("today")
    f = path("profile", "profile.csv")
    if f.exists():
        old = pd.read_csv(f, dtype={"bucket": str}).set_index(["code", "bucket"])["vol_per_min"]
        prof = pd.concat([old, today], axis=1)
        prof["vol_per_min"] = prof["vol_per_min"].fillna(prof["today"]) * (1 - alpha) + prof["today"].fillna(0) * alpha
        prof = prof["vol_per_min"]
    else:
        prof = today.rename("vol_per_min")
    prof.round(1).reset_index().to_csv(f, index=False)


def prune_snapshots(today):
    keep = SETTINGS["keep_snapshot_days"]
    for d in sorted((DATA / "snapshot").glob("????-??-??")):
        if (today - pd.Timestamp(d.name)).days > keep:
            shutil.rmtree(d)
            log.info(f"刪除舊快照 {d.name}")


def main(init=False):
    now = now_hkt()
    today = now.normalize().tz_localize(None)
    closed = now.strftime("%H%M") >= "1630"
    universe.main()
    uni = load_universe()
    cfg = load_yaml("rules.yaml")

    daily = fetch_daily(uni, SETTINGS["init_period"] if init else "5d")
    if daily.empty:
        raise SystemExit("日線下載失敗")
    last_full = daily["date"].max() if closed else daily.loc[daily["date"] < today, "date"].max()
    n = save_daily(daily, last_full)
    log.info(f"日線存檔 {n} 天，最新完整交易日 {last_full:%Y-%m-%d}")

    if last_full != today:
        # 假期、或尚未收市（例如盤中執行 --init）：只建立基準檔
        make_stats(cfg, last_full)
        return

    # ---- 當日已收市：盤後全量重算 ----
    raw = download(uni["yahoo"].tolist(), "1d", "1m", log)
    raw = raw[raw["time"].dt.normalize() == today]
    bars = raw.merge(uni[["yahoo", "code"]], left_on="ticker", right_on="yahoo").drop(columns=["ticker", "yahoo"])
    log.info(f"當日 1 分鐘 K 線：{bars['code'].nunique()} 檔、{len(bars)} 根")

    prev_day = load_daily(today - pd.Timedelta(days=1), 1)["date"].max()
    prev_f = path("stats", f"stats_{prev_day:%Y%m%d}.csv")
    prev_stats = pd.read_csv(prev_f) if prev_f.exists() else make_stats(cfg, prev_day)
    prof_f = path("profile", "profile.csv")
    prof = pd.read_csv(prof_f, dtype={"bucket": str}) if prof_f.exists() else None
    close_today = daily[daily["date"] == today].set_index("code")["close"]

    alerts = apply_cooldown(run_rules(bars, prev_stats, uni, cfg, prof, daily_close=close_today), cfg["cooldown_min"])
    alerts.to_csv(path("report", f"eod_alerts_{today:%Y%m%d}.csv"), index=False, encoding="utf-8-sig")
    summary = alerts.pivot_table(index="rule", columns="level", values="code", aggfunc="count", fill_value=0)
    summary.to_csv(path("report", f"eod_summary_{today:%Y%m%d}.csv"), encoding="utf-8-sig")
    log.info(f"盤後警報 {len(alerts)} 筆")

    make_stats(cfg, today)
    if not bars.empty:
        update_profile(bars)
    prune_snapshots(today)


if __name__ == "__main__":
    main(init="--init" in sys.argv)
