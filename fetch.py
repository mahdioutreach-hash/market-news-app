import os, re, json, hashlib, calendar
from datetime import datetime, timezone, timedelta, date
from urllib.parse import quote

from concurrent.futures import ThreadPoolExecutor

import feedparser
import trafilatura
import requests
from curl_cffi import requests as cr
import firebase_admin
from firebase_admin import credentials, firestore

FEEDS = {
    "CNBC": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "Bloomberg": "https://feeds.bloomberg.com/markets/news.rss",
    "Investing.com": "https://www.investing.com/rss/news.rss",
    "MarketWatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
}
ASSETS = [
    ("S&P 500", "SPX", "^GSPC"), ("Nasdaq", "IXIC", "^IXIC"), ("Gold", "XAU", "GC=F"),
    ("Silver", "XAG", "SI=F"), ("Oil (WTI)", "CL", "CL=F"),
    ("Dollar Index", "DXY", "DX-Y.NYB"), ("Bitcoin", "BTC", "BTC-USD"),
]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
MAX_NEW = 30      # ek run mein AI ko zyada se zyada itni nayi khabrein
KEEP_NEWS = 40    # Firestore mein itni khabrein rakho
KEEP_DAYS = 3

SYSTEM = """Tumhe bade news sites ki khabrein milengi (har khabar ka asal text ya chhota summary). Sirf wo khabrein chuno jo markets se judi hain (war, interest rates, Fed, CPI, inflation, jobs, GDP, oil, gold, silver, dollar, crypto, stocks, bade companies). Baqi chhod do.
Ek khabar kai sites par ho to ek item banao aur uske saare ids "ids" mein likho.
Jawab sirf JSON list mein do, har item mein ye keys:
ids (list of numbers),
title (Roman Urdu mein saaf headline),
summary (2-3 line, khabar ka nichod),
detail (poori khabar Roman Urdu mein news ke andaz mein: kya hua, kis ne kya kaha, numbers, background. Asal text mila ho to kam az kam 5-8 jumle. summary ko dohrao mat),
market_move (agar khabar mein likha ho ke market, stock ya asset ne kitna move kiya ya kitne waqt mein, to wahi numbers ke saath likho, warna khali string),
impact (kis asset par Bullish ya Bearish),
mood (log umeed mein hain ya ghabrahat mein),
focus (abhi sabki nazar kis cheez par hai),
scenario (agar aisa hua to ye ho sakta hai),
sentiment ("bull" ya "bear"),
importance (1 chhoti, 2 important, 3 bohot important).
RULES: khabar ke facts (naam, numbers, bayanat) sirf diye gaye text se lo, apni taraf se koi number ya bayan mat banao. impact, mood, focus, scenario analysis hain, unhein andaza ke tor par likho. Agar sirf chhota summary mila ho to detail mein utna hi likho jitna maloom hai, bekaar kheench kar lamba mat karo. Sab Roman Urdu mein likho (naam jaise Fed, CPI, Bitcoin English rahenge)."""


def get_article(url):
    try:
        r = cr.get(url, impersonate="chrome", timeout=12)
        if r.status_code == 200:
            return (trafilatura.extract(r.text) or "")[:2500]
    except Exception as e:
        print("article fail", url[:60], e)
    return ""


def get_prices():
    out = []
    for name, sym, yahoo in ASSETS:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(yahoo)}?interval=1d&range=1mo"
            r = cr.get(url, impersonate="chrome", timeout=20)
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
            price = res["meta"]["regularMarketPrice"]
            prev = closes[-2] if len(closes) > 1 else res["meta"].get("chartPreviousClose")
            pct = round((price - prev) / prev * 100, 2) if prev else 0.0
            out.append({"n": name, "s": sym, "v": round(price, 2), "c": pct,
                        "spark": [round(c, 2) for c in closes[-30:]]})
        except Exception as e:
            print("price fail", name, e)
    return out


