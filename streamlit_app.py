"""港股跨境交易監控 — Streamlit 儀表板
讀取 repo 內 data/ 的盤中快照、警報與盤後報告（由 GitHub Actions 每 30 分鐘更新）。
"""
import datetime as dt
import json
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
import yaml

from app_support import HKT, GitHub, ago, schedule_status

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
try:
    gh = GitHub(dict(st.secrets))
except Exception:                     # 本機沒有 secrets.toml
    gh = GitHub({})


@st.fragment(run_every=60)
def clock_panel():
    """現在時間、前次/下次排程（每分鐘自動更新）。排程時間直接讀 repo 內 workflow 檔，GitHub 上改排程後頁面同步。"""
    s = schedule_status()
    now = s["now"]
    st.markdown(f"**現在　{now:%m/%d %H:%M}**（台北/香港）")
    run_at = status.get("run_at")
    if run_at:
        ra = dt.datetime.fromisoformat(run_at).replace(tzinfo=HKT)
        st.caption(f"最近一次盤中抓取：{ra:%m/%d %H:%M}（{ago(ra, now)}前）")
        st.caption(f"資料截至 {status.get('data_until', '-')}（Yahoo 延遲約 15 分鐘）；"
                   f"取得 {status.get('stocks_ok', '-')} / {status.get('stocks_total', '-')} 檔")
    if s["prev"]:
        kind = "盤後" if s["prev"] in s["eod_set"] else "盤中"
        st.caption(f"前次排程：{s['prev']:%m/%d %H:%M}（{kind}，已過 {ago(s['prev'], now)}）")
    if s["next"]:
        kind = "盤後" if s["next"] in s["eod_set"] else "盤中"
        st.caption(f"下次排程：{s['next']:%m/%d %H:%M}（{kind}，還有 {ago(now, s['next']).lstrip('-')}）")
    with st.expander("今日排程"):
        if not s["intraday"] and not s["eod"]:
            st.caption("今日無排程（週末）")
        rows = [{"時間": f"{t:%H:%M}", "類型": "盤中", "狀態": "已過" if t <= now else "待執行"} for t in s["intraday"]]
        rows += [{"時間": f"{t:%H:%M}", "類型": "盤後", "狀態": "已過" if t <= now else "待執行"} for t in s["eod"]]
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        st.caption("GitHub 排程可能延遲數分鐘到數十分鐘。排程時間取自 repo 的 .github/workflows，修改後此處自動同步。")


def manual_run_panel():
    st.markdown("**手動抓取盤中資料**")
    if not gh.ready:
        st.caption("尚未設定 GITHUB_TOKEN，無法從頁面啟動。")
        return
    try:
        runs = gh.latest_runs("intraday.yml", 1)
    except Exception as e:
        runs = []
        st.caption(f"讀取執行狀態失敗：{e}")
    if runs:
        r = runs[0]
        t = dt.datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).astimezone(HKT)
        state = {"queued": "排隊中", "in_progress": "執行中", "completed": "完成"}.get(r["status"], r["status"])
        result = {"success": "成功", "failure": "失敗", "cancelled": "已取消"}.get(r.get("conclusion") or "", "")
        st.caption(f"最近一次：{t:%m/%d %H:%M} {state}{('・' + result) if result else ''}"
                   f"（{'排程' if r['event'] == 'schedule' else '手動'}）")
        busy = r["status"] in ("queued", "in_progress")
    else:
        busy = False
    force = st.checkbox("非交易時段也執行（測試用）", key="force_run")
    pw = st.text_input("管理密碼", type="password", key="run_pw")
    if st.button("立即抓取", disabled=busy, help="約 7 分鐘完成；完成後資料存回 GitHub，頁面約 1–2 分鐘內更新"):
        if gh.password and pw != gh.password:
            st.error("密碼錯誤")
        else:
            try:
                gh.dispatch("intraday.yml", {"force": "true" if force else "false"})
                st.success("已送出，約 7 分鐘後完成。")
            except Exception as e:
                st.error(f"啟動失敗：{e}")
    if busy:
        st.caption("目前已有一次執行中，完成後才能再啟動。")


