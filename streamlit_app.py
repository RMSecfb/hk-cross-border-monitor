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
              "value": "數值", "detail": "說明", "note": "備註", "found_at": "發現批次"}

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


def data_until(alerts_df, snap_df, day_str):
    """行情資料截止時間（HH:MM）。
    快照檔名帶的是程式執行時點，非行情時間，兩者可能差很多（例如補跑），
    故一律以實際行情時間為準：快照的 last_time → 警報最後時間 → status。"""
    for df, col in ((snap_df, "last_time"), (alerts_df, "time")):
        if not df.empty and col in df.columns:
            t = pd.to_datetime(df[col], errors="coerce").max()
            if pd.notna(t):
                return f"{t:%H:%M}"
    return status.get("data_until", "-") if status.get("trade_date") == day_str else "-"


until = data_until(alerts_all, snap, day)

# ---------- 標題與指標 ----------
st.title(f"港股交易監控　{day}")
c = st.columns(5)
c[0].metric("取得行情檔數",
            f"{len(snap):,} / {status.get('stocks_total', len(snap)):,}" if not snap.empty else "-",
            help="分母為監控名單全數檔數，分子為當次實際取得行情之檔數。")
c[1].metric("資料截至", until, help="行情最後更新時間（Yahoo 延遲約 15 分鐘），非程式執行時間。")
# 已有盤後預警列表時以盤後結果為準（每檔每項監控一列）；否則用盤中累計警報
day_rep = read_csv(DATA / "report" / f"eod_report_{ymd}.csv")
if not day_rep.empty:
    cnt, src = day_rep["等級"].value_counts(), "盤後"
else:
    cnt, src = (alerts_all["level"].value_counts() if not alerts_all.empty else pd.Series(dtype=int)), "盤中"
for i, lv in enumerate(LEVELS):
    c[i + 2].metric(f"{LEVEL_LABEL[lv]}（{src}）", int(cnt.get(lv, 0)))

# ---------- 監控摘要 ----------
cfg_rules = yaml.safe_load((ROOT / "config" / "rules.yaml").read_text(encoding="utf-8"))


def _rule_summary(alerts_df, snap_df, is_eod, rep_day_str, until_str=""):
    """規則生成監控摘要（無需 API）。
    盤中以行情資料截止時間為準，不取現在時間——報告可能於盤後才下載。"""
    time_str = rep_day_str if is_eod else (until_str or "-")
    src_str = "盤後報告" if is_eod else "盤中監控"

    if alerts_df.empty and snap_df.empty:
        return f"【{src_str}】{time_str} — 尚無資料。"

    lines = []
    if not alerts_df.empty:
        df = alerts_df
        red = int((df["level"] == "紅").sum()) if "level" in df.columns else 0
        orn = int((df["level"] == "橙").sum()) if "level" in df.columns else 0
        yel = int((df["level"] == "黃").sum()) if "level" in df.columns else 0
        stocks = df["code"].nunique() if "code" in df.columns else 0
        total = red + orn + yel
        lines.append(f"截至 {time_str}，共 **{stocks}** 檔觸發警報（{total} 筆）：紅燈 {red} 筆、橙燈 {orn} 筆、黃燈 {yel} 筆。")

        # 規則分布
        if "rule" in df.columns:
            top_rules = df.groupby("rule").size().nlargest(3)
            rule_str = "、".join([f"{r}（{n} 筆）" for r, n in top_rules.items()])
            lines.append(f"觸發最多的規則：{rule_str}。")

        # 高風險標的
        if "code" in df.columns and "level" in df.columns and "name" in df.columns:
            red_stocks = df[df["level"] == "紅"][["code", "name"]].drop_duplicates().head(5)
            if not red_stocks.empty:
                top_str = "、".join([f"{row['name']}（{int(row['code']):05d}）" for _, row in red_stocks.iterrows()])
                lines.append(f"紅燈標的（前5）：{top_str}。")
    elif not snap_df.empty:
        lines.append(f"截至 {time_str}，共監控 **{len(snap_df):,}** 檔，尚無觸發警報。")

    # 盤中額外：市場概況
    if not is_eod and not snap_df.empty and "chg_pct" in snap_df.columns:
        up = int((snap_df["chg_pct"] > 0).sum())
        dn = int((snap_df["chg_pct"] < 0).sum())
        flat = len(snap_df) - up - dn
        lines.append(f"市場概況：上漲 {up} 檔、下跌 {dn} 檔、平盤 {flat} 檔。")

    return "\n\n".join(lines)


