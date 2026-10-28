"""1 分 K 歷史保存：存成精簡 parquet，以 GitHub Release 附件保存

為什麼用 Release 而非 commit 進 repo：
  git 歷史不會因刪檔而縮小。每日 1.9 MB 的 1 分 K 若 commit 進 repo，即使只保留近 30 天的檔案，
  舊版本仍留在 .git 內，repo 將以每年約 0.47 GB 永久累積。
  GitHub 官方文件載明 Release 附件「不限總大小、不限下載頻寬」，且刪除後空間即回收。

用途：3 分鐘量暴增、連續放量需過去 5 個交易日；成交量暴增需過去 20 個交易日的「同時點」基準。

環境變數：
  GITHUB_TOKEN       GitHub Actions 自動提供（需 contents: write 權限）
  GITHUB_REPOSITORY  例如 "RMSecfb/hk-cross-border-monitor"
"""
import io
import os

import pandas as pd
import requests

from common import DATA, get_logger, path

log = get_logger("bars")

API = "https://api.github.com"
TAG_PREFIX = "bars-"
KEEP_DAYS = 30                 # Release 保留天數（規則只需 20 天，留 10 天緩衝）
MIN_AVG_TURNOVER = 500_000     # 收錄門檻：20 日均成交額 ≥ 50 萬港元


def _repo():
    return os.environ.get("GITHUB_REPOSITORY", "")


def _headers():
    tok = os.environ.get("GITHUB_TOKEN", "")
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def enabled():
    """有 token 與 repo 才啟用；本機測試時自動略過"""
    return bool(os.environ.get("GITHUB_TOKEN") and _repo())


# ---------------- 本地檔案 ----------------
def to_frame(bars, stats=None):
    """整理成精簡格式：code(int32)、m(分鐘，int16)、volume(int64)
    不存價格（日線與快照已有），以控制體積。"""
    b = bars[["code", "time", "volume"]].copy()
    b["m"] = (b["time"].dt.hour * 60 + b["time"].dt.minute).astype("int16")
    b["code"] = b["code"].astype("int32")
    b["volume"] = pd.to_numeric(b["volume"], errors="coerce").fillna(0).astype("int64")
    b = b[b["volume"] > 0]
    if stats is not None and "avg_turnover" in stats:
        keep = set(stats.loc[stats["avg_turnover"] >= MIN_AVG_TURNOVER, "code"].astype(int))
        if keep:
            b = b[b["code"].isin(keep)]
    return b[["code", "m", "volume"]].sort_values(["code", "m"]).reset_index(drop=True)


def save_local(bars, day, stats=None):
    f = path("bars", f"bars_{day:%Y%m%d}.parquet")
    df = to_frame(bars, stats)
    df.to_parquet(f, compression="zstd", index=False)
    log.info(f"1 分 K 存檔 {f.name}：{df['code'].nunique()} 檔、{len(df):,} 列、{f.stat().st_size / 1e6:.1f} MB")
    return f


# ---------------- Release 操作 ----------------
def _release(tag):
    r = requests.get(f"{API}/repos/{_repo()}/releases/tags/{tag}", headers=_headers(), timeout=30)
    return r.json() if r.status_code == 200 else None


# 上傳與刪除由 .github/workflows/eod.yml 內的 gh CLI 執行，不在 Python 內做：
#   1. gh 為 runner 內建，不需自行處理 Release 的 multipart 上傳網址
#   2. 寫入失敗會直接讓該步驟失敗並顯示於 Actions，不會被 Python 的例外處理吞掉
# 本模組只負責存檔（save_local）與讀取（fetch / load_history / available_days）。


def fetch(day):
    """下載指定交易日的 1 分 K；本地已有就直接用"""
    local = DATA / "bars" / f"bars_{day:%Y%m%d}.parquet"
    if local.exists():
        return pd.read_parquet(local)
    if not enabled():
        return None
    rel = _release(f"{TAG_PREFIX}{day:%Y%m%d}")
    if not rel:
        return None
    for a in rel.get("assets", []):
        # 同一個 Release 可能同時有 baseline_*.parquet，只能取 bars_ 開頭的
        if a["name"].startswith("bars_") and a["name"].endswith(".parquet"):
            r = requests.get(a["url"], headers=_headers() | {"Accept": "application/octet-stream"}, timeout=180)
            if r.status_code == 200:
                return pd.read_parquet(io.BytesIO(r.content))
    return None


