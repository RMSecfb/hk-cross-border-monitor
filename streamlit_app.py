"""港股跨境交易監控 — Streamlit 儀表板
讀取 repo 內 data/ 的盤中快照、警報與盤後報告（由 GitHub Actions 每 30 分鐘更新）。
"""
import json
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
import yaml

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LEVELS = ["紅", "橙", "黃"]
LEVEL_STYLE = {"紅": "background-color:#fbe3e4;color:#8f1d22;font-weight:600",
               "橙": "background-color:#fdecd9;color:#8a4500;font-weight:600",
               "黃": "background-color:#fbf3c9;color:#6b5600;font-weight:600"}
LEVEL_LABEL = {"紅": "▲ 紅 嚴重", "橙": "■ 橙 預警", "黃": "● 黃 關注"}

st.set_page_config(page_title="港股交易監控", layout="wide")


# ---------- 讀資料 ----------
@st.cache_data(ttl=120)
def read_status():
    f = DATA / "status.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


@st.cache_data(ttl=120)
def snapshot_files(day):
    return sorted((DATA / "snapshot" / day).glob("snapshot_*.parquet"))


@st.cache_data(ttl=120)
def read_parquet(f):
    return pd.read_parquet(f)


@st.cache_data(ttl=120)
def read_csv(f, **kw):
    f = Path(f)
    return pd.read_csv(f, **kw) if f.exists() else pd.DataFrame()


def trade_days():
    days = {p.name for p in (DATA / "snapshot").glob("????-??-??")}
    days |= {f"{p.stem[-8:-4]}-{p.stem[-4:-2]}-{p.stem[-2:]}" for p in (DATA / "report").glob("eod_alerts_*.csv")}
    return sorted(days, reverse=True)


def style_levels(df):
    view = df.copy()
    view["等級"] = view["level"].map(LEVEL_LABEL)
    return view


def level_styler(df, col="等級"):
    sty = df.style.map(lambda v: LEVEL_STYLE.get(str(v).split(" ")[1], "") if isinstance(v, str) and " " in v else "",
                       subset=[col])
    return sty.format({"數值": "{:.2f}"}) if "數值" in df.columns else sty


def h(n, cap=560):
    """表格高度：依筆數，最多 cap"""
    return min(cap, 38 + 35 * max(n, 1))


ALERT_COLS = {"time": "觸發時間", "code": "代碼", "name": "名稱", "等級": "等級", "rule": "規則",
              "value": "數值", "detail": "說明", "found_at": "發現批次"}

# ---------- 側欄 ----------
status = read_status()
days = trade_days()
with st.sidebar:
    st.header("港股交易監控")
    if not days:
        st.warning("尚無資料。請在 GitHub Actions 執行「盤後批次」（勾選初始化），再執行「盤中監控」。")
        st.stop()
    day = st.selectbox("交易日", days)
    st.caption(f"最近一次執行：{status.get('run_at', '-')}")
    st.caption(f"資料截至：{status.get('data_until', '-')}（Yahoo 延遲約 15 分鐘）")
    st.caption(f"取得檔數：{status.get('stocks_ok', '-')} / {status.get('stocks_total', '-')}")
    if st.button("重新整理"):
        st.cache_data.clear()
        st.rerun()

ymd = day.replace("-", "")
snaps = snapshot_files(day)
snap = read_parquet(snaps[-1]) if snaps else pd.DataFrame()
alerts = read_csv(DATA / "alerts" / f"alerts_{ymd}.csv")
eod = read_csv(DATA / "report" / f"eod_alerts_{ymd}.csv")
alerts_all = alerts if not alerts.empty else eod

# ---------- 標題與指標 ----------
st.title(f"港股交易監控　{day}")
c = st.columns(5)
c[0].metric("監控檔數", f"{len(snap):,}" if not snap.empty else "-")
c[1].metric("最新快照", snaps[-1].stem[-6:-4] + ":" + snaps[-1].stem[-4:-2] if snaps else "-")
cnt = alerts_all["level"].value_counts() if not alerts_all.empty else pd.Series(dtype=int)
for i, lv in enumerate(LEVELS):
    c[i + 2].metric(LEVEL_LABEL[lv], int(cnt.get(lv, 0)))