GROQ_API = "https://api.groq.com/openai/v1"
# 偏好順序；Groq 會不定期下架模型，實際使用的是 /models 回報中第一個對得上的
GROQ_PREFER = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b",
               "openai/gpt-oss-20b", "llama-3.1-8b-instant"]
# 非對話用途（語音、分類、TTS），自動挑選時排除
GROQ_SKIP = ("whisper", "prompt-guard", "orpheus", "tts", "guard", "embed")


@st.cache_data(ttl=3600, show_spinner=False)
def groq_models(groq_key):
    """問 Groq 現在有哪些模型可用（快取 1 小時）。回傳 (模型清單, 錯誤訊息)"""
    import requests as req
    try:
        r = req.get(f"{GROQ_API}/models", headers={"Authorization": f"Bearer {groq_key}"}, timeout=20)
        if r.status_code != 200:
            return [], f"{r.status_code} {r.text[:200]}"
        return [m["id"] for m in r.json().get("data", [])], ""
    except Exception as e:
        return [], str(e)


def pick_groq_model(groq_key):
    """選一個可用的對話模型：先照偏好順序，再退而求其次挑任一非語音類模型"""
    avail, err = groq_models(groq_key)
    if not avail:
        return None, err or "無法取得模型清單"
    for m in GROQ_PREFER:
        if m in avail:
            return m, ""
    chat = [m for m in avail if not any(s in m.lower() for s in GROQ_SKIP)]
    if chat:
        return chat[0], ""
    return None, f"帳號可用的模型中沒有對話模型：{', '.join(avail[:8])}"


AI_MAX_TOKENS = 450      # 輸出上限；輸入約 700–900 tokens，合計每次按鈕約 1,500 tokens


def _groq_payload(model, prompt):
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": AI_MAX_TOKENS, "temperature": 0.3}
    if model.startswith("openai/gpt-oss"):   # 推理模型：思考也算輸出 tokens，調低避免內容被截斷
        body["reasoning_effort"] = "low"
        body["max_tokens"] = AI_MAX_TOKENS + 450
    return body


def _fmt_code(c):
    try:
        return f"{int(c):05d}"
    except (TypeError, ValueError):
        return str(c)


def _ai_facts(alerts_df, snap_df):
    """整理給 AI 的資料（精簡文字，控制 token 用量）"""
    lines = []
    df = alerts_df
    if not df.empty and "level" in df.columns:
        cnt = df["level"].value_counts()
        n_codes = df["code"].nunique() if "code" in df.columns else 0
        lines.append(f"警報筆數：紅{int(cnt.get('紅', 0))}、橙{int(cnt.get('橙', 0))}、黃{int(cnt.get('黃', 0))}；觸發檔數{n_codes}")
        if "rule" in df.columns:
            for lv in ("紅", "橙"):
                sub = df[df["level"] == lv]
                if not sub.empty:
                    top = sub.groupby("rule").size().nlargest(3)
                    lines.append(f"{lv}燈前三規則：" + "、".join(f"{r}{n}筆" for r, n in top.items()))
            if "detail" in df.columns:
                det = df.groupby("rule")["detail"].first()
                top_rules = df.groupby("rule").size().nlargest(4).index
                lines.append("數值意義：" + "；".join(f"{r}＝{det[r]}" for r in top_rules))
        if {"code", "name", "rule"} <= set(df.columns):
            g = df.groupby(["code", "name"]).agg(
                n=("rule", "size"), kinds=("rule", "nunique"),
                red=("level", lambda x: int((x == "紅").sum()))).reset_index()
            g = g.sort_values(["red", "kinds", "n"], ascending=False).head(6)
            lines.append("重點標的（紅燈筆數／觸發規則種類／總筆數）：" + "；".join(
                f"{r['name']}({_fmt_code(r['code'])}) 紅{r['red']}／{r['kinds']}種／{r['n']}筆" for _, r in g.iterrows()))
            multi = int((df.groupby("code")["rule"].nunique() >= 3).sum())
            lines.append(f"同時觸發3種以上規則的標的：{multi}檔")
    if not snap_df.empty and "chg_pct" in snap_df.columns:
        chg = snap_df["chg_pct"].dropna()
        up, dn = int((chg > 0).sum()), int((chg < 0).sum())
        lines.append(f"全市場：上漲{up}檔、下跌{dn}檔、平盤{len(chg) - up - dn}檔，漲跌幅中位數{chg.median():.2f}%")
    return "\n".join(lines) if lines else "（無警報資料）"


