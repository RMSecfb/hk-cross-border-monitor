"""補跑歷史交易日：重新產出盤後監控結果，並累積 1 分 K 歷史

依日期由舊到新逐日：下載當天 1 分鐘 K 線 → 盤後規則 → 預警列表、風險分數 → stats 基準檔，
同時把 1 分 K 存成 data/bars/bars_YYYYMMDD.parquet（由 workflow 的 gh CLI 上傳為 Release 附件）。

用法：python src/backfill.py 2026-10-21 2026-10-27 [--rebuild-profile]

預設**完全不動** config 以外的分鐘量基準 data/profile/profile.csv：
  補的日子多半已經在 profile 裡，再灌一次會讓基準偏向那幾天，而 profile 正是盤中的退回分母。
  只有在 profile 壞掉、要整個重建時才加 --rebuild-profile（會先清空再依補跑日期重建，
  重建後的累積天數等於補跑天數，若少於 min_profile_days 會讓 3 分鐘量暴增與連續放量暫時失效）。

同時點基準（baseline）不在這裡算：補跑是倒著補歷史，用 available_days() 會抓到比補跑日更晚的
日期。補跑只負責把 1 分 K 存起來，基準交給當天的盤後批次重算後上傳。

Yahoo 的 1 分鐘資料約只保留 30 天。需先完成盤後初始化（data/daily 有這段期間的日線）。
"""
import sys

import pandas as pd

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

    done, skipped = [], []
    for d in days:
        d = pd.Timestamp(d)
        raw = download(uni["yahoo"].tolist(), None, "1m", log,
                       start=f"{d:%Y-%m-%d}", end=f"{d + pd.Timedelta(days=1):%Y-%m-%d}")
        if raw.empty:
            log.warning(f"{d:%Y-%m-%d} 抓不到 1 分鐘資料，略過")
            skipped.append(d)
            continue
        eod_for_day(d, raw, daily, uni, cfg, update_prof=rebuild_profile, build_base=False)
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
