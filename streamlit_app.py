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


RED_RAMP = ["#fdecea", "#f9c4bd", "#f08c80", "#d9473a", "#8f1d14"]
GREEN_RAMP = ["#e9f5ec", "#c3e6cb", "#7cc492", "#2f9a52", "#11622f"]


def ramp_css(v, lo, hi, ramp):
    """單一色相由淺到深；深色兩階改白字以維持對比"""
    if pd.isna(v) or hi <= lo:
        return ""
    i = int(min(max((v - lo) / (hi - lo), 0), 0.999) * len(ramp))
    return f"background-color:{ramp[i]};color:{'#ffffff' if i >= 3 else '#1f1f1f'}"


@st.cache_data(ttl=120)
def score_history(n=30):
    """近 n 個交易日風險分數 → 長表 date, code, name, score, rules"""
    files = sorted((DATA / "report").glob("score_*.csv"))[-n:]
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_csv(f).assign(date=f.stem[-8:]) for f in files], ignore_index=True)


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

tab_alert, tab_vcm, tab_rank, tab_stock, tab_eod, tab_trend, tab_param = st.tabs(
    ["警報", "VCM 監控", "市場排行", "個股", "盤後報告", "趨勢追蹤", "參數"])

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
cfg_rules = yaml.safe_load((ROOT / "config" / "rules.yaml").read_text(encoding="utf-8"))
with tab_eod:
    rep = read_csv(DATA / "report" / f"eod_report_{ymd}.csv", dtype={"股票代碼": str})
    if rep.empty:
        st.info("本日盤後報告尚未產生（每個交易日 16:42 後）。")
    else:
        sd = cfg_rules.get("streak_days", 3)
        st.caption("收市後以全天 1 分鐘 K 線與收盤價重算全部規則（含收市競價異常）；每檔 × 每項監控取當日最高等級一列。"
                   f"「連續上榜」= 連續幾個交易日風險分數 ≥ {cfg_rules.get('list_min_score', 30)}，"
                   f"達 {sd} 日以上以深色標示。盤中批次若有漏跑或失敗，以本報告為準。")
        k = st.columns(5)
        k[0].metric("預警檔數", rep["股票代碼"].nunique())
        ec = rep["等級"].value_counts()
        for i, lv in enumerate(LEVELS):
            k[i + 1].metric(LEVEL_LABEL[lv], int(ec.get(lv, 0)))
        k[4].metric(f"連續上榜 ≥{sd} 日", rep.loc[rep["連續上榜(日)"] >= sd, "股票代碼"].nunique())

        f1, f2, f3, f4 = st.columns([1, 2, 1, 1])
        lv_sel = f1.multiselect("等級", LEVELS, default=LEVELS, key="rep_lv")
        type_sel = f2.multiselect("監控類型", sorted(rep["監控類型"].unique()), key="rep_type")
        min_streak = f3.number_input("連續上榜 ≥（日）", min_value=0, value=0, step=1)
        kw = f4.text_input("代碼或名稱", key="rep_kw")
        r = rep[rep["等級"].isin(lv_sel) & (rep["連續上榜(日)"] >= min_streak)]
        if type_sel:
            r = r[r["監控類型"].isin(type_sel)]
        if kw:
            r = r[r["股票代碼"].str.contains(kw) | r["股票名稱"].astype(str).str.contains(kw, case=False)]
        st.subheader(f"預警列表（{len(r)} 列）")
        sty = (r.style
               .map(lambda v: LEVEL_STYLE.get(v, ""), subset=["等級"])
               .map(lambda v: "background-color:#8f1d14;color:#ffffff;font-weight:600" if v >= sd
                    else ("background-color:#f9c4bd" if v >= 2 else ""), subset=["連續上榜(日)"])
               )
        st.dataframe(sty, hide_index=True, use_container_width=True, height=h(len(r), 600), column_config={
            "預警ID": st.column_config.TextColumn(width="small"), "時間": st.column_config.TextColumn(width="small"),
            "等級": st.column_config.TextColumn(width="small"), "股票代碼": st.column_config.TextColumn("代碼", width="small"),
            "當日觸發次數": st.column_config.NumberColumn("觸發次數", width="small"),
            "風險分數": st.column_config.NumberColumn("分數", width="small", format="%d"),
            "連續上榜(日)": st.column_config.NumberColumn("連續上榜", width="small"),
            "近5日上榜(日)": st.column_config.NumberColumn("近5日上榜", width="small")})

        with st.expander("各規則警報數"):
            summ = rep.pivot_table(index="監控類型", columns="等級", values="預警ID", aggfunc="count", fill_value=0)
            summ = summ.reindex(columns=[l for l in LEVELS if l in summ.columns])
            summ["合計"] = summ.sum(axis=1)
            st.dataframe(summ.sort_values("合計", ascending=False), use_container_width=True)

