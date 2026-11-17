"""미국 ISM 제조업 PMI / 일본 공작기계 수주 — 발표 기간에 확인해서, 새 달 수치가 나오면 한 번만 전송.

  📈 미국 ISM 제조업 PMI — 2026년 10월
  55.5 (MoM +1.0p, YoY +6.8p)

  📈 일본 공작기계 수주 — 2026년 10월
  2,043.8억엔 (MoM -8.4%, YoY +42.5%)

Trading Economics는 GitHub 서버 접속을 막아서(403), 공식 발표를 다룬 뉴스(구글 뉴스)에서
AI가 수치만 뽑고, 이전 달·전년 같은 달 값은 저장해 둔 기록(state/macro.json)으로 계산.
기록에 없으면 그 달 발표 기사를 다시 찾아 채움.
"""
import datetime as dt
import json
import sys

from common import html_escape, load_config, load_state, notify_error_once, save_state, send_message
from news import ask_claude_json, google_news, llm_available

MONTH_EN = ["January", "February", "March", "April", "May", "June", "July", "August",
            "September", "October", "November", "December"]


def shift(ym, n):
    y, m = map(int, ym.split("-"))
    k = y * 12 + m - 1 + n
    return f"{k // 12}-{k % 12 + 1:02d}"


def articles(queries, hours):
    out = []
    for q, lang in queries:
        try:
            out += [{"title": i["title"], "source": i["source"], "snippet": i["desc"][:300],
                     "date": i["date"].date().isoformat() if i["date"] else "", "link": i["link"]}
                    for i in google_news(q, lang, hours=hours)[:12]]
        except Exception as e:  # noqa: BLE001
            print(f"{q}: {e}", file=sys.stderr)
    return out


def extract(ind, arts, want=None):
    """기사에서 수치 뽑기. want='YYYY-MM'이면 그 달 값만."""
    target = f"{want} 값만 찾아라." if want else "가장 최근 발표된 달의 값을 찾아라."
    prompt = (f"다음은 '{ind['name']}' 관련 기사 제목·요약이다. {target}\n"
              f"단위: {ind['unit_rule']}\n"
              "기사에 명시된 숫자만 써라. 추측 금지. 없으면 null.\n"
              '출력: {"month": "YYYY-MM", "value": 숫자, "prev_month_value": 숫자 또는 null, '
              '"yoy_pct": 기사에 나온 전년 대비 증감률(%) 또는 null, "source_idx": 근거 기사 번호}\n\n'
              + json.dumps(list(enumerate(arts)), ensure_ascii=False))
    return ask_claude_json(prompt, max_tokens=400)


def value_for(ind, ym, hist):
    """기록에 없는 달 값은 그 달 발표 기사를 찾아 채움."""
    if ym in hist:
        return hist[ym]
    y, m = ym.split("-")
    arts = articles([(q.format(month=MONTH_EN[int(m) - 1], year=y, m=int(m)), lang)
                     for q, lang in ind["lookup_queries"]], hours=None)
    if not arts:
        return None
    try:
        r = extract(ind, arts, want=ym)
        if r.get("month") == ym and r.get("value") is not None:
            hist[ym] = float(r["value"])
            return hist[ym]
    except Exception as e:  # noqa: BLE001
        print(f"{ind['name']} {ym} 값 찾기 실패: {e}", file=sys.stderr)
    return None


def fmt(ind, v):
    return f"{v:,.1f}{ind['suffix']}"


def change(ind, cur, base, given_pct=None):
    if ind["change"] == "point":
        return f"{cur - base:+.1f}p" if base is not None else "-"
    if given_pct is not None:
        return f"{float(given_pct):+.1f}%"
    return f"{(cur / base - 1) * 100:+.1f}%" if base else "-"


def main():
    force = "--force" in sys.argv
    only = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")), None)
    cfg = [i for i in load_config()["macro"] if not only or i["key"] == only]
    st = load_state("macro")
    if not llm_available():
        print("ANTHROPIC_API_KEY가 없어 수치를 뽑을 수 없음")
        return
    failed = False
    for ind in cfg:
        key = ind["key"]
        rec = st.setdefault(key, {})
        hist = rec.setdefault("history", {})
        for k, v in (ind.get("seed") or {}).items():
            hist.setdefault(str(k), float(v))
        arts = articles([(q, lang) for q, lang in ind["queries"]], hours=24 * 10)
        try:
            r = extract(ind, arts) if arts else None
        except Exception as e:  # noqa: BLE001
            r = None
            print(f"{ind['name']} 추출 실패: {e}", file=sys.stderr)
        if not r or not r.get("month") or r.get("value") is None:
            print(f"{ind['name']}: 아직 새 발표 기사 없음")
            continue
        ym, val = r["month"], float(r["value"])
        if r.get("prev_month_value") is not None:
            hist.setdefault(shift(ym, -1), float(r["prev_month_value"]))
        hist[ym] = val
        if rec.get("last_sent") == ym and not force:
            print(f"{ind['name']}: {ym}은 이미 보냄")
            save_state("macro", st)
            continue
        prev = value_for(ind, shift(ym, -1), hist)
        last_year = value_for(ind, shift(ym, -12), hist)
        mom = change(ind, val, prev)
        yoy = change(ind, val, last_year, r.get("yoy_pct") if ind["change"] == "pct" else None)
        y, m = ym.split("-")
        src = arts[r["source_idx"]]["link"] if isinstance(r.get("source_idx"), int) and r["source_idx"] < len(arts) else ""
        msg = (f"📈 <b>{html_escape(ind['name'])}</b> — {y}년 {int(m)}월\n"
               f"{fmt(ind, val)} (MoM {mom}, YoY {yoy})\n"
               + (f'<a href="{html_escape(src)}">기사</a> · ' if src else "")
               + f'<a href="{ind["url"]}">차트</a>')
        send_message(msg)
        rec["last_sent"] = ym
        save_state("macro", st)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        notify_error_once("ISM·공작기계", e)
        raise
