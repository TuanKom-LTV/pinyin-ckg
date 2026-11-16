"""Lịch tin kinh tế trong tuần. Lấy lần lượt từ 3 nguồn, nguồn trước lỗi (vd 429 gọi quá nhiều) thì dùng nguồn sau:
  1. Forex Factory JSON   2. Forex Factory XML (giờ GMT)   3. Myfxbook RSS (có cả số thực tế)
Lưu đệm 30 phút (và ra file) để không gọi nguồn quá nhiều."""

import html as H
import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from fastapi import APIRouter

from app.core.mailer import _V4Conn

router = APIRouter(prefix="/ckg/api", tags=["PinYin CKG · tin tức"])
FILE = Path(__file__).resolve().parent.parent.parent / "data" / "news_week.json"
TTL = 1800
UA = {"User-Agent": "Mozilla/5.0 (PinYin CKG)", "Accept": "*/*"}
_cache = {"t": 0, "items": None, "src": None}

COUNTRY_CCY = {"United States": "USD", "Euro Area": "EUR", "Eurozone": "EUR", "European Union": "EUR", "Germany": "EUR",
               "France": "EUR", "Italy": "EUR", "Spain": "EUR", "United Kingdom": "GBP", "Japan": "JPY", "Australia": "AUD",
               "New Zealand": "NZD", "Canada": "CAD", "Switzerland": "CHF", "China": "CNY"}


def _get(host, path):
    conn = _V4Conn(host, timeout=20)
    try:
        conn.request("GET", path, headers=UA)
        r = conn.getresponse()
        body = r.read()
        if r.status != 200:
            raise RuntimeError(f"{host} trả {r.status}")
        return body
    finally:
        conn.close()


def _ff_json():
    raw = json.loads(_get("nfs.faireconomy.media", "/ff_calendar_thisweek.json").decode("utf-8"))
    return [{"title": x.get("title", ""), "ccy": x.get("country", ""), "date": x.get("date", ""), "impact": x.get("impact", ""),
             "forecast": x.get("forecast", ""), "previous": x.get("previous", ""), "actual": ""} for x in raw if isinstance(x, dict)]


def _ff_xml():
    t = _get("nfs.faireconomy.media", "/ff_calendar_thisweek.xml").decode("cp1252", "ignore")
    out = []
    for ev in re.findall(r"<event>(.*?)</event>", t, re.S):
        g = lambda k: H.unescape(re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", (re.search(rf"<{k}>(.*?)</{k}>", ev, re.S) or [None, ""])[1])).strip()
        d, tm = g("date"), g("time").lower()
        m = re.match(r"(\d{1,2}):(\d{2})(am|pm)", tm)
        if not m:                       # "All Day", "Tentative": bỏ qua
            continue
        h = int(m[1]) % 12 + (12 if m[3] == "pm" else 0)
        mo, dd, yy = (int(v) for v in d.split("-"))
        dt = datetime(yy, mo, dd, h, int(m[2]), tzinfo=timezone.utc)   # giờ trong bản XML là GMT
        out.append({"title": g("title"), "ccy": g("country"), "date": dt.isoformat(), "impact": g("impact"),
                    "forecast": g("forecast"), "previous": g("previous"), "actual": ""})
    return out


def _mfb():
    t = _get("www.myfxbook.com", "/rss/forex-economic-calendar-events").decode("utf-8", "ignore")
    out = []
    for it in re.findall(r"<item>(.*?)</item>", t, re.S):
        title = H.unescape((re.search(r"<title>(.*?)</title>", it, re.S) or [None, ""])[1]).strip()
        ccy = next((c for k, c in COUNTRY_CCY.items() if title.startswith(k + " ")), None)
        if not ccy:
            continue
        name = next(title[len(k) + 1:] for k in COUNTRY_CCY if title.startswith(k + " "))
        desc = H.unescape((re.search(r"<description>(.*?)</description>", it, re.S) or [None, ""])[1])
        imp = "High" if "high-impact" in desc else "Medium" if "medium-impact" in desc else "Low"
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", desc, re.S)]
        pub = (re.search(r"<pubDate>(.*?)</pubDate>", it) or [None, ""])[1]
        try:
            dt = parsedate_to_datetime(pub.replace(" GMT", " +0000"))
        except Exception:
            continue
        prev, cons, act = (cells + ["", "", "", "", ""])[2:5]
        out.append({"title": name, "ccy": ccy, "date": dt.isoformat(), "impact": imp,
                    "forecast": cons, "previous": prev, "actual": act})
    return out


SOURCES = (("Forex Factory", _ff_json), ("Forex Factory (XML)", _ff_xml), ("Myfxbook", _mfb))


@router.get("/news")
def news():
    now = time.time()
    if _cache["items"] is None and FILE.exists():
        try:
            d = json.loads(FILE.read_text(encoding="utf-8"))
            _cache.update(t=d["t"], items=d["items"], src=d.get("src"))
        except Exception:
            pass
    err = []
    fresh = _cache["items"] is not None and now - _cache["t"] <= TTL
    if not fresh and now - _cache.get("fail", 0) > 300:
        for name, fn in SOURCES:
            try:
                items = fn()
                if not items:
                    raise RuntimeError("không có tin")
                _cache.update(t=now, items=items, src=name)
                FILE.parent.mkdir(parents=True, exist_ok=True)
                FILE.write_text(json.dumps({"t": now, "items": items, "src": name}, ensure_ascii=False), encoding="utf-8")
                err = []
                break
            except Exception as e:
                err.append(f"{name}: {e}")
        if err:
            _cache["fail"] = now                   # cả 3 nguồn lỗi: nghỉ 5 phút rồi mới thử lại
    if _cache["items"] is None:
        return {"ok": False, "loi": "Chưa lấy được lịch tin: " + "; ".join(err or ["các nguồn đang tạm chặn, thử lại sau vài phút"])}
    return {"ok": True, "items": _cache["items"], "upd": int(_cache["t"]), "src": _cache["src"], "cu": "; ".join(err) or None}
