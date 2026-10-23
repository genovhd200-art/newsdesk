"""Обща логика: теглене и разчитане на RSS/Atom, превод. Ползва се от server.py и от api/*.py."""
import json, re, time, html, os
import urllib.request, urllib.parse, urllib.error
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

BASE = os.path.dirname(os.path.abspath(__file__))
FEEDS_FILE = os.path.join(BASE, "feeds.json")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
first_seen = {}  # link -> време на първо виждане (за фийдове без дата)
CYR = re.compile("[А-Яа-я]")


def gtranslate(text):
    data = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": "bg", "dt": "t", "q": text}).encode()
    req = urllib.request.Request("https://translate.googleapis.com/translate_a/single", data=data, headers={"User-Agent": UA})
    for wait in (0, 5, 15, 40):  # при 429 изчакваме и опитваме пак
        time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return "".join(p[0] for p in json.load(r)[0] if p[0])
        except urllib.error.HTTPError as ex:
            if ex.code != 429:
                raise
    raise RuntimeError("rate limited")


def load_feeds():
    with open(FEEDS_FILE, encoding="utf-8") as f:
        feeds = json.load(f)
    for name in ("clubs.json", "x.json"):  # допълнителни фийдове: клубове/хора през Google News и X през keep.md
        extra = os.path.join(BASE, name)
        if os.path.exists(extra):
            with open(extra, encoding="utf-8") as f:
                feeds += json.load(f)
    return feeds


def strip(t, n=280):
    t = re.sub(r"<[^>]+>", " ", html.unescape(t or ""))
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "…"


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def local(tag):
    return tag.rsplit("}", 1)[-1]


def child_text(el, *names):
    for c in el:
        if local(c.tag) in names and (c.text or "").strip():
            return c.text.strip()
    return ""


def find_image(el, desc):
    for c in el:
        n = local(c.tag)
        if n in ("thumbnail", "content") and c.get("url") and (
                n == "thumbnail" or "image" in (c.get("type") or c.get("medium") or "image")):
            return c.get("url")
        if n == "enclosure" and "image" in (c.get("type") or ""):
            return c.get("url")
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', html.unescape(desc or ""))
    return m.group(1) if m else ""


_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def parse_xml(data):
    try:
        return ET.fromstring(data)
    except ET.ParseError:  # счупени UTF-8 байтове (напр. срязан емотикон) или забранени знаци
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        text = _BAD_XML.sub("", text)
        text = re.sub(r"^<\?xml[^>]*\?>", "", text.lstrip())
        return ET.fromstring(text)


def parse_feed(data, feed):
    root = parse_xml(data)
    out = []
    entries = [e for e in root.iter() if local(e.tag) in ("item", "entry")]
    for e in entries:
        link = child_text(e, "link")
        if not link:
            for c in e:
                if local(c.tag) == "link" and c.get("href"):
                    if c.get("rel", "alternate") == "alternate":
                        link = c.get("href"); break
        desc = child_text(e, "description", "summary", "encoded", "content")
        ts = parse_date(child_text(e, "pubDate", "published", "updated", "date"))
        now = time.time()
        est = False  # est=True: датата липсва или е в бъдещето, значи не е надеждна; браузърът ползва времето на първо виждане
        if ts is None or ts > now + 300:
            est, ts = True, first_seen.setdefault(link, now)
        else:
            ts = min(ts, now)
        out.append({
            "title": strip(child_text(e, "title"), 300) or "(без заглавие)",
            "link": link,
            "summary": strip(desc, 200),
            "image": find_image(e, desc),
            "ts": ts,
            "est": est,
            "source": feed["name"],
            "category": feed.get("category", ""),
            "type": feed.get("type", "rss"),
        })
    return out


UAS = [UA, "Mozilla/5.0 (compatible; NewsDesk/1.0)", "Feedly/1.0 (+http://www.feedly.com/fetcher.html)"]


FETCH_TIMEOUT = 7  # секунди; бавен източник не бива да бави всички останали


page_cache = {}  # link -> готов запис (за сайтове без RSS, чиито статии четем от sitemap)


def _meta(html_text, prop):
    m = re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]*content=["\']([^"\']*)' % re.escape(prop), html_text)
    return html.unescape(m.group(1)).strip() if m else ""


def _article(link, feed):
    if link in page_cache:
        return page_cache[link]
    req = urllib.request.Request(urllib.parse.quote(link, safe=":/?&=%+#@!$'()*,;~-._"), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=6) as r:
        txt = r.read(400000).decode("utf-8", "replace")
    title = _meta(txt, "og:title")
    if not title:
        return None
    m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', txt)
    ts = parse_date(_meta(txt, "article:published_time")) or (parse_date(m.group(1)) if m else None)
    item = {"title": strip(title, 300), "link": link, "summary": strip(_meta(txt, "og:description"), 200),
            "image": _meta(txt, "og:image") or None, "ts": min(ts, time.time()) if ts else None, "est": False,
            "source": feed["name"], "category": feed.get("category", ""), "type": feed.get("type", "rss")}
    cfg = feed.get("listing") or {}
    if ts is None and cfg.get("date_re"):  # само дата (без час): стари дни - обед на деня, днешни - от първото виждане
        dm = re.search(cfg["date_re"], txt)
        if dm:
            from zoneinfo import ZoneInfo
            d, mo, y = (int(x) for x in dm.groups())
            day = datetime(y, mo, d, 12, tzinfo=ZoneInfo("Europe/Sofia"))
            if day.date() < datetime.now(ZoneInfo("Europe/Sofia")).date():
                item["ts"] = day.timestamp()
    page_cache[link] = item
    return item