tab_alert, tab_vcm, tab_rank, tab_stock, tab_eod, tab_param = st.tabs(
    ["警報", "VCM 監控", "市場排行", "個股", "盤後報告", "參數"])

# ---------- 警報 ----------
with tab_alert:
    if alerts_all.empty:
        st.info("本日尚無警報。")
    else:
        f1, f2, f3 = st.columns([1, 2, 1])
        lv_sel = f1.multiselect("等級", LEVELS, default=LEVELS)
        rule_sel = f2.multiselect("規則", sorted(alerts_all["rule"].unique()))
        kw = f3.text_input("代碼或名稱")
        a = alerts_all[alerts_all["level"].isin(lv_sel)]
        if rule_sel:
            a = a[a["rule"].isin(rule_sel)]
        if kw:
            a = a[a["code"].astype(str).str.contains(kw) | a["name"].astype(str).str.contains(kw, case=False)]
        a = style_levels(a.sort_values("time", ascending=False))
        a = a[[k for k in ALERT_COLS if k in a.columns]].rename(columns=ALERT_COLS)
        st.caption(f"共 {len(a)} 筆")
        st.dataframe(level_styler(a), hide_index=True, use_container_width=True, height=h(len(a)))

        by_rule = alerts_all.pivot_table(index="rule", columns="level", values="code", aggfunc="count", fill_value=0)
        st.subheader("各規則警報數")
        st.dataframe(by_rule.reindex(columns=[l for l in LEVELS if l in by_rule.columns]), use_container_width=True)

# ---------- VCM 監控 ----------
with tab_vcm:
    if snap.empty:
        st.info("尚無盤中快照。")
    else:
        v = snap[snap["vcm_pct"].notna()].sort_values("vcm_usage_pct", ascending=False)
        st.caption("VCM 使用率 = 最近 5 分鐘漲跌幅 ÷ 該股 VCM 門檻。80% 以上為內部預警，100% 即達 VCM 觸發門檻（以 1 分鐘 K 線模擬）。")
        st.dataframe(
            v[["code", "name", "vcm_pct", "chg_5m_pct", "vcm_usage_pct", "price", "chg_pct", "last_time"]],
            hide_index=True, use_container_width=True, height=h(len(v)),
            column_config={
                "code": "代碼", "name": "名稱", "vcm_pct": st.column_config.NumberColumn("VCM 門檻 ±%", format="%.0f"),
                "chg_5m_pct": st.column_config.NumberColumn("5 分鐘漲跌 %", format="%.2f"),
                "vcm_usage_pct": st.column_config.ProgressColumn("VCM 使用率", format="%.0f%%", min_value=0, max_value=100),
                "price": "現價", "chg_pct": st.column_config.NumberColumn("漲跌幅 %", format="%.2f"),
                "last_time": st.column_config.DatetimeColumn("資料時間", format="HH:mm")})

# ---------- 市場排行 ----------
with tab_rank:
    if snap.empty:
        st.info("尚無盤中快照。")
    else:
        min_to = st.number_input("只看成交額 ≥（港元）", value=1_000_000, step=1_000_000)
        s = snap[snap["turnover_hkd"] >= min_to]
        cols = {"code": "代碼", "name": "名稱", "price": "現價", "chg_pct": "漲跌幅 %",
                "vol_ratio": "量比", "ma_dev_pct": "偏離 MA %", "turnover_m": "成交額（百萬）"}
        boards = [("漲幅前 20", "chg_pct", False), ("跌幅前 20", "chg_pct", True),
                  ("量比前 20", "vol_ratio", False), ("偏離均線前 20（絕對值）", "ma_dev_abs", False)]
        s = s.assign(ma_dev_abs=s["ma_dev_pct"].abs(), turnover_m=(s["turnover_hkd"] / 1e6).round(1))
        r1, r2 = st.columns(2), st.columns(2)
        for box, (title, key, asc) in zip([*r1, *r2], boards):
            box.subheader(title)
            t = s.sort_values(key, ascending=asc).head(20)[list(cols)].rename(columns=cols)
            box.dataframe(t, hide_index=True, use_container_width=True)

