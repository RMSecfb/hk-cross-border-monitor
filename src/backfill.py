"""補跑歷史交易日：重新產出盤後監控結果，並累積 1 分 K 歷史

依日期由舊到新逐日：下載當天 1 分鐘 K 線 → 盤後規則 → 預警列表、風險分數 → stats 基準檔，
同時把 1 分 K 存成 data/bars/bars_YYYYMMDD.parquet（由 workflow 的 gh CLI 上傳為 Release 附件）。

用法：python src/backfill.py 2026-10-21 2026-10-27 [--rebuild-profile]

預設**完全不動** data/profile/profile.csv。profile 已不參與任何指標計算（舊的 30 分鐘時段
  內插分母已移除），僅為保留歷史而維護；再灌一次會讓它偏向補的那幾天，故預設不動。
  只有要整個重建時才加 --rebuild-profile。

同時點基準（baseline）由本程式逐日自行計算，**只用嚴格早於當天的歷史**（本機已補好的日子
＋ Release 上既有的日子，取最近 20 日），所以補到後半段時，報告就是最終版公式的結果：
  前 4 天沒有足夠歷史 → 3 分鐘量暴增與連續放量**不計算**（沒有退回的分母）；第 5 天起開始算，
  成交量暴增要到第 10 天起。這也是為什麼補跑早於封存起點 + 5 個交易日的日子，
  報告會比原版更差——詳見規劃書 1.3。
算出來的基準不另存、不上傳 Release——基準統一交給當天的盤後批次重算後上傳，才不會在盤中
無預警把公式換掉。

Yahoo 的 1 分鐘資料約只保留 30 天。需先完成盤後初始化（data/daily 有這段期間的日線）。
"""
import sys

import pandas as pd

import bars_store
from common import DATA, download, get_logger, load_universe, load_yaml, path
from eod import eod_for_day, load_daily

log = get_logger("backfill")


def main(start, end, rebuild_profile=False):
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if (pd.Timestamp.now() - start).days > 28:
        log.warning("起始日超過約 28 天前，Yahoo 可能已無 1 分鐘資料")
    uni = load_universe()
    cfg = load_yaml("rules.yaml")
    daily = load_daily(end, 400)
    days = sorted(d for d in daily["date"].unique() if start <= d <= end)
    if not days:
        raise SystemExit("這段期間沒有交易日的日線資料，請先執行盤後初始化")
    log.info(f"補跑交易日：{', '.join(f'{d:%m/%d}' for d in days)}")

    prof = path("profile", "profile.csv")
    if rebuild_profile:
        if prof.exists():
            prof.unlink()
        log.warning("已清空分鐘量基準，將只依補跑日期重建")
    else:
        log.info("不更動分鐘量基準 profile.csv（補跑預設行為）")

    try:
        on_release = set(bars_store.available_days(limit=60))
    except Exception as e:
        on_release = set()
        log.warning(f"讀取 Release 上既有的 1 分 K 清單失敗：{e}")

    done, skipped = [], []
    for d in days:
        d = pd.Timestamp(d)
        raw = download(uni["yahoo"].tolist(), None, "1m", log,
                       start=f"{d:%Y-%m-%d}", end=f"{d + pd.Timedelta(days=1):%Y-%m-%d}")
        if raw.empty:
            log.warning(f"{d:%Y-%m-%d} 抓不到 1 分鐘資料，略過")
            skipped.append(d)
            continue
        # 同時點基準只能用「嚴格早於 d」的日子，否則就是拿未來算過去
        prior = sorted(x for x in (set(done) | on_release) if x < d)[-20:]
        base = None
        if prior:
            hist, got = bars_store.load_history(prior)
            if got:
                base = bars_store.build_baseline(hist)
                log.info(f"{d:%Y-%m-%d} 同時點基準用 {len(got)} 個早於當天的交易日"
                         f"（{got[0]:%m/%d}~{got[-1]:%m/%d}）")
        if base is None:
            log.info(f"{d:%Y-%m-%d} 之前無 1 分 K 歷史，量能指標走退回的時段內插法")
        eod_for_day(d, raw, daily, uni, cfg, update_prof=rebuild_profile,
                    build_base=False, baseline=base)
        done.append(d)
    log.info(f"補跑完成 {len(done)} 天"
             + (f"，略過 {len(skipped)} 天：{', '.join(f'{d:%m/%d}' for d in skipped)}" if skipped else "")
             + f"；報告在 {DATA / 'report'}、1 分 K 在 {DATA / 'bars'}")
    if not done:
        raise SystemExit("沒有任何一天補成功")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 2:
        raise SystemExit(__doc__)
    main(args[0], args[1], rebuild_profile="--rebuild-profile" in sys.argv)