def _groq_summary(alerts_df, snap_df, is_eod, rep_day_str, groq_key, until_str=""):
    """用 Groq API 生成摘要（按鈕觸發）"""
    try:
        import requests as req
        time_str = rep_day_str if is_eod else (until_str or "-")
        src_str = "盤後報告" if is_eod else "盤中監控"

        facts = _ai_facts(alerts_df, snap_df)

        prompt = f"""你是港股跨境交易監控系統的風險分析師。請只根據下列{src_str}資料，以繁體中文寫一段約 250 字的監控摘要，依序說明：(1) 異常概況與嚴重程度；(2) 風險集中在哪些規則與標的；(3) 建議優先關注的事項。
要求：只使用資料中出現的數字與名稱，不要推測原因、不要編造資料中沒有的內容；語氣專業簡潔；直接輸出段落，不加標題或編號。

時間：{time_str}
{facts}"""

        model, err = pick_groq_model(groq_key)
        if not model:
            raise RuntimeError(f"取不到可用模型（{err}）")

        resp = req.post(
            f"{GROQ_API}/chat/completions",
            headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
            json=_groq_payload(model, prompt),
            timeout=30
        )
        if resp.status_code != 200:
            # Groq 會把真正的原因放在 body（例如模型已下架、額度用盡、金鑰無效）
            raise RuntimeError(f"HTTP {resp.status_code}・模型 {model}・{resp.text[:300]}")
        return resp.json()["choices"][0]["message"]["content"].strip(), model
    except Exception as e:
        return f"（AI 摘要失敗：{e}）\n\n" + _rule_summary(alerts_df, snap_df, is_eod, rep_day_str, until_str), None


AI_DIR = DATA / "ai_summary"      # 每個資料時點一檔：data/ai_summary/YYYYMMDD/HHMM.json（盤後為 eod.json）


def _ai_label(key):
    return "盤後報告" if key == "eod" else (f"資料至 {key[:2]}:{key[2:]}" if len(key) == 4 and key.isdigit() else key)


def load_ai(day, key):
    """讀已存的 AI 分析；先看本次工作階段剛存的（GitHub 檔案要 1–2 分鐘才會同步到頁面）"""
    j = st.session_state.get(f"ai_saved_{day}_{key}")
    if j:
        return j
    f = AI_DIR / day / f"{key}.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    except Exception:
        return None


def list_ai(day):
    keys = {f.stem for f in (AI_DIR / day).glob("*.json")} if (AI_DIR / day).exists() else set()
    keys |= {k[len(f"ai_saved_{day}_"):] for k in st.session_state if k.startswith(f"ai_saved_{day}_")}
    return [(k, load_ai(day, k)) for k in sorted(keys) if load_ai(day, k)]


def save_ai(day, key, text, model):
    """存回 GitHub；同一資料時點再分析會覆蓋（提交紀錄保留舊版）"""
    meta = {"day": day, "key": key, "text": text, "model": model,
            "created_at": f"{dt.datetime.now(HKT):%Y-%m-%d %H:%M}"}
    path = f"data/ai_summary/{day}/{key}.json"
    _, sha = gh.get_file(path)
    gh.put_file(path, json.dumps(meta, ensure_ascii=False, indent=1), sha, f"AI 分析 {day} {_ai_label(key)}")
    st.session_state[f"ai_saved_{day}_{key}"] = meta
    return meta


