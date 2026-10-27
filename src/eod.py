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
import json
import shutil
import sys

import pandas as pd

import bars_store
import universe
from common import CONFIG, DATA, SETTINGS, download, get_logger, load_universe, load_yaml, now_hkt, path
from report import write_reports
from rules import apply_cooldown, bucket, run_rules

log = get_logger("eod")


def fetch_daily(uni, period):
    d = download(uni["yahoo"].tolist(), period, "1d", log, actions=True)   # actions=True：取得股息，供除息日標記
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
    last = d.groupby("code").tail(1).set_index("code")
    # 停牌判定：Yahoo 於停牌期間把 OHLC 全部填成最後價、成交量 0。
    # 復牌當日與停牌期間的價格比較會產生巨大假跳空（實測 −57%～+88%），故價格類指標需排除。
    halted = ((last["volume"] == 0) & (last["open"] == last["close"])
              & (last["high"] == last["low"]) & (last["open"] == last["high"]))
    stats = pd.DataFrame({
        "prev_close": g["close"].last(),
        "ma": g["close"].apply(lambda s: s.tail(n_ma).mean() if len(s) >= n_ma else None),
        "avg_vol": g["volume"].apply(lambda s: s.tail(n_vol).mean()),
        "avg_turnover": (d["close"] * d["volume"]).groupby(d["code"]).apply(lambda s: s.tail(n_vol).mean()),
        "halted": halted.astype(bool),
        # ex_div 指「asof 當日」是否除息；盤後由 eod_for_day 直接從當日日線取得，
        # 這裡保留供查核與盤中回溯使用（盤中無法預知當日除息，故盤中不做除息標記）
        "ex_div": (last["dividends"] > 0) if "dividends" in last else False,
    }).reset_index()
    shares = CONFIG / "shares.csv"
    if shares.exists():
        sh = pd.read_csv(shares)
        cols = ["code", "shares"] + (["source"] if "source" in sh else [])   # source 由 update_shares.py 產生
        sh = sh[cols].rename(columns={"source": "shares_src"})
        stats = stats.merge(sh, on="code", how="left")
    stats.to_csv(path("stats", f"stats_{asof:%Y%m%d}.csv"), index=False)
    log.info(f"基準檔 stats_{asof:%Y%m%d}.csv：{len(stats)} 檔")
    return stats


def update_profile(bars, window=20):
    """各 30 分鐘時段每分鐘均量（由 1 分鐘資料累積，與盤中量同一來源）
    前 20 天為簡單平均，之後為指數平均（約等於近 20 日），不需保存歷史分鐘資料。"""
    b = bars.assign(bucket=bucket(bars["time"]))
    today = (b.groupby(["code", "bucket"])["volume"].sum() / 30).rename("today")
    f = path("profile", "profile.csv")
    if f.exists():
        old = pd.read_csv(f, dtype={"bucket": str}).set_index(["code", "bucket"])
        if "n" not in old:
            old["n"] = window
        prof = old.join(today, how="outer")
        traded = prof.groupby(level="code")["today"].transform("count") > 0      # 當天有交易的股票才更新
        prof["today"] = prof["today"].where(~traded, prof["today"].fillna(0))
        upd = prof["today"].notna()
        n_new = (prof["n"].fillna(0) + 1).clip(upper=window)
        prof.loc[upd, "vol_per_min"] = prof.loc[upd, "vol_per_min"].fillna(0) + \
            (prof.loc[upd, "today"] - prof.loc[upd, "vol_per_min"].fillna(0)) / n_new[upd]
        prof.loc[upd, "n"] = n_new[upd]
        prof = prof[["vol_per_min", "n"]]
    else:
        prof = today.rename("vol_per_min").to_frame().assign(n=1)
    prof.round({"vol_per_min": 1}).reset_index().to_csv(f, index=False)


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
    eod_for_day(today, raw, daily, uni, cfg)
    prune_snapshots(today)


def eod_for_day(day, raw, daily, uni, cfg):
    """單一交易日的盤後計算（盤後批次與補跑共用）。daily 需含該日與之前的日線，且日線檔已存檔。"""
    raw = raw[raw["time"].dt.normalize() == day]
    bars = raw.merge(uni[["yahoo", "code"]], left_on="ticker", right_on="yahoo").drop(columns=["ticker", "yahoo"])
    log.info(f"{day:%Y-%m-%d} 1 分鐘 K 線：{bars['code'].nunique()} 檔、{len(bars)} 根")

    prev_day = load_daily(day - pd.Timedelta(days=1), 1)["date"].max()
    prev_f = path("stats", f"stats_{prev_day:%Y%m%d}.csv")
    prev_stats = pd.read_csv(prev_f) if prev_f.exists() else make_stats(cfg, prev_day)
    prof_f = path("profile", "profile.csv")
    prof = pd.read_csv(prof_f, dtype={"bucket": str}) if prof_f.exists() else None
    close_day = daily[daily["date"] == day].set_index("code")["close"]
    vol_day = daily[daily["date"] == day].set_index("code")["volume"]    # 換手率分子：日線成交量

    today_row = daily[daily["date"] == day]
    ex_div = set(today_row.loc[today_row["dividends"] > 0, "code"]) if "dividends" in today_row else set()
    if ex_div:
        log.info(f"{day:%Y-%m-%d} 除息股 {len(ex_div)} 檔，價格類警報將加註")
    alerts = apply_cooldown(run_rules(bars, prev_stats, uni, cfg, prof, daily_close=close_day,
                                      ex_div_codes=ex_div, daily_volume=vol_day), cfg["cooldown_min"])
    alerts.to_csv(path("report", f"eod_alerts_{day:%Y%m%d}.csv"), index=False, encoding="utf-8-sig")
    summary = alerts.pivot_table(index="rule", columns="level", values="code", aggfunc="count", fill_value=0)
    summary.to_csv(path("report", f"eod_summary_{day:%Y%m%d}.csv"), encoding="utf-8-sig")
    rep, score = write_reports(alerts, uni, cfg, day)
    log.info(f"{day:%Y-%m-%d} 盤後警報 {len(alerts)} 筆，預警列表 {len(rep)} 列，"
             f"上榜 {(score['score'] >= cfg['list_min_score']).sum()} 檔")

    stats_today = make_stats(cfg, day)
    if not bars.empty:
        update_profile(bars)
        # ---- 1 分 K 歷史：存成本地檔，由 workflow 的 gh CLI 上傳為 Release 附件 ----
        try:
            bars_store.save_local(bars, day, stats_today)
        except Exception as e:
            log.warning(f"1 分 K 存檔失敗（不影響當日監控）：{e}")
        q = bars_store.quality(bars, daily[daily["date"] == day].set_index("code")["volume"])
        if q:
            log.info(f"資料品質 1分K加總÷日線量：中位數 {q['median']}、"
                     f"5–95 百分位 {q['p05']}–{q['p95']}、超過 2 倍 {q['over_2x']} 檔")
            path("quality", f"quality_{day:%Y%m%d}.json").write_text(
                json.dumps(q, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main(init="--init" in sys.argv)
