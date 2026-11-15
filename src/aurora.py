"""오로라월드 미국 수입 B/L 월별 건수 → 전월 집계 완료 시 그래프 전송."""
import datetime as dt
import json
import sys

import requests

from charts import ACCENT, MUTED, plt
from common import (OUT_DIR, UA, html_escape, load_config, load_state,
                    notify_error_once, save_state, send_photo)


def fetch(url, referer):
    r = requests.get(url, headers={"User-Agent": UA, "Referer": referer,
                                   "X-Requested-With": "XMLHttpRequest",
                                   "Accept": "application/json"}, timeout=60)
    r.raise_for_status()
    rows = r.json()
    out = {}
    for row in rows:
        d = dt.datetime.utcfromtimestamp(row["date"] / 1000)
        out[f"{d.year}-{d.month:02d}"] = int(row["value"])
    return dict(sorted(out.items()))


def prev_month(today=None):
    today = today or dt.date.today()
    first = today.replace(day=1)
    last = first - dt.timedelta(days=1)
    return f"{last.year}-{last.month:02d}"


def shift(ym, months):
    y, m = map(int, ym.split("-"))
    idx = y * 12 + (m - 1) + months
    return f"{idx // 12}-{idx % 12 + 1:02d}"


def plot(series, target, title, path):
    keys = [k for k in series if k <= target]
    vals = [series[k] for k in keys]
    xs = [dt.date(int(k[:4]), int(k[5:]), 1) for k in keys]
    ma = [sum(vals[max(0, i - 11):i + 1]) / len(vals[max(0, i - 11):i + 1]) for i in range(len(vals))]

    fig, ax = plt.subplots(figsize=(11, 5))
    colors = [ACCENT if k == target else MUTED for k in keys]
    ax.bar(xs, vals, width=25, color=colors, linewidth=0)
    ax.plot(xs, ma, color="#1e3a8a", linewidth=1.6, label="12개월 이동평균")
    ax.annotate(f"{target}\n{series[target]}건", (xs[-1], vals[-1]), xytext=(-80, 45),
                textcoords="offset points", fontsize=10, color="#1e3a8a", fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="#c7d2fe"),
                arrowprops=dict(arrowstyle="-", color="#1e3a8a", lw=0.8))
    ax.set_title(title, loc="left", fontsize=14, fontweight="bold", color="#111827")
    ax.set_ylabel("월별 B/L 건수")
    ax.legend(frameon=False, loc="upper left")
    ax.grid(axis="x", visible=False)
    fig.text(0.01, 0.01, "출처: importinfo.com (미국 세관 B/L)", fontsize=8, color="#6b7280")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def caption(series, target, name, page_url):
    v = series[target]
    pm, py = series.get(shift(target, -1)), series.get(shift(target, -12))
    last12 = sum(series.get(shift(target, -i), 0) for i in range(12))
    prev12 = sum(series.get(shift(target, -i - 12), 0) for i in range(12))

    def pct(a, b):
        return f"{(a / b - 1) * 100:+.0f}%" if b else "n/a"
    lines = [f"📦 <b>{html_escape(name)}</b>", f"<b>{target}: {v}건</b>"]
    if pm is not None:
        lines.append(f"전월 {pm}건 대비 {pct(v, pm)}")
    if py is not None:
        lines.append(f"전년 동월 {py}건 대비 {pct(v, py)}")
    lines.append(f"최근 12개월 합계 {last12}건 (직전 12개월 대비 {pct(last12, prev12)})")
    lines.append(page_url)
    return "\n".join(lines)


def main():
    cfg = load_config()["aurora"]
    force = "--force" in sys.argv
    sample = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--sample=")), None)
    st = load_state("aurora")
    try:
        if sample:
            series = json.loads(open(sample, encoding="utf-8").read())
        else:
            series = fetch(cfg["data_url"], cfg["page_url"])
    except Exception as e:  # noqa: BLE001
        notify_error_once("오로라 수입 데이터", e)
        raise
    target = prev_month()
    if target not in series:
        target = [k for k in series if k < f"{dt.date.today():%Y-%m}"][-1]
    if st.get("last_sent") == target and not force:
        print(f"이미 보낸 달: {target}")
        return
    path = OUT_DIR / "aurora.png"
    plot(series, target, "오로라월드 미국 수입 B/L 월별 건수", path)
    send_photo(path, caption(series, target, cfg["name"], cfg["page_url"]))
    st.update(last_sent=target, series=series)
    save_state("aurora", st)


if __name__ == "__main__":
    main()