def summary_panel(alerts_df, snap_df, is_eod=False, rep_day_str="", day_key="", until_str="",
                  ai_day="", ai_key=""):
    """監控摘要區塊（表格上方）。回傳目前顯示的摘要文字，供匯出報告沿用。
    盤中與盤後各自獨立存放，切換頁籤不會互相覆蓋。
    ai_day／ai_key：AI 分析的存檔位置（交易日、資料時點）；每個資料時點只留最後一次。"""
    try:                               # 未設定 secrets.toml 時 st.secrets 會丟例外
        groq_key = st.secrets.get("GROQ_API_KEY", "") or ""
    except Exception:
        groq_key = ""

    kind = "eod" if is_eod else "intraday"
    k_txt, k_ai, k_day, k_model = (f"sum_txt_{kind}", f"sum_ai_{kind}",
                                   f"sum_day_{kind}", f"sum_model_{kind}")
    k_meta, k_warn = f"sum_meta_{kind}", f"sum_warn_{kind}"
    label = "盤後監控摘要" if is_eod else "盤中監控摘要"
    can_store = bool(ai_day and ai_key and ai_key != "-")
    saved = load_ai(ai_day, ai_key) if can_store else None

    with st.container(border=True):
        st.markdown(f"**📋 {label}**")
        if st.session_state.get(k_day) != day_key:       # 換日期或資料時點：有存檔就顯示 AI 分析，否則規則摘要
            if saved:
                st.session_state[k_txt], st.session_state[k_ai] = saved["text"], True
                st.session_state[k_model], st.session_state[k_meta] = saved.get("model"), saved
            else:
                st.session_state[k_txt] = _rule_summary(alerts_df, snap_df, is_eod, rep_day_str, until_str)
                st.session_state[k_ai], st.session_state[k_meta] = False, None
            st.session_state[k_day] = day_key
            st.session_state[k_warn] = ""

        col1, col2 = st.columns([5, 1])
        with col1:
            st.markdown(st.session_state.get(k_txt, ""))
            if st.session_state.get(k_ai):
                meta = st.session_state.get(k_meta)
                when = f"・{_ai_label(ai_key)}・產生於 {meta['created_at']}" if meta and can_store else ""
                st.caption(f"🤖 由 Groq AI 生成（模型 {st.session_state.get(k_model, '-')}）{when}")
            if st.session_state.get(k_warn):
                st.warning(st.session_state[k_warn])
        with col2:
            again = bool(saved) or bool(st.session_state.get(k_ai))
            if groq_key and st.button("🔄 重新分析" if again else "🤖 AI 分析", key=f"ai_btn_{kind}",
                                      help="使用 Groq AI 生成深度分析摘要；同一資料時點只保留最後一次"):
                with st.spinner("AI 分析中…"):
                    txt_ai, used = _groq_summary(alerts_df, snap_df, is_eod, rep_day_str, groq_key, until_str)
                st.session_state[k_txt] = txt_ai
                st.session_state[k_ai] = bool(used)
                st.session_state[k_model] = used
                st.session_state[k_meta], st.session_state[k_warn] = None, ""
                if used and can_store:
                    if gh.ready:
                        try:
                            st.session_state[k_meta] = save_ai(ai_day, ai_key, txt_ai, used)
                        except Exception as e:
                            st.session_state[k_warn] = f"AI 分析已產生，但儲存失敗：{e}"
                    else:
                        st.session_state[k_warn] = "尚未設定 GITHUB_TOKEN，這次 AI 分析沒有儲存。"
                st.rerun()
            if st.session_state.get(k_ai) and st.button("↩ 規則摘要", key=f"rule_btn_{kind}"):
                st.session_state[k_txt] = _rule_summary(alerts_df, snap_df, is_eod, rep_day_str, until_str)
                st.session_state[k_ai] = False
                st.rerun()

        if can_store:                                      # 回頭查：當日其他時點的 AI 分析
            others = [(k, j) for k, j in list_ai(ai_day) if k != ai_key]
            if others:
                with st.expander(f"當日其他時點的 AI 分析（{len(others)}）"):
                    keys = [k for k, _ in others]
                    pick = st.selectbox("資料時點", keys, index=len(keys) - 1, format_func=_ai_label,
                                        key=f"ai_pick_{kind}")
                    j = dict(others)[pick]
                    st.markdown(j["text"])
                    st.caption(f"🤖 模型 {j.get('model', '-')}・產生於 {j.get('created_at', '-')}")
    return st.session_state.get(k_txt, "")


