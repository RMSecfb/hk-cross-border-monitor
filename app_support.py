"""Streamlit 儀表板輔助：GitHub API（寫回參數、啟動 workflow）與排程時間解析
需要 Streamlit secrets：
  GITHUB_TOKEN   fine-grained token，僅授權本 repo：Contents 讀寫、Actions 讀寫
  GITHUB_REPO    例如 "RMSecfb/hk-cross-border-monitor"
  GITHUB_BRANCH  選填，預設 "main"
  ADMIN_PASSWORD 修改參數、手動啟動排程時需輸入
"""
import base64
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

HKT = ZoneInfo("Asia/Hong_Kong")
UTC = dt.timezone.utc
API = "https://api.github.com"
ROOT = Path(__file__).resolve().parent


# ---------------- GitHub API ----------------
class GitHub:
    def __init__(self, secrets):
        self.token = secrets.get("GITHUB_TOKEN")
        self.repo = secrets.get("GITHUB_REPO")
        self.branch = secrets.get("GITHUB_BRANCH", "main")
        self.password = secrets.get("ADMIN_PASSWORD")

    @property
    def ready(self):
        return bool(self.token and self.repo)

    def _h(self):
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    def get_file(self, path):
        """回傳 (文字內容, sha)；檔案不存在回傳 ("", None)"""
        r = requests.get(f"{API}/repos/{self.repo}/contents/{path}", headers=self._h(),
                         params={"ref": self.branch}, timeout=20)
        if r.status_code == 404:
            return "", None
        r.raise_for_status()
        j = r.json()
        return base64.b64decode(j["content"]).decode("utf-8"), j["sha"]

    def put_file(self, path, text, sha, message):
        body = {"message": message, "content": base64.b64encode(text.encode("utf-8")).decode(),
                "branch": self.branch}
        if sha:
            body["sha"] = sha
        r = requests.put(f"{API}/repos/{self.repo}/contents/{path}", headers=self._h(), json=body, timeout=30)
        r.raise_for_status()
        return r.json()["commit"]["sha"]

    def dispatch(self, workflow_file, inputs=None):
        r = requests.post(f"{API}/repos/{self.repo}/actions/workflows/{workflow_file}/dispatches",
                          headers=self._h(), json={"ref": self.branch, "inputs": inputs or {}}, timeout=20)
        r.raise_for_status()

    def latest_runs(self, workflow_file, n=3):
        r = requests.get(f"{API}/repos/{self.repo}/actions/workflows/{workflow_file}/runs",
                         headers=self._h(), params={"per_page": n}, timeout=20)
        r.raise_for_status()
        return r.json().get("workflow_runs", [])


# ---------------- 排程（讀 repo 內 workflow 檔，GitHub 上改了排程，頁面隨之同步）----------------
def _field(expr, lo, hi):
    vals = set()
    for part in expr.split(","):
        step = 1
        if "/" in part:
            part, step = part.split("/")
            step = int(step)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = map(int, part.split("-"))
        else:
            a = b = int(part)
        vals.update(range(a, b + 1, step))
    return vals


def _match(expr, t):
    """t 為 UTC datetime；支援 * , - / 的五欄 cron"""
    m, h, dom, mon, dow = expr.split()
    return (t.minute in _field(m, 0, 59) and t.hour in _field(h, 0, 23)
            and t.day in _field(dom, 1, 31) and t.month in _field(mon, 1, 12)
            and (t.isoweekday() % 7) in {d % 7 for d in _field(dow, 0, 7)})


def workflow_crons(name):
    f = ROOT / ".github" / "workflows" / name
    if not f.exists():
        return []
    y = yaml.safe_load(f.read_text(encoding="utf-8"))
    on = y.get("on", y.get(True, {})) or {}          # PyYAML 會把 on 讀成 True
    return [s["cron"] for s in on.get("schedule", []) or []]


def schedule_times(crons, start, end):
    """列出 [start, end) 期間（HKT）所有排程時間"""
    out, t = [], start.astimezone(UTC).replace(second=0, microsecond=0)
    end = end.astimezone(UTC)
    exprs = [c.strip() for c in crons]
    hours = set()
    for c in exprs:
        hours |= _field(c.split()[1], 0, 23)
    while t < end:
        if t.hour in hours and any(_match(c, t) for c in exprs):
            out.append(t.astimezone(HKT))
        t += dt.timedelta(minutes=1)
    return out


def schedule_status(now=None):
    """回傳 dict：今日盤中/盤後排程時間、前次與下次排程"""
    now = now or dt.datetime.now(HKT)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    intraday = workflow_crons("intraday.yml")
    eod = workflow_crons("eod.yml")
    today_i = schedule_times(intraday, day0, day0 + dt.timedelta(days=1))
    today_e = schedule_times(eod, day0, day0 + dt.timedelta(days=1))
    window = schedule_times(intraday + eod, now - dt.timedelta(days=4), now + dt.timedelta(days=4))
    prev = max((t for t in window if t <= now), default=None)
    nxt = min((t for t in window if t > now), default=None)
    return {"now": now, "intraday": today_i, "eod": today_e, "prev": prev, "next": nxt,
            "eod_set": set(schedule_times(eod, now - dt.timedelta(days=4), now + dt.timedelta(days=4)))}


def ago(t, now):
    """距今多久（中文）"""
    mins = int((now - t).total_seconds() // 60)
    if mins < 60:
        return f"{mins} 分鐘"
    h, m = divmod(mins, 60)
    if h < 48:
        return f"{h} 小時 {m} 分"
    return f"{h // 24} 天"