def get_items():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    items, seen_titles = [], set()
    for source, url in FEEDS.items():
        try:
            feed = feedparser.parse(url, agent="Mozilla/5.0")
            for e in feed.entries:
                t = e.get("published_parsed") or e.get("updated_parsed")
                ts = datetime.fromtimestamp(calendar.timegm(t), timezone.utc) if t else datetime.now(timezone.utc)
                title = (e.get("title") or "").strip()
                key = title.lower()
                if ts < cutoff or not title or key in seen_titles:
                    continue
                seen_titles.add(key)
                summary = re.sub(r"<[^>]+>", " ", e.get("summary") or "")[:700]
                items.append({"source": source, "title": title, "summary": summary,
                              "link": e.get("link", ""), "ts": ts})
        except Exception as ex:
            print("feed fail", source, ex)
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items


def analyze(items):
    lines = [f"[{i}] ({it['source']}) {it['title']} - {(it.get('text') or it['summary'])[:1500]}" for i, it in enumerate(items)]
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": "NAYI KHABREN:\n" + "\n".join(lines)}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    r = requests.post(url, headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"],
                                    "Content-Type": "application/json"}, json=body, timeout=120)
    r.raise_for_status()
    txt = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    if txt.startswith("```"):
        txt = txt.strip("`").removeprefix("json").strip()
    data = json.loads(txt)
    news = []
    for o in data:
        ids = [i for i in o.get("ids", []) if isinstance(i, int) and 0 <= i < len(items)]
        if not ids:
            continue
        src = [items[i] for i in ids]
        news.append({
            "id": hashlib.sha1(o.get("title", "").encode()).hexdigest()[:12],
            "title": o.get("title", ""), "orig": src[0]["title"],
            "summary": o.get("summary", ""), "detail": o.get("detail", ""),
            "market_move": o.get("market_move", ""),
            "impact": o.get("impact", ""), "mood": o.get("mood", ""), "focus": o.get("focus", ""),
            "scenario": o.get("scenario", ""),
            "sentiment": "bear" if o.get("sentiment") == "bear" else "bull",
            "importance": o.get("importance", 1),
            "sources": sorted({s["source"] for s in src}),
            "ts": max(s["ts"] for s in src).isoformat(),
            "link": src[0]["link"],
        })
    return news



FRED = "https://api.stlouisfed.org/fred"
INDICATORS = [
    {"name": "US CPI (mehngai)", "rel": 10, "series": "CPIAUCSL", "kind": "yoy", "st": 3,
     "note": "Mehngai ka sabse bada number. Fed ki rate ka faisla isi par depend karta hai."},
    {"name": "US PPI (producer prices)", "rel": 46, "series": "PPIFIS", "kind": "yoy", "st": 2,
     "note": "Factory/producer level ki mehngai, CPI ka pehla ishara."},
    {"name": "US Jobs report (Nonfarm Payrolls)", "rel": 50, "series": "PAYEMS", "kind": "jobs", "st": 3,
     "note": "Mahine mein kitni nayi naukriyan bani aur berozgari ki rate."},
    {"name": "US Retail Sales", "rel": 9, "series": "RSAFS", "kind": "mom", "st": 2,
     "note": "Logon ki kharch karne ki taqat."},
    {"name": "US GDP (growth)", "rel": 53, "series": "A191RL1Q225SBEA", "kind": "level", "st": 2,
     "note": "Maeeshat kitni barh rahi hai (saalana raftaar)."},
]