def fetch_listing(feed):
    """Сайт без RSS и sitemap: четем списъка с новини от началната страница, а подробностите - от всяка статия."""
    from concurrent.futures import ThreadPoolExecutor
    cfg = feed["listing"]
    req = urllib.request.Request(feed["url"], headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=8) as r:
        txt = r.read(600000).decode("utf-8", "replace")
    links, seen = [], set()
    for href in re.findall(cfg["link_re"], txt):
        link = urllib.parse.urljoin(feed["url"], html.unescape(href))
        if link not in seen:
            seen.add(link)
            links.append(link)
    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(lambda l: _safe(_article, l, feed), links[:cfg.get("limit", 10)]))
    return _finish(feed, res)


def _finish(feed, res):
    items = []
    for i in res:
        if i:
            if i["ts"] is None:  # няма час на публикуване: считаме от първото виждане (като във фийдовете без дата)
                i = {**i, "ts": first_seen.setdefault(i["link"], time.time()), "est": True}
            items.append(i)
    return feed["name"], items, {"ok": bool(items), "count": len(items), "at": time.time(), **({} if items else {"error": "няма статии"})}


def fetch_sitemap(feed):
    """Сайт без RSS: взимаме най-новите статии от sitemap, а заглавие/снимка/час - от самата страница (og: етикети)."""
    from concurrent.futures import ThreadPoolExecutor
    cfg = feed["sitemap"]
    req = urllib.request.Request(feed["url"], headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=8) as r:
        xml = r.read().decode("utf-8", "replace")
    if cfg.get("news"):
        return fetch_news_sitemap(feed, xml, cfg)
    links = [html.unescape(u) for u in re.findall(r"<loc>([^<]+)</loc>", xml) if cfg.get("pattern", "") in u]
    def key(u):  # номерът на статията в адреса (по-голям = по-нова)
        m = re.search(r"/(\d+)", u[u.rfind(cfg.get("pattern", "/")):])
        return int(m.group(1)) if m else 0
    links.sort(key=key, reverse=True)
    links = links[:cfg.get("limit", 12)]
    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(lambda l: _safe(_article, l, feed), links))
    return _finish(feed, res)


def _tag(block, name):
    m = re.search(r"<%s>\s*(.*?)\s*</%s>" % (name, name), block, re.S)
    return html.unescape(m.group(1)).strip() if m else ""


def fetch_news_sitemap(feed, xml, cfg):
    """Google News sitemap: заглавие, час и снимка са вече в самия файл (ако часът липсва, четем го от страницата)."""
    from concurrent.futures import ThreadPoolExecutor
    now, rows = time.time(), []
    for b in re.findall(r"<url>(.*?)</url>", xml, re.S):
        link, title, when = _tag(b, "loc"), _tag(b, "news:title"), _tag(b, "news:publication_date")
        if not (link and title):
            continue
        ts = parse_date(when)
        rows.append({"title": strip(title, 300), "link": link, "summary": "", "image": _tag(b, "image:loc") or None,
                     "ts": min(ts, now) if ts else now, "est": False, "_dateonly": "T" not in when,
                     "source": feed["name"], "category": feed.get("category", ""), "type": feed.get("type", "rss")})
    rows.sort(key=lambda i: i["ts"], reverse=True)
    rows = rows[:cfg.get("limit", 30)]
    def exact(i):  # само дата без час: взимаме часа от страницата (кеширано)
        if i["_dateonly"]:
            full = _safe(_article, i["link"], feed)
            if full:
                i["ts"] = full["ts"] or i["ts"]
                i["summary"] = full["summary"]
        return i
    with ThreadPoolExecutor(6) as ex:
        rows = list(ex.map(exact, rows))
    for i in rows:
        i.pop("_dateonly", None)
    rows.sort(key=lambda i: i["ts"], reverse=True)
    return feed["name"], rows, {"ok": bool(rows), "count": len(rows), "at": now, **({} if rows else {"error": "няма статии в sitemap"})}


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return None


def fetch(feed):
    if feed.get("listing"):
        try:
            return fetch_listing(feed)
        except Exception as ex:
            return feed["name"], [], {"ok": False, "error": str(ex)[:120], "at": time.time()}
    if feed.get("sitemap"):
        try:
            return fetch_sitemap(feed)
        except Exception as ex:
            return feed["name"], [], {"ok": False, "error": str(ex)[:120], "at": time.time()}
    err = "unknown"
    for ua in UAS:  # някои сайтове пускат само определен User-Agent
        try:
            url = urllib.parse.quote(feed["url"], safe=":/?&=%+#@!$'()*,;~-._")  # кирилица в адреса се кодира
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
                items = parse_feed(r.read(), feed)
            return feed["name"], items, {"ok": True, "count": len(items), "at": time.time()}
        except urllib.error.HTTPError as ex:
            err = str(ex)[:120]
            if ex.code not in (403, 406, 429, 202):  # друг User-Agent помага само при отказ, не при 404/500
                break
        except (TimeoutError, urllib.error.URLError) as ex:  # бавен или недостъпен сървър: нов опит е напразен
            err = str(ex)[:120]
            break
        except Exception as ex:  # празен или счупен отговор (напр. ESPN с първия UA): пробваме следващия
            err = str(ex)[:120]
    return feed["name"], [], {"ok": False, "error": err, "at": time.time()}