with st.sidebar:
    st.header("港股交易監控")
    if not days:
        st.warning("尚無資料。請在 GitHub Actions 執行「盤後批次」（勾選初始化），再執行「盤中監控」。")
        st.stop()
    day = st.selectbox("交易日（盤中頁面）", days)
    clock_panel()
    st.divider()
    manual_run_panel()
    st.divider()
    if st.button("重新整理資料"):
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
# 已有盤後預警列表時以盤後結果為準（每檔每項監控一列）；否則用盤中累計警報
day_rep = read_csv(DATA / "report" / f"eod_report_{ymd}.csv")
if not day_rep.empty:
    cnt, src = day_rep["等級"].value_counts(), "盤後"
else:
    cnt, src = (alerts_all["level"].value_counts() if not alerts_all.empty else pd.Series(dtype=int)), "盤中"
for i, lv in enumerate(LEVELS):
    c[i + 2].metric(f"{LEVEL_LABEL[lv]}（{src}）", int(cnt.get(lv, 0)))

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
    rep_days = sorted((p.stem[-8:] for p in (DATA / "report").glob("eod_report_*.csv")), reverse=True)
    if not rep_days:
        st.info("尚無盤後報告（每個交易日 16:42 後產生；也可用 Actions「補跑歷史盤後報告」補前幾天）。")
        rep = pd.DataFrame()
    else:
        fmt = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}"
        default = rep_days.index(ymd) if ymd in rep_days else 0
        rc1, rc2 = st.columns([1, 3])
        rep_day = rc1.selectbox("報告日期", rep_days, index=default, format_func=fmt, key="rep_day")
        rc2.caption(f"共 {len(rep_days)} 個交易日的報告（{fmt(rep_days[-1])} ～ {fmt(rep_days[0])}），可回溯查詢。")
        rep = read_csv(DATA / "report" / f"eod_report_{rep_day}.csv", dtype={"股票代碼": str})
    if rep.empty:
        pass
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

RULE_NAME = {"day_change": "即時漲跌幅", "chg_1m": "1分鐘急漲急跌", "chg_5m": "5分鐘急漲急跌", "ma_dev": "偏離均線",
             "open_gap": "開盤價異常", "cas_dev": "收市競價異常", "vol_ratio": "成交量暴增",
             "vol_3m": "3分鐘量暴增", "vol_streak": "連續放量", "turnover": "換手率異常"}
EXTRA_COOLDOWN = ["VCM觸發(模擬)", "VCM內部預警"]


def num(v):
    """整數就存整數"""
    return int(v) if float(v).is_integer() else float(v)


def save_params(changes, rule_edits, other_edits, cd_edits, action_edits, who, why):
    """把修改寫回 GitHub：config/rules.yaml、config/actions.yaml（保留註解），並追加 config/param_log.csv"""
    from ruamel.yaml import YAML
    ry = YAML()
    ry.preserve_quotes = True
    ry.width = 4096
    import io
    stamp = dt.datetime.now(HKT)
    msg = f"參數調整（{who}）：{why}"
    shas = []
    if rule_edits or other_edits or cd_edits:
        text, sha = gh.get_file("config/rules.yaml")
        doc = ry.load(text)
        for key, lv, v in rule_edits:
            doc[key][lv] = num(v)
        for key, v in other_edits:
            doc[key] = num(v)
        for rule, v in cd_edits:
            doc["cooldown_min"][rule] = num(v)
        buf = io.StringIO()
        ry.dump(doc, buf)
        shas.append(gh.put_file("config/rules.yaml", buf.getvalue(), sha, msg))
    if action_edits:
        text, sha = gh.get_file("config/actions.yaml")
        doc = ry.load(text)
        for grp, lv, v in action_edits:
            doc[grp][lv] = v
        buf = io.StringIO()
        ry.dump(doc, buf)
        shas.append(gh.put_file("config/actions.yaml", buf.getvalue(), sha, msg))
    text, sha = gh.get_file("config/param_log.csv")
    if not text:
        text = "\ufeff時間,修改人,參數,原值,新值,原因\n"
    for item, old, new in changes:
        text += f"{stamp:%Y-%m-%d %H:%M},{who},{item},{old},{new},{why.replace(',', '，')}\n"
    gh.put_file("config/param_log.csv", text, sha, msg + "（紀錄）")
    return shas