def fred(path, **params):
    params.update(api_key=os.environ["FRED_API_KEY"], file_type="json")
    r = requests.get(f"{FRED}/{path}", params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def obs(series, n):
    rows = fred("series/observations", series_id=series, sort_order="desc", limit=n)["observations"]
    return [(o["date"], float(o["value"])) for o in rows if o["value"] not in (".", "")]


def mon(d):
    return datetime.strptime(d, "%Y-%m-%d").strftime("%b")


def pct(a, b):
    return (a / b - 1) * 100


def values(ind):
    k = ind["kind"]
    s = obs(ind["series"], 16)
    if k == "yoy":
        mom = pct(s[0][1], s[1][1])
        return (f"{pct(s[0][1], s[12][1]):.1f}% ({mon(s[0][0])})",
                f"{pct(s[1][1], s[13][1]):.1f}% ({mon(s[1][0])})",
                f"Pichle mahine se: {mom:+.1f}%")
    if k == "jobs":
        u = obs("UNRATE", 2)
        return (f"{s[0][1] - s[1][1]:+.0f}K ({mon(s[0][0])})",
                f"{s[1][1] - s[2][1]:+.0f}K ({mon(s[1][0])})",
                f"Berozgari ki rate: {u[0][1]:.1f}%")
    if k == "mom":
        return (f"{pct(s[0][1], s[1][1]):+.1f}% ({mon(s[0][0])})",
                f"{pct(s[1][1], s[2][1]):+.1f}% ({mon(s[1][0])})", "")
    return f"{s[0][1]:.1f}% ({mon(s[0][0])})", f"{s[1][1]:.1f}% ({mon(s[1][0])})", ""


def rel_dates(rid):
    rows = fred("release/dates", release_id=rid, include_release_dates_with_no_data="true",
                sort_order="desc", limit=24)["release_dates"]
    return sorted(r["date"] for r in rows)


def get_econ():
    if not os.environ.get("FRED_API_KEY"):
        return None
    today = datetime.now(timezone.utc).date()
    events, past = [], []
    for ind in INDICATORS:
        try:
            latest, prev, extra = values(ind)
            dates = rel_dates(ind["rel"])
            ev = {"name": ind["name"], "st": ind["st"], "note": ind["note"],
                  "latest": latest, "prev": prev, "extra": extra}
            nxt = [d for d in dates if d >= today.isoformat()]
            last = [d for d in dates if d < today.isoformat()]
            if nxt:
                events.append({**ev, "date": nxt[0]})
            if last and (today - date.fromisoformat(last[-1])).days <= 35:
                past.append({**ev, "date": last[-1]})
        except Exception as e:
            print("fred fail", ind["name"], e)
    rate = ""
    try:
        rate = f"{obs('DFEDTARL', 1)[0][1]:.2f}% - {obs('DFEDTARU', 1)[0][1]:.2f}%"
    except Exception as e:
        print("fred rate fail", e)
    events.sort(key=lambda x: x["date"])
    past.sort(key=lambda x: x["date"], reverse=True)
    return {"events": events, "past": past, "rate": rate, "at": datetime.now(timezone.utc).isoformat()}


def main():
    cred = credentials.Certificate(json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"]))
    firebase_admin.initialize_app(cred)
    ref = firestore.client().collection("public").document("latest")
    snap = ref.get()
    old = snap.to_dict() if snap.exists else {}

    prices = get_prices() or old.get("prices", [])

    econ = old.get("econ")
    try:
        fresh = econ and (datetime.now(timezone.utc) - datetime.fromisoformat(econ["at"])) < timedelta(minutes=50)
        if not fresh:
            econ = get_econ() or econ
    except Exception as e:
        print("econ fail", e)

    seen = list(old.get("seen", []))
    items = get_items()
    new_items = [i for i in items if i["link"] not in set(seen)][:MAX_NEW]
    news = list(old.get("news", []))
    if new_items:
        with ThreadPoolExecutor(8) as ex:
            for it, t in zip(new_items, ex.map(lambda i: get_article(i["link"]), new_items)):
                it["text"] = t
        print("articles with text:", sum(1 for i in new_items if i.get("text")), "of", len(new_items))
        for src in sorted({i["source"] for i in new_items}):
            grp = [i for i in new_items if i["source"] == src]
            print(f"  {src}: {sum(1 for i in grp if i.get('text'))} of {len(grp)} poora article mila")
        try:
            news = analyze(new_items) + news
            seen = (seen + [i["link"] for i in new_items])[-500:]
        except Exception as e:
            print("gemini fail", e)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    uniq = {}
    for n in sorted(news, key=lambda x: x["ts"], reverse=True):
        if n["ts"] >= cutoff and n["id"] not in uniq:
            uniq[n["id"]] = n
    news = list(uniq.values())[:KEEP_NEWS]

    ref.set({"updatedAt": datetime.now(timezone.utc).isoformat(),
             "prices": prices, "news": news, "seen": seen, "econ": econ})
    print(f"ok: {len(prices)} prices, {len(news)} news, {len(new_items)} new items")


if __name__ == "__main__":
    main()