# ---------- 趨勢追蹤 ----------
with tab_trend:
    hist = score_history()
    if hist.empty:
        st.info("尚無盤後風險分數。每個交易日盤後批次完成後累積；也可用 Actions「補跑歷史盤後報告」補前幾天。")
    else:
        dates = sorted(hist["date"].unique())
        sd = cfg_rules.get("streak_days", 3)
        th = cfg_rules.get("list_min_score", 30)
        w = cfg_rules.get("score_weights", {})
        fmt_d = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}"
        latest, base = dates[-1], (dates[-2] if len(dates) > 1 else None)
        st.caption(f"資料範圍：最近 {len(dates)} 個交易日｜最新監控日：{fmt_d(latest)}｜比較基準日："
                   f"{fmt_d(base) if base else '-'}｜連續 {sd} 日區間：{' / '.join(fmt_d(d) for d in dates[-sd:][::-1])}")
        st.caption(f"風險分數 = 各監控項目當日最高等級加總（紅 {w.get('紅')}、橙 {w.get('橙')}、黃 {w.get('黃')}，上限 100）；"
                   f"分數 ≥ {th} 視為上榜。")
        wide = hist.pivot_table(index="code", columns="date", values="score", aggfunc="first")
        names = hist.drop_duplicates("code", keep="last").set_index("code")["name"]
        rules_latest = hist[hist["date"] == latest].set_index("code")["rules"]

        st.subheader(f"連續異常標的（最近 {sd} 個交易日皆 ≥ {th}）")
        if len(dates) < sd:
            st.info(f"目前只有 {len(dates)} 個交易日的資料，滿 {sd} 日後顯示。")
        else:
            lastn = dates[-sd:]
            hit = wide[lastn].fillna(0).ge(th).all(axis=1)
            cont = wide.loc[hit, lastn[::-1]].copy()
            cont.columns = ["最新分數"] + [f"前{i}日" for i in range(1, sd)]
            cont["平均"] = cont.mean(axis=1).round(1)
            on = wide.fillna(0).ge(th)
            cont["連續上榜(日)"] = [next((i for i, x in enumerate(on.loc[c, ::-1]) if not x), len(dates)) for c in cont.index]
            cont.insert(0, "名稱", names.reindex(cont.index))
            cont["最新觸發項目"] = rules_latest.reindex(cont.index)
            cont = cont.sort_values(["連續上榜(日)", "平均"], ascending=False).reset_index()
            cont["code"] = cont["code"].map(lambda c: f"{int(c):05d}")
            cont = cont.rename(columns={"code": "代碼"})
            score_cols = ["最新分數"] + [f"前{i}日" for i in range(1, sd)]
            st.caption(f"共 {len(cont)} 檔")
            st.dataframe(cont.style.map(lambda v: ramp_css(v, th, 100, RED_RAMP), subset=score_cols)
                         .format({c: "{:.0f}" for c in score_cols} | {"平均": "{:.1f}"}),
                         hide_index=True, use_container_width=True, height=h(len(cont), 480))

        st.subheader("風險變化排行")
        if base is None:
            st.info("需要至少兩個交易日的資料。")
        else:
            st.caption(f"個股風險分數在最新監控日（{fmt_d(latest)}）與比較基準日（{fmt_d(base)}）之間的變化。")
            chg = pd.DataFrame({"名稱": names.reindex(wide.index),
                                "分數變化": wide[latest].fillna(0) - wide[base].fillna(0),
                                "最新分數": wide[latest].fillna(0)}).rename_axis("代碼").reset_index()
            chg["代碼"] = chg["代碼"].map(lambda c: f"{int(c):05d}")
            up, dn = st.columns(2)
            top_up = chg[chg["分數變化"] > 0].nlargest(10, "分數變化")
            top_dn = chg[chg["分數變化"] < 0].nsmallest(10, "分數變化")
            up.markdown("**風險上升 TOP 10**")
            up.dataframe(top_up.style.map(lambda v: ramp_css(v, 0, 100, RED_RAMP), subset=["分數變化"])
                         .format({"分數變化": "{:+.0f}", "最新分數": "{:.0f}"}), hide_index=True, use_container_width=True)
            dn.markdown("**風險下降 TOP 10**")
            dn.dataframe(top_dn.style.map(lambda v: ramp_css(-v, 0, 100, GREEN_RAMP), subset=["分數變化"])
                         .format({"分數變化": "{:+.0f}", "最新分數": "{:.0f}"}), hide_index=True, use_container_width=True)

        st.subheader(f"近 {min(10, len(dates))} 日上榜次數最多")
        recent = dates[-10:]
        freq = pd.DataFrame({"名稱": names.reindex(wide.index),
                             "上榜天數": wide[recent].fillna(0).ge(th).sum(axis=1),
                             "平均分數": wide[recent].mean(axis=1).round(1),
                             "最新分數": wide[latest]}).rename_axis("代碼").reset_index()
        freq = freq[freq["上榜天數"] >= 2].sort_values(["上榜天數", "平均分數"], ascending=False).head(20)
        freq["代碼"] = freq["代碼"].map(lambda c: f"{int(c):05d}")
        st.dataframe(freq.style.format({"平均分數": "{:.1f}", "最新分數": "{:.0f}"}, na_rep="-"),
                     hide_index=True, use_container_width=True)

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
    "list_min_score": ("趨勢追蹤：風險分數達此值視為上榜", "分"),
    "streak_days": ("趨勢追蹤：連續異常標的的連續天數", "交易日"),
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