def load_history(days):
    """載入多個交易日並合併；回傳 (DataFrame, 實際取得的日期清單)"""
    got, frames = [], []
    for d in days:
        df = fetch(d)
        if df is not None and not df.empty:
            frames.append(df.assign(day=pd.Timestamp(d).normalize()))
            got.append(pd.Timestamp(d).normalize())
    if not frames:
        return pd.DataFrame(columns=["code", "m", "volume", "day"]), []
    return pd.concat(frames, ignore_index=True), got


def available_days(limit=60):
    """列出 Release 上現有的交易日（新到舊）"""
    if not enabled():
        files = sorted((DATA / "bars").glob("bars_*.parquet")) if (DATA / "bars").exists() else []
        return [pd.Timestamp(f.stem[-8:]) for f in files][::-1][:limit]
    r = requests.get(f"{API}/repos/{_repo()}/releases", headers=_headers(),
                     params={"per_page": 100}, timeout=30)
    if r.status_code != 200:
        return []
    out = []
    for rel in r.json():
        t = rel.get("tag_name", "")
        if t.startswith(TAG_PREFIX) and len(t) == len(TAG_PREFIX) + 8:
            try:
                out.append(pd.Timestamp(t[len(TAG_PREFIX):]))
            except ValueError:
                pass
    return sorted(out, reverse=True)[:limit]


def expired_tags(today, keep_days=KEEP_DAYS):
    """列出超過保留天數、應刪除的 Release tag（實際刪除由 workflow 的 gh CLI 執行）"""
    cut = pd.Timestamp(today).normalize() - pd.Timedelta(days=keep_days)
    return [f"{TAG_PREFIX}{d:%Y%m%d}" for d in available_days(limit=200) if d < cut]


# ---------------- 同時點基準（階段 4）----------------
# 規則端需要的是「過去 N 日同一時點」的三個基準，不是原始 1 分 K。
# 若每個盤中批次都去抓 20 天的原始檔（約 40 MB），下載量與時間都不划算，
# 因此盤後算好一份精簡基準檔，與當日 1 分 K 放在同一個 Release，盤中只抓這一個檔。
#   avg1   = 過去 5 日，該分鐘的平均成交量          → 連續放量
#   avg3   = 過去 5 日，結束於該分鐘的 3 分鐘平均量 → 3 分鐘量暴增
#   avgcum = 過去 20 日，09:30 至該分鐘的平均累計量 → 成交量暴增
# 平均一律除以「該檔有交易的天數」而非有資料的筆數：沒有 K 線代表那一分鐘沒成交（視為 0），
# 但整天停牌的日子要剔除，否則分母被壓低、復牌當天的倍數會失真。

def _day_count(h):
    """每檔有實際交易的天數（整日無量者不計入）"""
    t = h.groupby(["code", "day"])["volume"].sum()
    return t[t > 0].reset_index().groupby("code")["day"].nunique()


