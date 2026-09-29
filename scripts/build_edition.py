"""Build the daily "Hong Kong in English" edition.

Fetches Hong Kong local news (RTHK, Inmedia, HK01), asks Claude to translate and
summarise it into plain English, and writes the result into index.html.
Only the JSON inside <script id="edition"> changes; the page design stays as is.

Needs the ANTHROPIC_API_KEY environment variable (a GitHub Actions secret).
"""
import email.utils
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

HKT = ZoneInfo("Asia/Hong_Kong")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
PAGE = os.path.join(os.path.dirname(__file__), "..", "index.html")
UA = {"User-Agent": "Mozilla/5.0 (compatible; HKinEnglish/1.0; personal news digest)"}

SECTIONS = [
    {"id": "top", "name": "Top stories"},
    {"id": "life", "name": "Hong Kong life"},
    {"id": "crime", "name": "Crime & scams"},
    {"id": "gov", "name": "Government & politics"},
    {"id": "money", "name": "Money & markets"},
    {"id": "sport", "name": "Sport & culture"},
]

RSS_FEEDS = [
    ("RTHK", "https://rthk.hk/rthk/news/rss/c_expressnews_clocal.xml"),
    ("RTHK", "https://rthk.hk/rthk/news/rss/c_expressnews_cfinance.xml"),
    ("Inmedia", "https://www.inmediahk.net/rss.xml"),
]
HK01_URL = "https://www.hk01.com/latest"


def clean(text, limit):
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def fetch_rss(source, url, since):
    items = []
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    for it in root.iter("item"):
        pub = it.findtext("pubDate") or ""
        try:
            when = email.utils.parsedate_to_datetime(pub).astimezone(HKT)
        except Exception:
            continue
        if when < since:
            continue
        items.append({
            "source": source,
            "published_hkt": when.strftime("%a %d %b %H:%M"),
            "title": clean(it.findtext("title"), 200),
            "url": (it.findtext("link") or "").strip(),
            "body": clean(it.findtext("description"), 1800),
        })
    return items


def fetch_hk01():
    r = requests.get(HK01_URL, headers=UA, timeout=30)
    r.raise_for_status()
    seen, items = set(), []
    for href, text in re.findall(r'<a[^>]+href="(https://www\.hk01\.com/[^"/]+/\d+/[^"]*)"[^>]*>([^<]{8,})</a>', r.text):
        title = clean(text, 200)
        if href in seen or not title:
            continue
        seen.add(href)
        items.append({"source": "HK01", "published_hkt": "recent", "title": title, "url": href, "body": ""})
    return items[:30]


def gather():
    now = datetime.now(HKT)
    since = now - timedelta(hours=26)
    items, ok, failed = [], [], []
    for source, url in RSS_FEEDS:
        try:
            got = fetch_rss(source, url, since)
            items += got
            ok.append(f"{source} ({len(got)})")
        except Exception as e:
            failed.append(f"{url}: {e}")
    try:
        got = fetch_hk01()
        items += got
        ok.append(f"HK01 ({len(got)})")
    except Exception as e:
        failed.append(f"{HK01_URL}: {e}")
    return now, items, ok, failed


INSTRUCTIONS = """You are the editor of "Hong Kong in English", a daily digest of Hong Kong local news for an English speaker who cannot read Chinese.

Below are Chinese-language news items from the last ~24 hours (RTHK, Inmedia, HK01). Produce today's edition.

Choose 18-25 stories. Put the 3 most important Hong Kong stories in section "top". Prefer local news that matters to daily life: transport, housing, health, prices, weather, scams, government. Drop routine intraday market updates (keep only the Hang Seng closing story), repeated coverage and minor items. Merge duplicates across sources. Skip entertainment and celebrity gossip.

For each story, translate and summarise into plain, clear English:
- "section": one of top, life, crime, gov, money, sport
- "headline": one clear sentence, no jargon
- "summary": 2-3 sentences with the key facts
- "details": list of 0-4 short strings with extra facts, figures, and context a non-local reader needs
- "source": "RTHK", "Inmedia" or "HK01" (the item's source)
- "time": e.g. "4:53 pm" if published today, otherwise e.g. "Mon 28 Sep"; "Today" if unknown
- "url": the item's original link, copied exactly
- "zh": the item's original Chinese headline, copied exactly
Use the usual English names for people and places (e.g. John Lee, Joe Chow, Tsim Sha Tsui). Explain Cantonese slang in plain English. Stay neutral and faithful to the source: add no opinion and invent no facts. HK01 items are headlines only, so keep those summaries short and do not add detail beyond the headline.

Also produce "glance": exactly 3 objects with "label", "value", "sub", "trend" ("up", "down" or ""):
1. label "Weather": today's forecast if any item mentions it; otherwise value "See Observatory" and sub "No forecast in today's reports".
2. label "Hang Seng Index": the latest close, e.g. value "24,523 ▼118", sub "−0.48% at the close", trend "down" (use ▲ and "up" for gains).
3. label "Coming up": one useful upcoming item (holiday, typhoon signal, major event).

Respond with ONLY a JSON object: {"glance": [...], "stories": [...]}. No markdown fences, no commentary."""