# ---------- 個股 ----------
with tab_stock:
    if snap.empty:
        st.info("尚無盤中快照。")
    else:
        opts = snap.sort_values("code").apply(lambda r: f"{r['code']} {r['name']}", axis=1).tolist()
        pick = st.selectbox("選擇股票", opts)
        code = int(pick.split(" ")[0])
        row = snap[snap["code"] == code].iloc[0]
        m = st.columns(5)
        m[0].metric("現價", row["price"], f"{row['chg_pct']:.2f}%" if pd.notna(row["chg_pct"]) else None)
        m[1].metric("量比", f"{row['vol_ratio']:.2f}" if pd.notna(row["vol_ratio"]) else "-")
        m[2].metric("偏離 MA", f"{row['ma_dev_pct']:.2f}%" if pd.notna(row["ma_dev_pct"]) else "-")
        m[3].metric("5 分鐘漲跌", f"{row['chg_5m_pct']:.2f}%" if pd.notna(row["chg_5m_pct"]) else "-")
        m[4].metric("VCM 門檻", f"±{row['vcm_pct']:.0f}%" if pd.notna(row["vcm_pct"]) else "不適用")

        hist = pd.concat([read_parquet(f).query("code == @code")[["last_time", "price"]] for f in snaps])
        st.subheader("各批次價格")
        hist = hist.drop_duplicates("last_time")
        chart = alt.Chart(hist).mark_line(point=alt.OverlayMarkDef(size=60), strokeWidth=2).encode(
            x=alt.X("last_time:T", title=None, axis=alt.Axis(format="%H:%M")),
            y=alt.Y("price:Q", title="價格（港元）", scale=alt.Scale(zero=False)),
            tooltip=[alt.Tooltip("last_time:T", title="時間", format="%H:%M"), alt.Tooltip("price:Q", title="價格")])
        st.altair_chart(chart.properties(height=260), use_container_width=True)
        if not alerts_all.empty:
            a = style_levels(alerts_all[alerts_all["code"] == code].sort_values("time", ascending=False))
            if not a.empty:
                a = a[[k for k in ALERT_COLS if k in a.columns]].rename(columns=ALERT_COLS)
                st.dataframe(level_styler(a), hide_index=True, use_container_width=True)

# ---------- 盤後報告 ----------
with tab_eod:
    if eod.empty:
        st.info("本日盤後報告尚未產生（每個交易日 16:42 後）。")
    else:
        st.caption("收市後以全天 1 分鐘 K 線與收盤價重算全部規則（含收市競價異常）。盤中批次若有漏跑或失敗，以本報告為準。")
        k = st.columns(4)
        k[0].metric("警報檔數", eod["code"].nunique())
        ec = eod["level"].value_counts()
        for i, lv in enumerate(LEVELS):
            k[i + 1].metric(LEVEL_LABEL[lv], int(ec.get(lv, 0)))
        summ = eod.pivot_table(index="rule", columns="level", values="code", aggfunc="count", fill_value=0)
        summ = summ.reindex(columns=[l for l in LEVELS if l in summ.columns])
        summ["合計"] = summ.sum(axis=1)
        st.subheader("各規則警報數")
        st.dataframe(summ.sort_values("合計", ascending=False).rename_axis("規則"), use_container_width=True)
        st.subheader("警報明細")
        rsel = st.multiselect("規則", sorted(eod["rule"].unique()), key="eod_rule")
        e = eod[eod["rule"].isin(rsel)] if rsel else eod
        e = e.assign(_o=e["level"].map({l: i for i, l in enumerate(LEVELS)})).sort_values(["_o", "time"])
        e = style_levels(e)
        e = e[[k for k in ALERT_COLS if k in e.columns]].rename(columns=ALERT_COLS)
        st.dataframe(level_styler(e), hide_index=True, use_container_width=True, height=h(len(e), 480))

