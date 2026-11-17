"""매일 06:00 — 지난 24시간 동안 관심 인물들이 AI·주식시장에 대해 의견을 낸 기사 링크 (없으면 안 보냄)."""
import datetime as dt
import json
import re
import sys

from common import html_escape, load_config, load_state, save_state
from news import (ask_claude_json, dedupe, fetch_rss, google_news, link,
                  llm_available, send_long)

SYSTEM = ("너는 투자 리서치 비서다. 뉴스 제목·요약만 보고, 해당 인물 '본인'이 직접 밝힌 의견을 다룬 기사만 고른다. "
          "추측하지 말고, 본인 발언이 아닌 기사(회사 실적, 주변 인물 이야기, "
          "단순 언급)는 제외한다.")


def collect(person, hours=26):
    items = []
    for q in person.get("queries", []):
        try:
            items += google_news(q["q"], q.get("lang", "en"), hours)
        except Exception as e:  # noqa: BLE001
            print(f"{person['name']} {q}: {e}", file=sys.stderr)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    for f in person.get("feeds", []):
        try:
            items += [i for i in fetch_rss(f) if not i["date"] or i["date"] >= cutoff]
        except Exception as e:  # noqa: BLE001
            print(f"{person['name']} {f}: {e}", file=sys.stderr)
    return dedupe(items)[:40]


def pick_with_llm(bundle, topic_hint, already):
    """API 키가 있을 때: 본인 발언 기사만 고르고, 이미 보낸 발언과 같은 내용은 뺌 (요약은 보내지 않음)."""
    payload = [{"person": name, "articles": [
        {"id": k, "title": i["title"], "source": i["source"], "snippet": i["desc"][:200]}
        for k, i in enumerate(items)]} for name, items in bundle.items() if items]
    prompt = (f"인물별 지난 24시간 뉴스 목록이다. 각 인물 '본인'이 '{topic_hint}'을(를) 직접 밝힌 기사만 골라라.\n"
              "규칙: 1) 본인이 직접 말하거나 쓴 내용(인터뷰·연설·서한·SNS·메모 인용)만. 남이 그 사람에 대해 쓴 해석·회사 실적·동명이인은 제외.\n"
              "2) 같은 발언을 다룬 기사는 하나만.\n"
              "3) 아래 '이미 보낸 발언'과 같은 발언(같은 자리에서 나온 같은 주장)을 다룬 기사는 날짜가 달라도 제외. "
              "새로운 발언이거나 같은 주제라도 새 내용이 추가된 경우만 포함.\n"
              f"[이미 보낸 발언]\n{json.dumps(already, ensure_ascii=False)}\n\n"
              '출력: [{"person": 이름, "ids": [기사 id 최대 3개], "remark": "고른 발언을 한 줄로 (중복 확인용, 메시지에는 안 나감)"}] '
              "— 해당 기사가 없는 인물은 빼라.\n\n" + json.dumps(payload, ensure_ascii=False))
    return ask_claude_json(prompt, SYSTEM, max_tokens=1500)


def _key(title):
    return re.sub(r"\W+", "", title.lower())[:60]


def main():
    cfg = load_config()["people"]
    st = load_state("people")
    cutoff = (dt.date.today() - dt.timedelta(days=14)).isoformat()
    sent = [x for x in st.get("sent", []) if x.get("date", "") >= cutoff]  # 최근 2주 보낸 발언·기사
    sent_titles = {x.get("key") for x in sent}
    bundle = {}
    for p in cfg["list"]:  # 이미 보낸 기사는 처음부터 제외
        bundle[p["name"]] = [i for i in collect(p) if _key(i["title"]) not in sent_titles]
    if sum(len(v) for v in bundle.values()) == 0:
        print("새 기사 없음")
        return

    chosen, remarks = None, {}
    if llm_available():
        try:
            already = [{"person": x["person"], "remark": x.get("remark", ""), "date": x["date"]} for x in sent if x.get("remark")]
            chosen = {}
            for r in pick_with_llm(bundle, cfg["topic_hint"], already):
                name = r.get("person")
                if name in bundle:
                    chosen[name] = [bundle[name][i] for i in r.get("ids", []) if isinstance(i, int) and 0 <= i < len(bundle[name])]
                    remarks[name] = r.get("remark", "")
        except Exception as e:  # noqa: BLE001
            print(f"AI 선별 실패, 키워드로 대신 고름: {e}", file=sys.stderr)
            chosen = None
    if chosen is None:
        # 영어 단어는 단어 단위로 비교 ('ai'가 'said' 안에서 걸리지 않도록)
        pats = [re.compile(r"\b" + re.escape(k.lower()) + r"\b") if k.isascii() else re.compile(re.escape(k))
                for k in cfg["fallback_keywords"]]
        chosen = {name: [i for i in items if any(p.search(i["title"].lower()) for p in pats)][:3]
                  for name, items in bundle.items()}

    blocks = [f"<b>{html_escape(name)}</b>\n" + "\n".join(
        f"  · {link(i['title'][:110], i['link'])} ({html_escape(i['source'])})" for i in items)
        for name, items in chosen.items() if items]
    if not blocks:
        print("관련 발언 없음 → 메시지 안 보냄")
        return
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%m/%d")
    send_long(f"🗣 <b>투자 거물 발언</b> ({today}, 지난 24시간)\n\n" + "\n\n".join(blocks))
    for name, items in chosen.items():
        for i in items:
            sent.append({"date": dt.date.today().isoformat(), "person": name, "key": _key(i["title"]),
                         "remark": remarks.get(name, "")})
    st["sent"] = sent
    save_state("people", st)


if __name__ == "__main__":
    main()
