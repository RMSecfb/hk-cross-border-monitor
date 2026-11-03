"""共用工具：設定、路徑、日誌、Yahoo 批次下載"""
import logging
import time
from pathlib import Path

import pandas as pd
import yaml

HKT = "Asia/Hong_Kong"
ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"


def load_yaml(name):
    with open(CONFIG / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


SETTINGS = load_yaml("settings.yaml")
DATA = Path(SETTINGS["data_dir"])
if not DATA.is_absolute():
    DATA = ROOT / DATA


def path(*parts):
    """在 data_dir 底下建立資料夾並回傳路徑"""
    p = DATA.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def now_hkt():
    return pd.Timestamp.now(tz=HKT)


def get_logger(name):
    """輸出到畫面（GitHub Actions 的執行紀錄會保存）"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(h)
    return logger


def to_yahoo(code):
    """港交所代碼 → Yahoo 代碼：700 → 0700.HK、09988 → 9988.HK"""
    return f"{int(code):04d}.HK"


def _to_long(raw, tickers):
    """yfinance 多檔下載結果（MultiIndex 欄位）→ 長表 [ticker, time, open, high, low, close, volume]"""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["ticker", "time", "open", "high", "low", "close", "volume"])
    if not isinstance(raw.columns, pd.MultiIndex):           # 只有一檔時
        raw = pd.concat({tickers[0]: raw}, axis=1)
    lvl0 = set(raw.columns.get_level_values(0))
    tick_level = 0 if lvl0 & set(tickers) else 1
    long = raw.stack(level=tick_level, future_stack=True).rename_axis(["time", "ticker"]).reset_index()
    long.columns = [str(c).lower() for c in long.columns]
    long = long.rename(columns={"datetime": "time", "date": "time"})
    long = long.dropna(subset=["close"])
    t = pd.to_datetime(long["time"])
    if t.dt.tz is not None:
        t = t.dt.tz_convert(HKT).dt.tz_localize(None)
    long["time"] = t
    cols = ["ticker", "time", "open", "high", "low", "close", "volume"]
    if "dividends" in long.columns:          # 日線加抓 actions 時才有：供除息日標記
        long["dividends"] = pd.to_numeric(long["dividends"], errors="coerce").fillna(0)
        cols.append("dividends")
    # 股份分割／合併比例。Yahoo 的歷史價格多已還原過，此欄只作事件註記（見 eod.recent_splits）。
    # 2026-11-03 補上：先前只取 dividends，把這一欄丟掉了，導致日線序列橫跨分割點而未還原——
    # 實測 2723 於 2026-10-06 由 747.00 跌到 70.65（10 股換 1 股），MA20 因而失真，
    # 偏離均線連續多日報紅燈；1780 與 1094 更在 10/30 分割當天衝上風險排行第 1 與第 3 名。
    if "stock splits" in long.columns:
        long["splits"] = pd.to_numeric(long["stock splits"], errors="coerce").fillna(0)
        cols.append("splits")
    return long[cols]


def download(tickers, period, interval, logger=None, start=None, end=None, actions=False):
    """分批下載，失敗自動重試；回傳長表。指定 start/end（YYYY-MM-DD）時忽略 period
    actions=True 時一併取得股息（僅日線使用，供除息日標記）"""
    import yfinance as yf
    size, pause, retry = SETTINGS["batch_size"], SETTINGS["batch_sleep_sec"], SETTINGS["max_retry"]
    out = []
    for i in range(0, len(tickers), size):
        batch = tickers[i:i + size]
        for attempt in range(1, retry + 1):
            try:
                when = {"start": start, "end": end} if start else {"period": period}
                raw = yf.download(batch, interval=interval, group_by="ticker", actions=actions,
                                  auto_adjust=False, progress=False, threads=True, **when)
                out.append(_to_long(raw, batch))
                break
            except Exception as e:  # 包含 Yahoo 限流
                if logger:
                    logger.warning(f"批次 {i}-{i + len(batch)} 第 {attempt} 次失敗：{e}")
                time.sleep(pause * 5 * attempt)
        time.sleep(pause)
    if not out:
        return _to_long(None, tickers)
    df = pd.concat(out, ignore_index=True)
    # 同一檔同一時點只留一筆：重複會讓滾動加總與累計量灌水，進而污染所有量能指標
    n0 = len(df)
    df = (df.drop_duplicates(subset=["ticker", "time"], keep="last")
            .sort_values(["ticker", "time"]).reset_index(drop=True))   # 去重會打亂順序，重新排好
    if logger and len(df) < n0:
        logger.warning(f"下載結果有 {n0 - len(df):,} 筆重複（ticker+time），已去除")
    return df


def latest_file(folder, pattern, before=None):
    """取資料夾中檔名排序最後的檔案；before='20261026' 時只取早於該日期者"""
    folder = DATA / folder
    files = sorted(folder.glob(pattern)) if folder.exists() else []
    if before:
        files = [f for f in files if f.stem.split("_")[-1][:8] < before]
    return files[-1] if files else None


def load_universe():
    f = CONFIG / "universe.csv"
    if not f.exists():
        raise SystemExit("找不到 config/universe.csv，請先執行：python src/universe.py")
    return pd.read_csv(f, dtype={"name": str})