# ---------- 參數 ----------
RULE_INFO = {  # 參數鍵: (監控項目, 單位, 適用範圍, Excel 出處)
    "day_change": ("即時漲跌幅", "%", "全市場", "價格異常"),
    "chg_1m": ("1 分鐘急漲急跌", "%", "全市場（當日成交額達門檻）", "價格異常"),
    "chg_5m": ("5 分鐘急漲急跌", "%", "全市場（當日成交額達門檻）", "價格異常"),
    "ma_dev": ("偏離移動平均", "%", "活躍股", "價格異常"),
    "open_gap": ("開盤價異常", "%", "全市場", "價格異常"),
    "cas_dev": ("收市競價異常", "%", "全市場（盤後）", "價格異常"),
    "vol_ratio": ("成交量暴增", "倍", "全市場", "成交量異常"),
    "vol_3m": ("3 分鐘成交量暴增", "倍", "活躍股", "成交量異常"),
    "vol_streak": ("連續放量", "倍/分鐘", "活躍股", "成交量異常"),
    "turnover": ("換手率異常", "%", "全市場（需股本資料）", "成交量異常"),
}
OTHER_INFO = {
    "ma_days": ("偏離均線使用的 MA 天數", "日"),
    "vol_avg_days": ("均量計算天數", "日"),
    "min_turnover_hkd": ("當日成交額低於此值不做短時規則", "港元"),
    "active_min_avg_turnover_hkd": ("活躍股門檻：20 日平均成交額", "港元"),
    "vol_streak_minutes": ("連續放量的連續分鐘數", "分鐘"),
    "min_profile_days": ("分鐘量基準至少累積天數", "交易日"),
    "vcm_early_ratio": ("VCM 內部預警比例（門檻的幾成）", "比例"),
}

with tab_param:
    cfg = yaml.safe_load((ROOT / "config" / "rules.yaml").read_text(encoding="utf-8"))
    st.caption("黃 = 關注（記錄並通知風控）、橙 = 預警（通知主管並調查）、紅 = 嚴重（即時升級）。"
               "修改請編輯 repo 的 config/rules.yaml，並登記於 Excel「參數調整日誌」。")
    st.subheader("異常交易監控閾值")
    cd = cfg.get("cooldown_min", {})
    rows = []
    for key, (name, unit, scope, src) in RULE_INFO.items():
        v = cfg.get(key, {})
        rule_name = {"day_change": "即時漲跌幅", "chg_1m": "1分鐘急漲急跌", "chg_5m": "5分鐘急漲急跌", "ma_dev": "偏離均線",
                     "open_gap": "開盤價異常", "cas_dev": "收市競價異常", "vol_ratio": "成交量暴增",
                     "vol_3m": "3分鐘量暴增", "vol_streak": "連續放量", "turnover": "換手率異常"}[key]
        rows.append({"類別": src, "監控項目": name, "黃": v.get("黃"), "橙": v.get("橙"), "紅": v.get("紅"),
                     "單位": unit, "適用範圍": scope, "冷卻（分）": cd.get(rule_name)})
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    st.subheader("VCM 參數")
    uni_f = ROOT / "config" / "universe.csv"
    if uni_f.exists():
        u = pd.read_csv(uni_f)
        vc = u["vcm_pct"].dropna().value_counts().sort_index()
        vrows = [{"觸發門檻": f"±{p:.0f}%", "內部預警": f"±{p * cfg['vcm_early_ratio']:.0f}%", "股票數": int(n)}
                 for p, n in vc.items()]
        st.dataframe(pd.DataFrame(vrows), hide_index=True, use_container_width=True)
    st.caption("門檻取自 HKEX 每次公布的 VCM 證券名單（每日盤後自動更新）。不適用時段：早、午市首 15 分鐘，"
               "午市最後 20 分鐘，開市前與收市競價時段；午市不沿用早市參考價；冷靜期 5 分鐘。")

    st.subheader("其他參數")
    orows = [{"參數": desc, "設定值": cfg.get(k), "單位": unit, "設定鍵": k} for k, (desc, unit) in OTHER_INFO.items()]
    st.dataframe(pd.DataFrame(orows), hide_index=True, use_container_width=True)