# ---------- 報告匯出（盤中、盤後共用）----------
LEVEL_ORDER = {"紅": 0, "橙": 1, "黃": 2}
PDF_LEVELS = ["紅", "橙"]     # 盤中 PDF 只列紅、橙；黃燈量大，留在 Excel


def rule_pivot(df, idx, val):
    """監控類型 × 等級 的筆數表，附合計並由多到少排序"""
    if df.empty:
        return pd.DataFrame()
    p = df.pivot_table(index=idx, columns="等級", values=val, aggfunc="count", fill_value=0)
    for lv in LEVELS:
        if lv not in p.columns:
            p[lv] = 0
    p["合計"] = p[LEVELS].sum(axis=1)
    return p.sort_values("合計", ascending=False)


def intraday_meta(alerts_df, snap_df, day_str, summary_text, until_label, run_at="", total=None):
    """盤中報告：把累計警報整理成中文欄位的明細表"""
    d = alerts_df.copy()
    det = pd.DataFrame({
        "時間": pd.to_datetime(d["time"]).dt.strftime("%H:%M:%S"),
        "等級": d["level"],
        "股票代碼": d["code"].map(lambda c: f"{int(c):05d}"),
        "股票名稱": d["name"],
        "監控類型": d["rule"],
        "指標值": pd.to_numeric(d["value"], errors="coerce").round(2).astype(str),
        "說明": d["detail"] if "detail" in d.columns else "",
        "備註": d["note"] if "note" in d.columns else "",
    })
    det = det.sort_values(["等級", "時間"], key=lambda s: s.map(LEVEL_ORDER) if s.name == "等級" else s,
                          ascending=[True, False])
    ec = d["level"].value_counts()
    got = int(len(snap_df))
    stats = [("報告日期", day_str), ("資料截至", until_label)]
    if run_at:
        stats.append(("程式執行時間", run_at))
    stats += [("取得行情檔數", f"{got:,} / {total:,}" if total else f"{got:,}"),
              ("觸發檔數", int(d["code"].nunique())), ("警報筆數", int(len(d)))]
    stats += [(f"{lv}燈", int(ec.get(lv, 0))) for lv in LEVELS]
    n_hi = int(det["等級"].isin(PDF_LEVELS).sum())
    return dict(title="港股跨境交易監控盤中報告", day_label=f"{day_str}　資料截至 {until_label}",
                summary_text=summary_text.replace("**", ""), stats=stats,
                rule_pivot=rule_pivot(det, "監控類型", "股票代碼"), detail=det,
                detail_note=f"PDF 只列紅燈與橙燈共 {n_hi:,} 列中的前 200 列；"
                            f"含黃燈的完整明細共 {len(det):,} 列，請看 Excel。",
                pdf_levels=PDF_LEVELS, pdf_rows=200)


def eod_meta(rep, day_str, summary_text):
    """盤後報告：直接沿用預警列表的欄位與排序"""
    ec = rep["等級"].value_counts()
    sd = cfg_rules.get("streak_days", 3)
    stats = [("報告日期", day_str), ("預警檔數（去重）", int(rep["股票代碼"].nunique())),
             ("預警總筆數", int(len(rep)))]
    stats += [(f"{lv}燈", int(ec.get(lv, 0))) for lv in LEVELS]
    stats.append((f"連續上榜 ≥{sd} 日（檔）", int(rep.loc[rep["連續上榜(日)"] >= sd, "股票代碼"].nunique())))
    return dict(title="港股跨境交易監控日報（盤後）", day_label=day_str,
                summary_text=summary_text.replace("**", ""), stats=stats,
                rule_pivot=rule_pivot(rep, "監控類型", "預警ID"), detail=rep,
                detail_note=f"完整明細共 {len(rep):,} 列（風險分數由高到低），"
                            "PDF 顯示前 200 列的重點欄位；完整欄位與全部列請看 Excel。",
                pdf_cols=["股票代碼", "股票名稱", "等級", "監控類型", "指標值", "閾值",
                          "備註", "風險分數", "連續上榜(日)"],
                pdf_rows=200)


