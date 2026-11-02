"""盤後批次（GitHub Actions 每個交易日 16:42 執行）
1. 更新監控名單（HKEX 證券名單 + VCM 名單）→ config/universe.csv
2. 日線：每天一個檔 data/daily/YYYY/daily_YYYYMMDD.parquet（與既有檔合併，不覆寫，見 save_daily）
3. 以收盤資料重跑全部規則（含收市競價）→ data/report/eod_alerts_YYYYMMDD.csv、eod_summary_YYYYMMDD.csv
   ※ 盤中批次若當天失敗，以這份盤後報告為準。
4. 明日盤中基準檔：data/stats/stats_YYYYMMDD.csv、data/profile/profile.csv（各時段每分鐘均量，近 20 日指數平均）
5. 刪除超過保留天數的盤中快照
用法：python src/eod.py                      一般盤後
      python src/eod.py --init               第一次使用：下載約半年日線並建立基準檔（任何時間都可執行）
      python src/eod.py --refetch-daily 3mo  只重抓日線並與既有檔合併，修復被不完整下載覆寫的歷史
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
    """依日期各存一檔；upto 之後（未收市）的資料不存。

    **與既有檔案合併，不覆寫。** 盤後批次每次都重抓最近 5 天，Yahoo 偶爾只回部分資料；
    原本的無條件覆寫會把已經正確的歷史直接摧毀，實際發生過兩次：
      2026-10-21 由 2,789 列被覆寫成 1 列（至 10/30 才發現，已壞 9 天）
      2026-10-28 由 2,790 列被覆寫成 25 列，連帶污染 stats_20261028.csv，
                 使 10/29 整天（盤中 11 批次＋盤後）的 prev_close 有 58%、均量有 67% 是錯的
    重疊的股票以新資料為準（Yahoo 會回頭修正收盤價），既有而本次沒回的一律保留。
    本次回傳列數少於既有時留下警告，供盤後檢查資料品質。
    """
    d = d[d["date"] <= upto]
    for dt, part in d.groupby("date"):
        f = path("daily", f"{dt:%Y}", f"daily_{dt:%Y%m%d}.parquet")
        if f.exists():
            old = pd.read_parquet(f)
            n_old, n_new = len(old), len(part)
            part = (pd.concat([old, part], ignore_index=True)
                      .drop_duplicates(subset="code", keep="last")
                      .sort_values("code").reset_index(drop=True))
            if n_new < n_old:
                log.warning(f"daily_{dt:%Y%m%d}：本次下載 {n_new:,} 列少於既有 {n_old:,} 列，"
                            f"已合併保留 {len(part):,} 列（Yahoo 回傳不完整，未覆寫）")
            elif len(part) > n_old:
                log.info(f"daily_{dt:%Y%m%d}：由 {n_old:,} 列補齊為 {len(part):,} 列")
        part.to_parquet(f, index=False)
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
    # ---- 停牌判定（2026-11-03 改寫，見規劃書九之五）----
    # Yahoo 對「當天沒有成交」有兩種表示法：給一列 volume=0、OHLC 全同，或整列不給。
    # 2026-10-30 起改為後者，原本只認前者的判定因而從約 800 檔掉到 437 檔。
    # 改以「名單 × 交易日」的格線判定：該格沒有列，或列的 volume==0，都算當天沒成交。
    htd = cfg.get("halt_tag_days", {"short": 5, "long": 20, "run_min": 3})
    # 排除資料不完整的日子，否則 2026-10-21 那種幾乎空白的檔案會讓全市場都被判成沒成交。
    # 門檻取近期中位數的七成：10/30 與 11/02 的新表示法約為中位數的 78%，不會被誤擋。
    cnt = d.groupby("date").size()
    days_ok = sorted(cnt[cnt >= cnt.median() * cfg.get("incomplete_day_ratio", 0.7)].index)
    traded = (d[d["date"].isin(days_ok)].assign(_t=lambda x: x["volume"] > 0)
              .pivot_table(index="code", columns="date", values="_t", aggfunc="max")
              .reindex(index=sorted(d["code"].unique()), columns=days_ok)
              .fillna(False).astype(bool))
    notrade = ~traded
    # 價格類排除用：最近一個交易日沒有成交 → prev_close 是過期價格，比出來的跳空是假的。
    halted = notrade[days_ok[-1]] if days_ok else pd.Series(False, index=notrade.index)

    def _max_run(row):
        """一列布林值中最長的連續 True 長度"""
        best = cur = 0
        for v in row:
            cur = cur + 1 if v else 0
            best = max(best, cur)
        return best

    # 量能類標記用：近 N 個交易日內曾有「連續 run_min 天以上」沒成交，才算一段停牌。
    # 零星單日無成交是冬眠股的常態，不是停牌；舊的「任一天」判定會把它們全標上。
    run_min = htd.get("run_min", 3)
    runs = {k: (notrade[days_ok[-htd[k]:]].apply(_max_run, axis=1) >= run_min)
            if days_ok else pd.Series(False, index=notrade.index)
            for k in ("short", "long")}
    stats = pd.DataFrame({
        "prev_close": g["close"].last(),
        "ma": g["close"].apply(lambda s: s.tail(n_ma).mean() if len(s) >= n_ma else None),
        "avg_vol": g["volume"].apply(lambda s: s.tail(n_vol).mean()),
        "avg_turnover": (d["close"] * d["volume"]).groupby(d["code"]).apply(lambda s: s.tail(n_vol).mean()),
        "halted": halted.astype(bool),
        "halt_run_5": runs["short"],
        "halt_run_20": runs["long"],
        # ex_div 指「asof 當日」是否除息；盤後由 eod_for_day 直接從當日日線取得，
        # 這裡保留供查核與盤中回溯使用（盤中無法預知當日除息，故盤中不做除息標記）
        "ex_div": (last["dividends"] > 0) if "dividends" in last else False,
    }).reset_index()
    shares = CONFIG / "shares.csv"
    if shares.exists():
        sh = pd.read_csv(shares)
        # source 由 update_shares.py 產生；舊檔沒有這欄，代表是 fast_info 的 A+H 合計股數，
        # 一律標為 implied，讓換手率警報帶上「股本為A+H合計」的備註，不要靜默用錯分母
        if "source" not in sh:
            sh["source"] = "implied"
        sh = sh[["code", "shares", "source"]].rename(columns={"source": "shares_src"})
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


def refetch_daily(period):
    """只重新下載日線並與既有檔案合併，不做任何其他計算。

    用途是修復被不完整下載覆寫掉的歷史日線（save_daily 的說明有兩次實例）。
    因為 save_daily 已改為合併，重抓只會補齊缺漏，不會蓋掉既有的正確資料，
    所以即使這次下載也不完整，也不會把情況弄得更糟。
    用法：python src/eod.py --refetch-daily 3mo
    """
    uni = load_universe()
    daily = fetch_daily(uni, period)
    if daily.empty:
        raise SystemExit("日線下載失敗，既有檔案未更動")
    now = now_hkt()
    today = now.normalize().tz_localize(None)
    closed = now.strftime("%H%M") >= "1630"
    last_full = daily["date"].max() if closed else daily.loc[daily["date"] < today, "date"].max()
    n = save_daily(daily, last_full)
    log.info(f"重抓日線（{period}）：涵蓋 {n} 個交易日，最新完整交易日 {last_full:%Y-%m-%d}")


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


def eod_for_day(day, raw, daily, uni, cfg, update_prof=True, build_base=True, baseline="auto"):
    """單一交易日的盤後計算（盤後批次與補跑共用）。daily 需含該日與之前的日線，且日線檔已存檔。

    update_prof=False：不更新分鐘量基準 profile.csv。補跑時必須關掉——補的日子多半已經在
      profile 裡，再灌一次會讓基準偏向那幾天，而 profile 正是盤中的退回分母。
    build_base=False：跑完後不重算、不另存同時點基準檔。補跑時關掉——基準統一交給當天的
      盤後批次重算後上傳，補跑只負責累積 1 分 K。
    baseline="auto"：自行向 Release 取最近一份基準（盤後批次用）。補跑要自己算，因為
      available_days() 會抓到比補跑日更晚的日期，必須由呼叫端只餵嚴格早於 day 的歷史；
      傳 None 則明確表示沒有基準，一律走退回的時段內插法。
    """
    raw = raw[raw["time"].dt.normalize() == day]
    bars = raw.merge(uni[["yahoo", "code"]], left_on="ticker", right_on="yahoo").drop(columns=["ticker", "yahoo"])
    log.info(f"{day:%Y-%m-%d} 1 分鐘 K 線：{bars['code'].nunique()} 檔、{len(bars)} 根")

    prev_day = load_daily(day - pd.Timedelta(days=1), 1)["date"].max()
    prev_f = path("stats", f"stats_{prev_day:%Y%m%d}.csv")
    prev_stats = pd.read_csv(prev_f) if prev_f.exists() else make_stats(cfg, prev_day)
    if not {"halted", "halt_run_5", "halt_run_20"} <= set(prev_stats):   # 舊格式缺停牌欄，重算一份
        log.info(f"stats_{prev_day:%Y%m%d}.csv 為舊格式（缺停牌欄位），重新產生")
        prev_stats = make_stats(cfg, prev_day)
    # prev_close／ma／avg_vol 必須是「截至前一交易日」的值，但股本是靜態屬性，不該沿用快照。
    # 否則 shares.csv 更新後要再等一個交易日才生效（換手率的分母會慢一天）。
    sh_f = CONFIG / "shares.csv"
    if sh_f.exists():
        sh = pd.read_csv(sh_f)
        if "source" not in sh:
            sh["source"] = "implied"        # 舊檔沒有來源欄，一律視為 A+H 合計
        prev_stats = (prev_stats.drop(columns=[c for c in ("shares", "shares_src") if c in prev_stats])
                      .merge(sh[["code", "shares", "source"]].rename(columns={"source": "shares_src"}),
                             on="code", how="left"))
    if isinstance(baseline, str) and baseline == "auto":
        try:
            base = bars_store.fetch_baseline(before=day)  # 用不含當日的基準，與盤中同條件
        except Exception as e:
            base = None
            log.warning(f"同時點基準取得失敗，改用時段內插法：{e}")
    else:
        base = baseline
    close_day = daily[daily["date"] == day].set_index("code")["close"]
    vol_day = daily[daily["date"] == day].set_index("code")["volume"]    # 換手率分子：日線成交量

    today_row = daily[daily["date"] == day]
    ex_div = set(today_row.loc[today_row["dividends"] > 0, "code"]) if "dividends" in today_row else set()
    if ex_div:
        log.info(f"{day:%Y-%m-%d} 除息股 {len(ex_div)} 檔，價格類警報將加註")
    alerts = apply_cooldown(run_rules(bars, prev_stats, uni, cfg, daily_close=close_day,
                                      ex_div_codes=ex_div, daily_volume=vol_day,
                                      baseline=base), cfg["cooldown_min"])
    alerts.to_csv(path("report", f"eod_alerts_{day:%Y%m%d}.csv"), index=False, encoding="utf-8-sig")
    summary = alerts.pivot_table(index="rule", columns="level", values="code", aggfunc="count", fill_value=0)
    summary.to_csv(path("report", f"eod_summary_{day:%Y%m%d}.csv"), encoding="utf-8-sig")
    rep, score = write_reports(alerts, uni, cfg, day)
    log.info(f"{day:%Y-%m-%d} 盤後警報 {len(alerts)} 筆，預警列表 {len(rep)} 列，"
             f"上榜 {(score['score'] >= cfg['list_min_score']).sum()} 檔")

    stats_today = make_stats(cfg, day)
    if not bars.empty:
        if update_prof:
            update_profile(bars)
        # ---- 1 分 K 歷史：存成本地檔，由 workflow 的 gh CLI 上傳為 Release 附件 ----
        try:
            bars_store.save_local(bars, day, stats_today)
            if build_base:
                # 同時點基準：含當日在內的近 20 日，供明天盤中直接使用（盤中只需抓這一個小檔）
                hist, got = bars_store.load_history(bars_store.available_days(limit=25)[:20] or [day])
                if day not in got:    # Release 尚未上傳當日檔，補上本地剛存的
                    today_df = bars_store.to_frame(bars, stats_today).assign(day=day)
                    hist = pd.concat([hist, today_df], ignore_index=True)
                    got = sorted(set(got) | {day})
                bl = bars_store.build_baseline(hist)
                if not bl.empty:
                    bars_store.save_baseline(bl, day)
                    log.info(f"同時點基準使用 {len(got)} 個交易日"
                             f"（3分鐘/連續放量取近 5 日、成交量暴增取近 20 日）")
        except Exception as e:
            log.warning(f"1 分 K 存檔／基準計算失敗（不影響當日監控）：{e}")
        q = bars_store.quality(bars, daily[daily["date"] == day].set_index("code")["volume"])
        if q:
            log.info(f"資料品質 1分K加總÷日線量：中位數 {q['median']}、"
                     f"5–95 百分位 {q['p05']}–{q['p95']}、超過 2 倍 {q['over_2x']} 檔")
            path("quality", f"quality_{day:%Y%m%d}.json").write_text(
                json.dumps(q, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    if "--refetch-daily" in sys.argv:
        i = sys.argv.index("--refetch-daily")
        refetch_daily(sys.argv[i + 1] if len(sys.argv) > i + 1 else "3mo")
    else:
        main(init="--init" in sys.argv)
