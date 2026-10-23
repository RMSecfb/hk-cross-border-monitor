"""更新監控名單 config/universe.csv
來源：
  1. HKEX 證券名單 ListOfSecurities.xlsx → 全部股本證券（主板 + GEM）
  2. HKEX VCM 證券名單 VCM_list{生效日}_csv.csv → 各股 VCM 觸發門檻（±5/10/15/20%）
建議每日盤後（eod.py 會自動呼叫）或每週執行一次。
"""
import csv
import io
import re

import pandas as pd
import requests

from common import CONFIG, SETTINGS, get_logger, now_hkt, to_yahoo

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
LIST_URL = "https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx"
VCM_PAGE = "https://www.hkex.com.hk/Services/Trading/Securities/Securities-Lists/Volatility-Control-Mechanism-(VCM)-Securities?sc_lang=en"
VCM_BASE = ("https://www.hkex.com.hk/-/media/HKEX-Market/Services/Trading/Securities/Securities-Lists/"
            "Volatility-Control-Mechanism-(VCM)-Securities/")

log = get_logger("universe")


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.content


def parse_securities(xlsx_bytes):
    raw = pd.read_excel(io.BytesIO(xlsx_bytes), header=None, dtype=str)
    hdr = raw.index[raw.apply(lambda r: r.astype(str).str.strip().eq("Stock Code").any(), axis=1)][0]
    df = raw.iloc[hdr + 1:].copy()
    df.columns = [str(c).strip() for c in raw.iloc[hdr]]
    df = df[df["Category"].astype(str).str.strip().eq("Equity")]
    out = pd.DataFrame({
        "code": pd.to_numeric(df["Stock Code"], errors="coerce"),
        "name": df["Name of Securities"].astype(str).str.strip(),
        "board": df["Sub-Category"].astype(str).str.contains("GEM", case=False).map({True: "GEM", False: "MB"}),
    }).dropna(subset=["code"])
    out["code"] = out["code"].astype(int)
    return out


def parse_vcm(csv_bytes):
    text = csv_bytes.decode("utf-8-sig", errors="ignore").splitlines()
    start = next(i for i, l in enumerate(text) if l.startswith("No.,Stock Code"))
    rows = [r for r in csv.reader(text[start + 1:]) if r]          # 每列結尾多一個逗號
    df = pd.DataFrame([r[1:4] for r in rows if len(r) >= 4], columns=["code", "vcm_name", "threshold"])
    df["code"] = pd.to_numeric(df["code"], errors="coerce")
    df["vcm_pct"] = df["threshold"].astype(str).str.extract(r"(\d+(?:\.\d+)?)")[0].astype(float)
    return df.dropna(subset=["code"]).astype({"code": int})[["code", "vcm_name", "vcm_pct"]]


def latest_vcm_url():
    """從 HKEX 網頁找出生效日 ≤ 今天的最新 VCM 名單 CSV"""
    html = fetch(VCM_PAGE).decode("utf-8", errors="ignore")
    today = f"{now_hkt():%Y%m%d}"
    found = re.findall(r"(VCM_list(\d{8})_csv(?:_p)?\.csv)", html)
    files = sorted({(d, f) for f, d in found if d <= today})
    if not files:
        raise RuntimeError("VCM 網頁找不到生效中的名單連結")
    d, f = files[-1]
    return VCM_BASE + f, d


def main():
    old = CONFIG / "universe.csv"
    try:
        sec = parse_securities(fetch(LIST_URL))
        url, eff = latest_vcm_url()
        vcm = parse_vcm(fetch(url))
    except Exception as e:
        log.error(f"更新失敗，沿用舊名單：{e}")
        return
    uni = sec.merge(vcm, on="code", how="outer")          # VCM 名單中的 ETF 也一併納入
    uni["name"] = uni["name"].fillna(uni["vcm_name"])
    uni["board"] = uni["board"].fillna("ETP")
    if SETTINGS.get("exclude_rmb_counter", True):
        uni = uni[~uni["code"].between(80000, 89999)]
    uni["yahoo"] = uni["code"].map(to_yahoo)
    uni = uni.sort_values("code")[["code", "yahoo", "name", "board", "vcm_pct"]]
    uni.to_csv(old, index=False, encoding="utf-8-sig")
    log.info(f"universe 更新完成：{len(uni)} 檔，其中 VCM {uni['vcm_pct'].notna().sum()} 檔（VCM 生效日 {eff}）")


if __name__ == "__main__":
    main()
