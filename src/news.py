"""구글 뉴스 RSS, 일반 RSS 수집과 Claude API 호출 공통 함수."""
import datetime as dt
import email.utils
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests

from common import UA, html_escape, send_message


def _parse_date(s):
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except Exception:  # noqa: BLE001
        pass
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _strip(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def fetch_rss(url, timeout=30):
    """RSS/Atom → [{title, link, date(datetime|None), desc, source}]"""
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for it in root.iter("item"):
        src = it.find("source")
        out.append({
            "title": (it.findtext("title") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
            "date": _parse_date(it.findtext("pubDate")),
            "desc": _strip(it.findtext("description"))[:400],
            "source": (src.text if src is not None else "") or "",
        })
    ns = "{http://www.w3.org/2005/Atom}"
    for it in root.iter(f"{ns}entry"):
        link = it.find(f"{ns}link")
        out.append({
            "title": (it.findtext(f"{ns}title") or "").strip(),
            "link": link.get("href") if link is not None else "",
            "date": _parse_date(it.findtext(f"{ns}updated") or it.findtext(f"{ns}published")),
            "desc": _strip(it.findtext(f"{ns}summary") or it.findtext(f"{ns}content"))[:400],
            "source": "",
        })
    return out


def google_news(query, lang="en", hours=26):
    """구글 뉴스 검색 RSS. 최근 hours 시간 이내 기사만."""
    if lang == "ko":
        hl, gl, ceid = "ko", "KR", "KR:ko"
    else:
        hl, gl, ceid = "en-US", "US", "US:en"
    q = urllib.parse.quote(f"{query} when:{max(1, round(hours / 24))}d")
    url = f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"
    items = fetch_rss(url)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    res = []
    for i in items:
        if i["date"] and i["date"] < cutoff:
            continue
        # 제목 끝의 ' - 매체명' 분리
        m = re.match(r"^(.*)\s+-\s+([^-]+)$", i["title"])
        if m and not i["source"]:
            i["title"], i["source"] = m.group(1).strip(), m.group(2).strip()
        elif m and m.group(2).strip() == i["source"].strip():
            i["title"] = m.group(1).strip()
        res.append(i)
    return res


def dedupe(items):
    seen, out = set(), []
    for i in items:
        k = re.sub(r"\W+", "", i["title"].lower())[:80]
        if k and k not in seen:
            seen.add(k)
            out.append(i)
    return out


# ---------- Claude API ----------
def llm_available():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def ask_claude(prompt, system="", max_tokens=3000, model=None):
    model = model or os.environ.get("CLAUDE_MODEL") or "claude-sonnet-5-5"
    body = {"model": model, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}]}
    if system:
        body["system"] = system
    for attempt in range(3):
        r = requests.post("https://api.anthropic.com/v1/messages", json=body, timeout=180, headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        })
        if r.ok:
            return "".join(b.get("text", "") for b in r.json()["content"])
        print(f"claude error {r.status_code}: {r.text[:300]}", file=sys.stderr)
        if r.status_code in (429, 500, 502, 503, 529):
            time.sleep(20 * (attempt + 1))
            continue
        r.raise_for_status()
    r.raise_for_status()


def ask_claude_json(prompt, system="", max_tokens=3000):
    text = ask_claude(prompt + "\n\n반드시 JSON만 출력하세요. 코드블록 표시 없이.", system, max_tokens)
    m = re.search(r"[\[{].*[\]}]", text, re.S)
    return json.loads(m.group(0) if m else text)


# ---------- 긴 메시지 나눠 보내기 ----------
def send_long(text, silent=False, limit=3800):
    """긴 메시지를 줄 단위로 나눠 보냄 (HTML 태그가 중간에 잘리지 않도록)."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > limit:
            chunks.append(cur.rstrip("\n"))
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur.rstrip("\n"))
    for c in chunks:
        send_message(c, silent=silent, limit=None)
        time.sleep(1)


def link(title, url):
    return f'<a href="{html_escape(url)}">{html_escape(title)}</a>'
