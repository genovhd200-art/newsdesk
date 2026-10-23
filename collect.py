"""Събира новините, превежда новите на български и записва site/data/items.json (пуска се от GitHub Actions на всеки 5 минути)."""
import json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, wait

import core

KEEP = 86400
CAP = 3000
LIMIT = 1500
MIN_PER_SOURCE = 8
MAX_TRANSLATE = 500  # на едно пускане; останалите - следващия път
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site", "data")


def previous():
    """Предишният снимък се взима от вече публикувания сайт (за превода, първото виждане и пазене на изпадналите статии)."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    url = os.environ.get("PREV_URL") or (f"https://{repo.split('/')[0]}.github.io/{repo.split('/')[1]}/data/items.json" if "/" in repo else "")
    if not url:
        return None
    try:
        with urllib.request.urlopen(urllib.request.Request(url + f"?t={int(time.time())}", headers={"User-Agent": core.UA}), timeout=30) as r:
            return json.load(r)
    except Exception as ex:
        print("няма предишен снимък:", str(ex)[:100])
        return None


def collect(feeds, prev):
    ex = ThreadPoolExecutor(16)
    futs = {ex.submit(core.fetch, f): f for f in feeds}
    done, _ = wait(futs, timeout=120)
    ex.shutdown(wait=False, cancel_futures=True)
    now = time.time()
    names = {f["name"] for f in feeds}
    prev_items = [i for i in (prev or {}).get("items", []) if i["source"] in names]
    prev_by_link = {i["link"]: i for i in prev_items}
    status, fresh = {}, {}
    for fut, f in futs.items():
        if fut in done and not fut.exception():
            name, its, st = fut.result()
        else:
            name, its, st = f["name"], [], {"ok": False, "error": "изтекло време", "at": now}
        if not st["ok"]:
            old = [i for i in prev_items if i["source"] == name]
            if old:
                its, st = old, {**st, "ok": True, "count": len(old), "stale": True}
        status[name] = st
        for it in its:
            if not it["link"]:
                continue
            p = prev_by_link.get(it["link"])
            if p:
                if it.get("est"):
                    it["ts"] = p["ts"]
                for k in ("title_bg", "summary_bg"):  # вече направеният превод се пази
                    if p.get(k) and not it.get(k):
                        it[k] = p[k]
            fresh[it["link"]] = it
    for link, it in prev_by_link.items():
        if link not in fresh and it["ts"] > now - KEEP:
            fresh[link] = it
    return sorted(fresh.values(), key=lambda i: i["ts"], reverse=True)[:CAP], status


def _chrome(text):
    q = urllib.parse.urlencode({"client": "dict-chrome-ex", "sl": "auto", "tl": "bg", "q": text})
    req = urllib.request.Request("https://clients5.google.com/translate_a/t?" + q, headers={"User-Agent": core.UA})
    with urllib.request.urlopen(req, timeout=12) as r:
        d = json.load(r)
    if d and isinstance(d[0], list):
        return d[0][0]
    return d[0] if isinstance(d, list) else d["sentences"][0]["trans"]


def _gtx(text):
    q = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": "bg", "dt": "t", "q": text})
    req = urllib.request.Request("https://translate.googleapis.com/translate_a/single?" + q, headers={"User-Agent": core.UA})
    with urllib.request.urlopen(req, timeout=12) as r:
        return "".join(p[0] for p in json.load(r)[0] if p[0])


def tr_text(text):
    last = None
    for fn in (_chrome, _gtx, _chrome):
        try:
            out = fn(text)
            if out:
                return out
        except Exception as ex:
            last = ex
            time.sleep(0.5)
    raise RuntimeError(str(last))


def translate(i):
    t = i["title"].replace("\n", " ")[:300]
    s = (i.get("summary") or "").replace("\n", " ")[:300]
    try:
        out = tr_text(t + ("\n" + s if s else "")).split("\n")
        i["title_bg"], i["summary_bg"] = out[0].strip(), " ".join(out[1:]).strip()
    except Exception:
        pass  # следващото пускане ще опита пак


def main():
    feeds = core.load_feeds()
    prev = previous()
    items, status = collect(feeds, prev)
    todo = [i for i in items if not i.get("title_bg") and not core.CYR.search(i["title"])][:MAX_TRANSLATE]
    print(f"{len(items)} статии, за превод: {len(todo)}")
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(translate, todo))
    print("преведени:", sum(1 for i in todo if i.get("title_bg")))
    top, per = items[:LIMIT], {}
    for i in top:
        per[i["source"]] = per.get(i["source"], 0) + 1
    for i in items[LIMIT:]:
        if per.get(i["source"], 0) < MIN_PER_SOURCE:
            per[i["source"]] = per.get(i["source"], 0) + 1
            top.append(i)
    top.sort(key=lambda i: i["ts"], reverse=True)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "items.json"), "w", encoding="utf-8") as f:
        json.dump({"items": top, "feeds": feeds, "status": status, "updated": time.time()}, f, ensure_ascii=False, separators=(",", ":"))
    bad = [n for n, s in status.items() if not s.get("ok")]
    print("източници с грешка:", bad or "няма")


if __name__ == "__main__":
    main()
