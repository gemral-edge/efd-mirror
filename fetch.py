# -*- coding: utf-8 -*-
"""Download public U.S. Senate Periodic Transaction Reports (PTR) from efdsearch.senate.gov into raw JSON.

Standalone, no database, no secrets.
Output (out dir):
  filings.json          list of {doc, kind, url, first, last, filed (MM/DD/YYYY), report_for, amendment}
  docs/<uuid>.json      {doc, kind, url, html, images: {src: base64}, fetched_at}
                        html = transactions <table> (electronic) or the page <img> tags (paper)
  errors.json           [{doc, url, error}]
Usage: python efd_ptr_fetch.py --since 2012-01-01 [--until 2014-12-31] --out out [--delay 1.0] [--no-images]
       [--skip-file known.txt]   (one uuid per line: docs already fetched, not downloaded again)
"""
from __future__ import annotations

import argparse
import base64
import json
import random
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

EFD = "https://efdsearch.senate.gov"
_HREF_RE = re.compile(r'href="(/search/view/(ptr|paper)/([0-9a-f\-]{36})/)"')
_TITLE_RE = re.compile(r"for (\d{2}/\d{2}/\d{4})(?: \(Amendment (\d+)\))?")
_TABLE_RE = re.compile(r"<table\b.*?</table>", re.S | re.I)
_IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
_SRC_RE = re.compile(r'src="([^"]+)"', re.I)


class Fetcher:
    def __init__(self, delay: float):
        from curl_cffi import requests
        self.delay = delay
        self.s = requests.Session(impersonate="chrome120")
        self._last = 0.0

    def _wait(self):
        gap = self.delay * random.uniform(0.8, 1.3) - (time.time() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.time()

    def req(self, method: str, url: str, **kw):
        last = None
        for attempt in range(6):
            self._wait()
            try:
                r = self.s.request(method, url, timeout=60, **kw)
                if r.status_code == 200:
                    return r
                last = f"HTTP {r.status_code}"
                if r.status_code in (403, 429, 503):           # back off hard when the site pushes back
                    time.sleep(min(300, 15 * 2 ** attempt))
                    if r.status_code == 403 and attempt >= 1:
                        self.agree()
                    continue
            except Exception as exc:  # noqa: BLE001
                last = repr(exc)[:200]
            time.sleep(min(120, 3 * 2 ** attempt))
        raise RuntimeError(f"{method} {url}: {last}")

    def agree(self):
        r = self.s.get(EFD + "/search/home/", timeout=30)
        m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', r.text)
        if r.status_code != 200 or not m:
            raise RuntimeError(f"home page blocked (HTTP {r.status_code})")
        self.s.post(EFD + "/search/home/", data={"csrfmiddlewaretoken": m.group(1), "prohibition_agreement": "1"},
                    headers={"Referer": EFD + "/search/home/"}, timeout=30)


def list_ptrs(f: Fetcher, since: date, until: date | None) -> list[dict]:
    out, start = [], 0
    while True:
        data = {"start": str(start), "length": "100", "report_types": "[11]", "filer_types": "[]",
                "submitted_start_date": since.strftime("%m/%d/%Y 00:00:00"),
                "submitted_end_date": until.strftime("%m/%d/%Y 23:59:59") if until else "",
                "candidate_state": "", "senator_state": "", "office_id": "", "first_name": "", "last_name": "",
                "draw": "1"}
        j = f.req("POST", EFD + "/search/report/data/", data=data,
                  headers={"Referer": EFD + "/search/", "X-CSRFToken": f.s.cookies.get("csrftoken") or ""}).json()
        for first, last, _full, link, filed in j["data"]:
            m, t = _HREF_RE.search(link), _TITLE_RE.search(re.sub(r"<[^>]+>", "", link))
            if not m:
                continue
            out.append({"doc": m.group(3), "kind": "electronic" if m.group(2) == "ptr" else "paper",
                        "url": EFD + m.group(1), "first": first.strip(), "last": last.strip(), "filed": filed.strip(),
                        "report_for": t.group(1) if t else None,
                        "amendment": int(t.group(2)) if t and t.group(2) else 0})
        start += 100
        print(f"  listed {min(start, j['recordsTotal'])}/{j['recordsTotal']}", flush=True)
        if start >= j["recordsTotal"]:
            return out


def fetch_doc(f: Fetcher, item: dict, images: bool) -> dict:
    html = f.req("GET", item["url"]).text
    if item["kind"] == "electronic":
        m = _TABLE_RE.search(html)
        body, imgs = (m.group(0) if m else ""), {}
    else:
        tags = [t for t in _IMG_RE.findall(html) if "efd-media-public" in t]
        body, imgs = "\n".join(tags), {}
        if images:
            for t in tags:
                src = _SRC_RE.search(t).group(1)
                imgs[src] = base64.b64encode(f.req("GET", src).content).decode("ascii")
    return {"doc": item["doc"], "kind": item["kind"], "url": item["url"], "html": body, "images": imgs,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", default=None)
    ap.add_argument("--out", default="out")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--no-images", action="store_true", help="skip page images of paper filings")
    ap.add_argument("--skip-file", default=None, help="uuids (one per line) not to download again")
    ap.add_argument("--max-docs", type=int, default=None)
    a = ap.parse_args()

    out = Path(a.out)
    (out / "docs").mkdir(parents=True, exist_ok=True)
    skip = set()
    if a.skip_file and Path(a.skip_file).exists():
        skip = {x.strip() for x in Path(a.skip_file).read_text(encoding="utf-8").split() if x.strip()}
    f = Fetcher(a.delay)
    f.agree()
    filings = list_ptrs(f, date.fromisoformat(a.since), date.fromisoformat(a.until) if a.until else None)
    (out / "filings.json").write_text(json.dumps(filings, indent=0), encoding="utf-8")
    todo = [x for x in filings if x["doc"] not in skip][:a.max_docs]
    kinds = {k: sum(1 for x in filings if x["kind"] == k) for k in ("electronic", "paper")}
    print(f"filings={len(filings)} {kinds} · to fetch={len(todo)} · skipped={len(filings) - len(todo)}", flush=True)
    errors = []
    for i, item in enumerate(todo, 1):
        try:
            d = fetch_doc(f, item, not a.no_images)
            (out / "docs" / f"{item['doc']}.json").write_text(json.dumps(d), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 — one bad filing must not stop the batch
            errors.append({"doc": item["doc"], "url": item["url"], "error": str(exc)[:300]})
        if i % 50 == 0:
            print(f"  fetched {i}/{len(todo)} · errors {len(errors)}", flush=True)
    (out / "errors.json").write_text(json.dumps(errors, indent=0), encoding="utf-8")
    print(f"done: fetched={len(todo) - len(errors)} errors={len(errors)}", flush=True)
    return 1 if todo and len(errors) > max(5, len(todo) // 100) else 0


if __name__ == "__main__":
    sys.exit(main())