def ask_claude(now, items):
    key = os.environ["ANTHROPIC_API_KEY"]
    payload = {
        "model": MODEL,
        "max_tokens": 16000,
        "messages": [{
            "role": "user",
            "content": INSTRUCTIONS + f"\n\nToday is {now:%A %d %B %Y}, {now:%I:%M %p} HKT.\n\nNEWS ITEMS (JSON):\n" + json.dumps(items, ensure_ascii=False),
        }],
    }
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    last_err = None
    for attempt in range(3):
        try:
            r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload, timeout=600)
            if r.status_code >= 400:
                raise RuntimeError(f"API {r.status_code}: {r.text[:500]}")
            text = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text").strip()
            text = re.sub(r"^```(?:json)?|```$", "", text).strip()
            data = json.loads(text[text.index("{"): text.rindex("}") + 1])
            return validate(data)
        except Exception as e:
            last_err = e
            print(f"Attempt {attempt + 1} failed: {e}", file=sys.stderr)
            time.sleep(10)
    raise SystemExit(f"Claude call failed: {last_err}")


def validate(data):
    ids = {s["id"] for s in SECTIONS}
    stories = []
    for s in data.get("stories", []):
        if s.get("section") not in ids or not s.get("headline") or not s.get("summary"):
            continue
        stories.append({
            "section": s["section"],
            "headline": str(s["headline"]),
            "summary": str(s["summary"]),
            "details": [str(d) for d in (s.get("details") or [])][:4],
            "source": str(s.get("source", "")),
            "time": str(s.get("time", "Today")),
            "url": str(s.get("url", "")),
            "zh": str(s.get("zh", "")),
        })
    glance = [
        {k: str(g.get(k, "")) for k in ("label", "value", "sub", "trend")}
        for g in (data.get("glance") or [])[:3]
    ]
    if len(stories) < 8:
        raise ValueError(f"only {len(stories)} usable stories")
    return glance, stories


def write_page(now, glance, stories):
    edition = {
        "date": now.strftime("%Y-%m-%d"),
        "dateLabel": now.strftime("%A, ") + str(now.day) + now.strftime(" %B %Y"),
        "updated": "Morning edition · updated " + now.strftime("%I:%M %p").lstrip("0").lower() + " HKT",
        "glance": glance,
        "sections": SECTIONS,
        "stories": stories,
    }
    blob = json.dumps(edition, ensure_ascii=False, indent=2).replace("</", "<\\/")
    page = open(PAGE, encoding="utf-8").read()
    pat = re.compile(r'(<script id="edition" type="application/json">)(.*?)(</script>)', re.S)
    if not pat.search(page):
        raise SystemExit("edition block not found in index.html")
    page = pat.sub(lambda m: m.group(1) + "\n" + blob + "\n" + m.group(3), page, count=1)
    open(PAGE, "w", encoding="utf-8").write(page)


def main():
    now, items, ok, failed = gather()
    print("Sources OK:", ", ".join(ok) or "none")
    for f in failed:
        print("Source failed:", f)
    if len(items) < 5:
        raise SystemExit("Too few news items fetched; keeping yesterday's edition.")
    glance, stories = ask_claude(now, items)
    write_page(now, glance, stories)
    print(f"Wrote {len(stories)} stories for {now:%Y-%m-%d}.")


if __name__ == "__main__":
    main()