def download_panel(kind, day_key, meta_fn, n_rows):
    """產生 Excel 與 PDF 後提供下載。資料量大時 Excel 需要數秒，所以按鈕觸發而非每次重算。"""
    import report_export
    st.subheader("下載監控報告")
    slot = f"files_{kind}"
    cur = st.session_state.get(slot) or {}
    hint = "（約需 10～20 秒）" if n_rows > 5000 else ""
    c1, c2, c3 = st.columns([1.3, 1, 1])
    if c1.button(f"產生報告檔{hint}", key=f"gen_{kind}"):
        with st.spinner("產生報告中…"):
            try:
                meta = meta_fn()
                cur = {"day": day_key,
                       "xlsx": report_export.build_excel(meta),
                       "pdf": report_export.build_pdf(meta)}
                st.session_state[slot] = cur
            except Exception as e:
                st.error(f"產生失敗：{e}")
                cur = {}
    if cur.get("day") == day_key:
        stem = f"hk_monitor_{'eod' if kind == 'eod' else 'intraday'}_{day_key}"
        c2.download_button("📥 Excel", cur["xlsx"], f"{stem}.xlsx", key=f"dx_{kind}",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        c3.download_button("📄 PDF", cur["pdf"], f"{stem}.pdf", key=f"dp_{kind}",
                           mime="application/pdf")
        st.caption("報告內容以上方摘要開頭，接著統計、規則分布與明細表；"
                   "若先按過「AI 分析」，報告會帶入 AI 生成的摘要。")
    elif cur:
        st.caption("日期已變更，請重新產生報告檔。")


tab_alert, tab_vcm, tab_rank, tab_stock, tab_eod, tab_trend, tab_param = st.tabs(
    ["警報", "VCM 監控", "今日高風險排行", "個股查詢", "盤後報告", "趨勢追蹤", "參數"])

# ---------- 警報 ----------
with tab_alert:
    sum_intraday = summary_panel(alerts_all, snap, is_eod=False, rep_day_str=day, day_key=f"{ymd}_{until}",
                                 until_str=until, ai_day=ymd, ai_key=until.replace(":", ""))
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

        download_panel("intraday", ymd,
                       lambda: intraday_meta(alerts_all, snap, day, sum_intraday, until,
                                             status.get("run_at", "") if status.get("trade_date") == day else "",
                                             status.get("stocks_total")),
                       len(alerts_all))

# ---------- VCM 監控 ----------
with tab_vcm:
    if snap.empty:
        st.info("尚無盤中快照。")
    else:
        v = snap[snap["vcm_pct"].notna()].sort_values("vcm_usage_pct", ascending=False)
        st.caption("VCM 使用率 = 最近 5 分鐘漲跌幅 ÷ 該股 VCM 門檻。80% 以上為內部預警，100% 即達 VCM 觸發門檻（以 1 分鐘 K 線模擬）。已依使用率由大到小排序。")
        st.dataframe(
            v[["code", "name", "vcm_pct", "chg_5m_pct", "vcm_usage_pct", "price", "chg_pct", "last_time"]],
            hide_index=True, use_container_width=True, height=h(len(v)),
            column_config={
                "code": "代碼", "name": "名稱", "vcm_pct": st.column_config.NumberColumn("VCM 門檻 ±%", format="%.0f"),
                "chg_5m_pct": st.column_config.NumberColumn("5 分鐘漲跌 %", format="%.2f"),
                "vcm_usage_pct": st.column_config.ProgressColumn("VCM 使用率", format="%.0f%%", min_value=0, max_value=100),
                "price": "現價", "chg_pct": st.column_config.NumberColumn("漲跌幅 %", format="%.2f"),
                "last_time": st.column_config.DatetimeColumn("資料時間", format="HH:mm")})

# ---------- 今日高風險排行 ----------
with tab_rank:
    if snap.empty:
        st.info("尚無盤中快照。")
    else:
        st.caption("依盤中快照風險分數排行，顯示當日最需關注的標的（最近一次排程更新）。")
        # 讀今日盤中 score（若盤後已完成則用盤後）
        score_f_intraday = DATA / "report" / f"score_{ymd}.csv"
        if score_f_intraday.exists():
            score_today = pd.read_csv(score_f_intraday)
        else:
            # 即時估算：從快照 alerts 計算
            score_today = pd.DataFrame()

        min_score = st.number_input("風險分數 ≥", value=30, step=5, min_value=0, max_value=100)
        top_n = st.number_input("顯示前 N 檔", value=30, step=5, min_value=5, max_value=100)

        if not score_today.empty:
            hi_risk = (score_today[score_today["score"] >= min_score]
                       .sort_values("score", ascending=False)
                       .head(int(top_n)))
            # 補入即時價格
            if not snap.empty and "code" in snap.columns:
                price_map = snap.set_index("code")[["price", "chg_pct", "turnover_hkd"]].to_dict("index")
                hi_risk = hi_risk.copy()
                hi_risk["現價"] = hi_risk["code"].map(lambda c: price_map.get(c, {}).get("price"))
                hi_risk["漲跌幅%"] = hi_risk["code"].map(lambda c: price_map.get(c, {}).get("chg_pct"))
                hi_risk["成交額(萬)"] = hi_risk["code"].map(
                    lambda c: round(price_map.get(c, {}).get("turnover_hkd", 0) / 1e4, 0))
            hi_risk["代碼"] = hi_risk["code"].map(lambda c: f"{int(c):05d}")
            disp = hi_risk.rename(columns={"name": "名稱", "score": "風險分數", "rules": "觸發規則",
                                           "紅": "紅燈數", "橙": "橙燈數", "黃": "黃燈數"})
            cols_show = [c for c in ["代碼", "名稱", "風險分數", "紅燈數", "橙燈數", "黃燈數",
                                     "觸發規則", "現價", "漲跌幅%", "成交額(萬)"] if c in disp.columns]
            st.caption(f"共 {len(hi_risk)} 檔分數 ≥ {min_score}")
            sty = disp[cols_show].style.map(
                lambda v: ramp_css(v, min_score, 100, RED_RAMP), subset=["風險分數"])
            st.dataframe(sty.format({"風險分數": "{:.0f}", "現價": "{:.3f}",
                                     "漲跌幅%": "{:+.2f}", "成交額(萬)": "{:,.0f}"}, na_rep="-"),
                         hide_index=True, use_container_width=True, height=h(len(hi_risk), 600))
        else:
            # 若無 score 檔，退而求其次顯示漲跌幅排行
            st.info("盤後風險分數尚未產生，以下顯示即時漲幅排行（盤後批次完成後改為風險分數排行）。")
            min_to = st.number_input("只看成交額 ≥（港元）", value=1_000_000, step=1_000_000)
            s = snap[snap["turnover_hkd"] >= min_to]
            s = s.assign(turnover_m=(s["turnover_hkd"] / 1e6).round(1))
            cols = {"code": "代碼", "name": "名稱", "price": "現價", "chg_pct": "漲跌幅 %",
                    "vol_ratio": "量比", "turnover_m": "成交額（百萬）"}
            top20 = s.sort_values("chg_pct", ascending=False).head(20)[list(cols)].rename(columns=cols)
            st.dataframe(top20, hide_index=True, use_container_width=True)

# ---------- 個股查詢 ----------
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
        sms = cfg_rules.get("streak_min_score", cfg_rules.get("list_min_score", 30))

        # 摘要區塊
        rep_alerts_for_summary = pd.DataFrame()
        if not rep.empty:
            rep_alerts_for_summary = rep.rename(columns={"等級": "level", "股票代碼": "code", "股票名稱": "name",
                                                          "監控類型": "rule"})
        sum_eod = summary_panel(rep_alerts_for_summary, pd.DataFrame(), is_eod=True,
                                rep_day_str=fmt(rep_day), day_key=rep_day,
                                ai_day=rep_day, ai_key="eod")

        st.caption("收市後以全天 1 分鐘 K 線與收盤價重算全部規則（含收市競價異常）；每檔 × 每項監控取當日最高等級一列。"
                   f"「連續上榜」= 連續幾個交易日風險分數 ≥ {cfg_rules.get('list_min_score', 30)}，"
                   f"達 {sd} 日以上以深色標示。「連續異常標的」需分數 ≥ {sms}。盤中批次若有漏跑或失敗，以本報告為準。")
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

        download_panel("eod", rep_day,
                       lambda: eod_meta(rep, fmt(rep_day), sum_eod),
                       len(rep))

# ---------- 趨勢追蹤 ----------
with tab_trend:
    hist = score_history()
    if hist.empty:
        st.info("尚無盤後風險分數。每個交易日盤後批次完成後累積；也可用 Actions「補跑歷史盤後報告」補前幾天。")
    else:
        dates = sorted(hist["date"].unique())
        sd = cfg_rules.get("streak_days", 3)
        th = cfg_rules.get("list_min_score", 30)
        streak_th = cfg_rules.get("streak_min_score", 60)   # 連續異常標的門檻
        w = cfg_rules.get("score_weights", {})
        fmt_d = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}"
        latest, base = dates[-1], (dates[-2] if len(dates) > 1 else None)
        st.caption(f"資料範圍：最近 {len(dates)} 個交易日｜最新監控日：{fmt_d(latest)}｜比較基準日："
                   f"{fmt_d(base) if base else '-'}｜連續 {sd} 日區間：{' / '.join(fmt_d(d) for d in dates[-sd:][::-1])}")
        st.caption(f"風險分數 = 各監控項目當日最高等級加總（紅 {w.get('紅')}、橙 {w.get('橙')}、黃 {w.get('黃')}，上限 100）；"
                   f"分數 ≥ {th} 視為「上榜」；連續異常標的需分數 ≥ {streak_th}（至少紅+橙 或兩個紅）。")
        wide = hist.pivot_table(index="code", columns="date", values="score", aggfunc="first")
        names = hist.drop_duplicates("code", keep="last").set_index("code")["name"]
        rules_latest = hist[hist["date"] == latest].set_index("code")["rules"]

        st.subheader(f"連續異常標的（最近 {sd} 個交易日皆分數 ≥ {streak_th}）")
        if len(dates) < sd:
            st.info(f"目前只有 {len(dates)} 個交易日的資料，滿 {sd} 日後顯示。")
        else:
            lastn = dates[-sd:]
            # 使用 streak_th（60）而非 th（30）作為連續異常標的門檻
            hit = wide[lastn].fillna(0).ge(streak_th).all(axis=1)
            cont = wide.loc[hit, lastn[::-1]].copy()
            cont.columns = ["最新分數"] + [f"前{i}日" for i in range(1, sd)]
            cont["平均"] = cont.mean(axis=1).round(1)
            # 連續上榜天數仍用 th（30）計算
            on = wide.fillna(0).ge(th)
            cont["連續上榜(日)"] = [next((i for i, x in enumerate(on.loc[c, ::-1]) if not x), len(dates)) for c in cont.index]
            cont.insert(0, "名稱", names.reindex(cont.index))
            cont["最新觸發項目"] = rules_latest.reindex(cont.index)
            cont = cont.sort_values(["連續上榜(日)", "平均"], ascending=False).reset_index()
            cont["code"] = cont["code"].map(lambda c: f"{int(c):05d}")
            cont = cont.rename(columns={"code": "代碼"})
            score_cols = ["最新分數"] + [f"前{i}日" for i in range(1, sd)]
            st.caption(f"共 {len(cont)} 檔")
            st.dataframe(cont.style.map(lambda v: ramp_css(v, streak_th, 100, RED_RAMP), subset=score_cols)
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
    "streak_min_score": ("連續異常標的：分數門檻（進入連續異常列表）", "分"),
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
    import io as _io
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
        buf2 = _io.StringIO()
        ry.dump(doc, buf2)
        shas.append(gh.put_file("config/rules.yaml", buf2.getvalue(), sha, msg))
    if action_edits:
        text, sha = gh.get_file("config/actions.yaml")
        doc = ry.load(text)
        for grp, lv, v in action_edits:
            doc[grp][lv] = v
        buf2 = _io.StringIO()
        ry.dump(doc, buf2)
        shas.append(gh.put_file("config/actions.yaml", buf2.getvalue(), sha, msg))
    text, sha = gh.get_file("config/param_log.csv")
    if not text:
        text = "﻿時間,修改人,參數,原值,新值,原因\n"
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
    o_df = pd.DataFrame([{"鍵": k, "參數": d, "設定值": float(cfg.get(k, 0)), "單位": unit}
                         for k, (d, unit) in OTHER_INFO.items() if k in cfg])
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
