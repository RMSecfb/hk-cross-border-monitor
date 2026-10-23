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
    return long[["ticker", "time", "open", "high", "low", "close", "volume"]]


def download(tickers, period, interval, logger=None):
    """分批下載，失敗自動重試；回傳長表"""
    import yfinance as yf
    size, pause, retry = SETTINGS["batch_size"], SETTINGS["batch_sleep_sec"], SETTINGS["max_retry"]
    out = []
    for i in range(0, len(tickers), size):
        batch = tickers[i:i + size]
        for attempt in range(1, retry + 1):
            try:
                raw = yf.download(batch, period=period, interval=interval, group_by="ticker",
                                  auto_adjust=False, progress=False, threads=True)
                out.append(_to_long(raw, batch))
                break
            except Exception as e:  # 包含 Yahoo 限流
                if logger:
                    logger.warning(f"批次 {i}-{i + len(batch)} 第 {attempt} 次失敗：{e}")
                time.sleep(pause * 5 * attempt)
        time.sleep(pause)
    return pd.concat(out, ignore_index=True) if out else _to_long(None, tickers)


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
