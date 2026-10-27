"""更新各股股本 config/shares.csv（換手率異常的分母）

分母必須是「在港上市股數」，不是公司總股本。
`fast_info["shares"]` 回傳的是 Implied Shares Outstanding＝A 股＋H 股合計，
對 A+H 兩地上市公司會把分母放大數倍，換手率因而被低估：

    01398 工商銀行　fast_info["shares"] = 3,564 億（A+H）
                    info["sharesOutstanding"] =  868 億（在港 H 股）   → 差 4.1 倍

故改用 `info["sharesOutstanding"]`。代價是 `.info` 比 `.fast_info` 慢很多
（全港股約 1–2 小時），所以本程式不放進每日排程，建議每週手動或排程執行一次。
缺值時退回 fast_info 並在 source 欄標記，供報表判讀。
"""
import time

import pandas as pd

from common import CONFIG, get_logger, load_universe

log = get_logger("shares")


def fetch_one(tk):
    """回傳 (股數, 來源)。優先在港股數，取不到才退回 A+H 合計。"""
    import yfinance as yf
    t = yf.Ticker(tk)
    try:
        v = (t.info or {}).get("sharesOutstanding")
        if v and v > 0:
            return float(v), "hk"
    except Exception as e:
        log.warning(f"{tk} info 失敗：{e}")
    try:
        v = t.fast_info.get("shares")
        if v and v > 0:
            return float(v), "implied"      # A+H 合計，換手率會偏低
    except Exception as e:
        log.warning(f"{tk} fast_info 失敗：{e}")
    return None, None


def main():
    uni = load_universe()
    rows = []
    for i, r in enumerate(uni.itertuples(), 1):
        shares, src = fetch_one(r.yahoo)
        rows.append({"code": r.code, "shares": shares, "source": src})
        if i % 100 == 0:
            log.info(f"{i}/{len(uni)}")
            time.sleep(5)
    df = pd.DataFrame(rows).dropna(subset=["shares"])
    # 本次查不到的，沿用上一版的值，避免單次網路不穩就讓換手率整批失效
    f = CONFIG / "shares.csv"
    if f.exists():
        old = pd.read_csv(f)
        if "source" not in old:
            old["source"] = "implied"                    # 舊檔一律為 fast_info 的 A+H 合計
        keep = old[~old["code"].isin(df["code"])]
        if len(keep):
            log.info(f"{len(keep)} 檔本次查詢失敗，沿用舊值")
        df = pd.concat([df, keep], ignore_index=True).sort_values("code")
    df.to_csv(f, index=False)
    n_fb = int((df["source"] == "implied").sum())
    log.info(f"股本更新完成：{len(df)} 檔，其中 {n_fb} 檔取不到在港股數、退回 A+H 合計（換手率會偏低）")


if __name__ == "__main__":
    main()