def build_baseline(hist, days_short=5, days_long=20):
    """由多日 1 分 K 算出同時點基準。hist 需含 code/m/volume/day。"""
    if hist is None or hist.empty:
        return pd.DataFrame(columns=["code", "m", "avg1", "avg3", "avgcum"])
    hist = hist[["code", "m", "volume", "day"]]
    all_days = sorted(hist["day"].unique())
    short = hist[hist["day"].isin(all_days[-days_short:])]
    long_ = hist[hist["day"].isin(all_days[-days_long:])]
    n_s, n_l = _day_count(short), _day_count(long_)

    # 每分鐘平均量
    avg1 = short.groupby(["code", "m"])["volume"].sum().reset_index()
    avg1["avg1"] = avg1["volume"] / avg1["code"].map(n_s)

    # 結束於該分鐘的 3 分鐘平均量：把每筆分別記到 m、m+1、m+2 三個視窗再加總
    w = pd.concat([short.assign(m=short["m"] + k) for k in (0, 1, 2)], ignore_index=True)
    avg3 = w.groupby(["code", "m"])["volume"].sum().reset_index()
    avg3["avg3"] = avg3["volume"] / avg3["code"].map(n_s)

    # 09:30 至該分鐘的平均累計量。
    # 不能直接用「同一分鐘跨日加總 ÷ 天數」：某分鐘只有 2/20 天有成交時，其餘 18 天的累計量
    # 並不是 0（是前一筆的累計值），那樣算會把分母嚴重低估。先補成每分鐘都有的稠密矩陣再累計。
    grid = range(int(long_["m"].min()), int(long_["m"].max()) + 1)
    piv = (long_.pivot_table(index=["code", "day"], columns="m", values="volume",
                             aggfunc="sum", fill_value=0)
                .reindex(columns=grid, fill_value=0))
    # 每一列是「某檔某日」，故 mean 的分母就是該檔有交易的天數
    avgcum = (piv.cumsum(axis=1).groupby(level="code").mean()
                 .stack().rename("avgcum").reset_index())

    out = (avg1[["code", "m", "avg1"]]
           .merge(avg3[["code", "m", "avg3"]], on=["code", "m"], how="outer")
           .merge(avgcum[["code", "m", "avgcum"]], on=["code", "m"], how="outer"))
    # 逐檔的可用天數：規則端據此判斷該檔是否已有足夠歷史可用同時點分母
    # （新上市或長期停牌的股票，即使全市場已累積 20 天，它自己也可能只有 2 天）
    out["n_short"] = out["code"].map(n_s).fillna(0).astype("int16")
    out["n_long"] = out["code"].map(n_l).fillna(0).astype("int16")
    out["code"] = out["code"].astype("int32")
    out["m"] = out["m"].astype("int16")
    for c in ("avg1", "avg3", "avgcum"):
        out[c] = out[c].astype("float32")
    return out.sort_values(["code", "m"]).reset_index(drop=True)


def save_baseline(df, day):
    f = path("baseline", f"baseline_{day:%Y%m%d}.parquet")
    df.to_parquet(f, compression="zstd", index=False)
    log.info(f"同時點基準 {f.name}：{df['code'].nunique()} 檔、{len(df):,} 列、"
             f"{f.stat().st_size / 1e6:.1f} MB")
    return f


def fetch_baseline(before=None):
    """取最近一個交易日的同時點基準（盤中用）。before 為 Timestamp 時只取早於該日者。"""
    local = sorted((DATA / "baseline").glob("baseline_*.parquet")) if (DATA / "baseline").exists() else []
    if before is not None:
        local = [f for f in local if f.stem[-8:] < f"{pd.Timestamp(before):%Y%m%d}"]
    if local:
        return pd.read_parquet(local[-1])
    if not enabled():
        return None
    for d in available_days(limit=10):
        if before is not None and d >= pd.Timestamp(before).normalize():
            continue
        rel = _release(f"{TAG_PREFIX}{d:%Y%m%d}")
        for a in (rel or {}).get("assets", []):
            if a["name"].startswith("baseline_"):
                r = requests.get(a["url"], headers=_headers() | {"Accept": "application/octet-stream"},
                                 timeout=180)
                if r.status_code == 200:
                    log.info(f"取得同時點基準 {a['name']}")
                    return pd.read_parquet(io.BytesIO(r.content))
    return None


# ---------------- 資料品質 ----------------
def quality(bars, daily_volume):
    """1 分 K 加總 ÷ 日線量。理論上應接近 1，實測會在 0.28～6.49 之間跳動，
    用於監看 Yahoo 分鐘資料的可信度（只記錄，不影響判定）。"""
    if bars.empty or daily_volume is None or len(daily_volume) == 0:
        return {}
    s = bars.groupby("code")["volume"].sum()
    j = pd.concat([s.rename("m1"), pd.Series(daily_volume).rename("d1")], axis=1).dropna()
    j = j[j["d1"] > 0]
    if j.empty:
        return {}
    r = j["m1"] / j["d1"]
    return {"compared": int(len(r)), "median": round(float(r.median()), 2),
            "p05": round(float(r.quantile(.05)), 2), "p95": round(float(r.quantile(.95)), 2),
            "over_2x": int((r > 2).sum()), "under_0p5x": int((r < 0.5).sum())}
