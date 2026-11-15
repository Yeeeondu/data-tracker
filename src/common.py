"""공통 유틸: 설정, 상태 저장, 텔레그램 전송, 브라우저."""
import contextlib
import hashlib
import json
import os
import pathlib
import sys
import time

import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
OUT_DIR = ROOT / "out"
OUT_DIR.mkdir(exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


def load_config():
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state(name):
    p = STATE_DIR / f"{name}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def save_state(name, data):
    STATE_DIR.mkdir(exist_ok=True)
    (STATE_DIR / f"{name}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------- Telegram ----------
def _tg(method, data=None, files=None):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(f"[DRY-RUN] telegram {method}: {data}")
        return None
    data = dict(data or {}, chat_id=chat_id)
    for attempt in range(3):
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                          data=data, files=files, timeout=60)
        if r.ok:
            return r.json()
        print(f"telegram error {r.status_code}: {r.text[:300]}", file=sys.stderr)
        time.sleep(3 * (attempt + 1))
    r.raise_for_status()


def send_message(text, silent=False, preview=False):
    return _tg("sendMessage", {
        "text": text[:4000], "parse_mode": "HTML",
        "disable_notification": str(silent).lower(),
        "disable_web_page_preview": str(not preview).lower(),
    })


def send_photo(path, caption="", silent=False):
    with open(path, "rb") as f:
        return _tg("sendPhoto", {
            "caption": caption[:1000], "parse_mode": "HTML",
            "disable_notification": str(silent).lower(),
        }, files={"photo": f})


def send_media_group(items, silent=False):
    """items: [(path, caption)] 최대 10장."""
    media, files = [], {}
    for i, (path, cap) in enumerate(items[:10]):
        key = f"p{i}"
        files[key] = open(path, "rb")
        media.append({"type": "photo", "media": f"attach://{key}",
                      "caption": cap[:1000], "parse_mode": "HTML"})
    try:
        return _tg("sendMediaGroup", {"media": json.dumps(media),
                                      "disable_notification": str(silent).lower()},
                   files=files)
    finally:
        for f in files.values():
            f.close()


def html_escape(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------- Browser ----------
@contextlib.contextmanager
def browser_page(locale="ko-KR", width=1280, height=900):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(user_agent=UA, locale=locale,
                            viewport={"width": width, "height": height},
                            device_scale_factor=2)
        page = ctx.new_page()
        try:
            yield page
        finally:
            ctx.close()
            b.close()


def notify_error_once(job, err):
    """같은 오류는 하루 1번만 알림."""
    st = load_state("errors")
    key = f"{job}:{sha(str(err))}"
    today = time.strftime("%Y-%m-%d")
    if st.get(key) != today:
        st[key] = today
        save_state("errors", st)
        send_message(f"⚠️ <b>{html_escape(job)}</b> 수집 실패\n<code>{html_escape(str(err))[:500]}</code>",
                     silent=True)