with tab_param:
    cfg = yaml.safe_load((ROOT / "config" / "rules.yaml").read_text(encoding="utf-8"))
    acts = yaml.safe_load((ROOT / "config" / "actions.yaml").read_text(encoding="utf-8"))
    cd = cfg.get("cooldown_min", {})
    st.caption("黃 = 關注（記錄並通知風控）、橙 = 預警（通知主管並調查）、紅 = 嚴重（即時升級）。"
               "表格中欄名旁有筆形圖示的欄位可直接修改，修改後在最下方填寫修改人與原因，按「儲存到 GitHub」。"
               "下一次排程執行即套用新參數，頁面約 1–2 分鐘後更新。")

    st.subheader("異常交易監控閾值")
    rows = []
    for key, (name, unit, scope, src) in RULE_INFO.items():
        v = cfg.get(key, {})
        rows.append({"鍵": key, "類別": src, "監控項目": name, "黃": v.get("黃"), "橙": v.get("橙"), "紅": v.get("紅"),
                     "單位": unit, "適用範圍": scope, "冷卻（分）": cd.get(RULE_NAME[key])})
    th_df = pd.DataFrame(rows)
    th_new = st.data_editor(th_df, hide_index=True, use_container_width=True, key="ed_th",
                            disabled=["鍵", "類別", "監控項目", "單位", "適用範圍"],
                            column_config={"鍵": None, "黃": st.column_config.NumberColumn(min_value=0),
                                           "橙": st.column_config.NumberColumn(min_value=0),
                                           "紅": st.column_config.NumberColumn(min_value=0),
                                           "冷卻（分）": st.column_config.NumberColumn(min_value=0, step=1)})

    st.subheader("VCM 參數")
    uni_f = ROOT / "config" / "universe.csv"
    if uni_f.exists():
        u = pd.read_csv(uni_f)
        vc = u["vcm_pct"].dropna().value_counts().sort_index()
        vrows = [{"觸發門檻": f"±{p:.0f}%", "內部預警": f"±{p * cfg['vcm_early_ratio']:.0f}%", "股票數": int(n)}
                 for p, n in vc.items()]
        st.dataframe(pd.DataFrame(vrows), hide_index=True, use_container_width=True)
    st.caption("觸發門檻取自 HKEX 每次公布的 VCM 證券名單（每日盤後自動更新，不在此修改）；內部預警比例與冷卻時間可在下表修改。"
               "不適用時段：早、午市首 15 分鐘，午市最後 20 分鐘，開市前與收市競價時段；午市不沿用早市參考價；冷靜期 5 分鐘。")
    vcm_cd = pd.DataFrame([{"規則": r, "冷卻（分）": cd.get(r)} for r in EXTRA_COOLDOWN])
    vcm_cd_new = st.data_editor(vcm_cd, hide_index=True, use_container_width=True, key="ed_vcmcd", disabled=["規則"])

    st.subheader("其他參數")
    o_df = pd.DataFrame([{"鍵": k, "參數": d, "設定值": float(cfg.get(k)), "單位": unit} for k, (d, unit) in OTHER_INFO.items()])
    o_new = st.data_editor(o_df, hide_index=True, use_container_width=True, key="ed_other",
                           disabled=["鍵", "參數", "單位"], column_config={"鍵": None})

    st.subheader("操作建議（盤後預警列表）")
    a_df = pd.DataFrame([{"類別": g, "適用規則": "、".join(v["rules"]), "黃": v.get("黃"), "橙": v.get("橙"), "紅": v.get("紅")}
                         for g, v in acts.items()])
    a_new = st.data_editor(a_df, hide_index=True, use_container_width=True, key="ed_act", disabled=["類別", "適用規則"])

    # ---- 比對差異 ----
    changes, rule_edits, other_edits, cd_edits, action_edits, errors = [], [], [], [], [], []
    for (_, o), (_, n) in zip(th_df.iterrows(), th_new.iterrows()):
        for lv in ("黃", "橙", "紅"):
            if pd.notna(n[lv]) and float(n[lv]) != float(o[lv]):
                rule_edits.append((o["鍵"], lv, n[lv]))
                changes.append((f"{o['監控項目']}・{lv}", o[lv], num(n[lv])))
        if not (float(n["黃"]) <= float(n["橙"]) <= float(n["紅"])):
            errors.append(f"{o['監控項目']}：需 黃 ≤ 橙 ≤ 紅")
        if pd.notna(n["冷卻（分）"]) and float(n["冷卻（分）"]) != float(o["冷卻（分）"]):
            cd_edits.append((RULE_NAME[o["鍵"]], n["冷卻（分）"]))
            changes.append((f"{o['監控項目']}・冷卻", o["冷卻（分）"], num(n["冷卻（分）"])))
    for (_, o), (_, n) in zip(vcm_cd.iterrows(), vcm_cd_new.iterrows()):
        if pd.notna(n["冷卻（分）"]) and float(n["冷卻（分）"]) != float(o["冷卻（分）"]):
            cd_edits.append((o["規則"], n["冷卻（分）"]))
            changes.append((f"{o['規則']}・冷卻", o["冷卻（分）"], num(n["冷卻（分）"])))
    for (_, o), (_, n) in zip(o_df.iterrows(), o_new.iterrows()):
        if pd.notna(n["設定值"]) and float(n["設定值"]) != float(o["設定值"]):
            other_edits.append((o["鍵"], n["設定值"]))
            changes.append((o["參數"], num(o["設定值"]), num(n["設定值"])))
    for (_, o), (_, n) in zip(a_df.iterrows(), a_new.iterrows()):
        for lv in ("黃", "橙", "紅"):
            if str(n[lv]) != str(o[lv]):
                action_edits.append((o["類別"], lv, str(n[lv])))
                changes.append((f"操作建議・{o['類別']}・{lv}", o[lv], n[lv]))

    st.subheader("儲存修改")
    if not changes:
        st.caption("目前沒有修改。")
    else:
        st.dataframe(pd.DataFrame(changes, columns=["參數", "原值", "新值"]), hide_index=True, use_container_width=True)
        for e in errors:
            st.error(e)
        if not gh.ready:
            st.warning("尚未設定 GITHUB_TOKEN，無法寫回 GitHub。請在 Streamlit 的 Secrets 設定（見 README）。")
        else:
            c1, c2, c3 = st.columns([1, 2, 1])
            who = c1.text_input("修改人")
            why = c2.text_input("修改原因")
            pw = c3.text_input("管理密碼", type="password", key="param_pw")
            if st.button("儲存到 GitHub", type="primary", disabled=bool(errors)):
                if not who or not why:
                    st.error("請填寫修改人與修改原因")
                elif gh.password and pw != gh.password:
                    st.error("密碼錯誤")
                else:
                    try:
                        save_params(changes, rule_edits, other_edits, cd_edits, action_edits, who, why)
                        st.success("已寫回 GitHub。下一次排程執行即套用；頁面約 1–2 分鐘後顯示新值。")
                    except Exception as e:
                        st.error(f"寫回失敗：{e}")

    log_f = ROOT / "config" / "param_log.csv"
    if log_f.exists():
        st.subheader("參數調整紀錄")
        lg = pd.read_csv(log_f)
        st.dataframe(lg.iloc[::-1], hide_index=True, use_container_width=True, height=h(len(lg), 300))
