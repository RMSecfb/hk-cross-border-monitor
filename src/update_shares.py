"""選用：更新各股股本 config/shares.csv（換手率異常用）
全港股逐檔查詢約需 30–60 分鐘，建議每週六執行一次。
"""
import time

import pandas as pd

from common import CONFIG, get_logger, load_universe

log = get_logger("shares")


def main():
    import yfinance as yf
    uni = load_universe()
    rows = []
    for i, r in enumerate(uni.itertuples(), 1):
        try:
            shares = yf.Ticker(r.yahoo).fast_info.get("shares")
        except Exception as e:
            shares = None
            log.warning(f"{r.yahoo} 失敗：{e}")
        rows.append({"code": r.code, "shares": shares})
        if i % 100 == 0:
            log.info(f"{i}/{len(uni)}")
            time.sleep(5)
    df = pd.DataFrame(rows).dropna()
    df.to_csv(CONFIG / "shares.csv", index=False)
    log.info(f"股本更新完成：{len(df)} 檔")


if __name__ == "__main__":
    main()
