#!/usr/bin/env python3
"""Find research-topic leads in an official faculty directory, without inferring admissions."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import json
import re
import urllib.parse
import urllib.request


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.words = []
        self.links = []
        self.href = None
        self.label = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if tag == "a" and not self.skip:
            self.href = dict(attrs).get("href")
            self.label = []

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.words.append(data.strip())
            if self.href:
                self.label.append(data.strip())

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            label = " ".join(" ".join(self.label).split())
            if label:
                self.links.append((label, self.href))
            self.href = None
            self.label = []
        if tag in ("script", "style") and self.skip:
            self.skip -= 1


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=12) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        body = response.read(300000).decode(charset, "replace")
        page = Page()
        page.feed(body)
        return response.url, page


def same_host(url, root):
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme in ("http", "https") and parsed.hostname == urllib.parse.urlsplit(root).hostname


def teacher_links(page, base_url, root):
    seen = set()
    links = []
    for label, href in page.links:
        url = urllib.parse.urljoin(base_url, href)
        if not same_host(url, root) or url in seen:
            continue
        if not re.fullmatch(r"[\u4e00-\u9fff]{2,5}", label):
            continue
        if not re.search(r"(?:page\.(?:htm|html|php)|/person/|/people/|/teacher/)", url, re.I):
            continue
        seen.add(url)
        links.append((label, url))
    return links


def research_excerpt(text, term):
    position = text.casefold().find(term.casefold())
    if position < 0:
        return ""
    return text[max(0, position - 55):position + len(term) + 115]


def inspect_person(item, terms):
    name, url = item
    try:
        final_url, page = fetch(url)
    except Exception as exc:
        return {"name": name, "url": url, "error": type(exc).__name__ + ": " + str(exc)[:120]}
    text = " ".join(" ".join(page.words).split())
    matched = [term for term in terms if term.casefold() in text.casefold()]
    return {"name": name, "url": final_url, "matched_terms": matched,
            "snippets": {term: research_excerpt(text, term) for term in matched},
            "role_excerpt": next((research_excerpt(text, role) for role in ("博士生导师", "硕士生导师", "教授", "研究员")
                                  if role in text), "")}


def scan(url, terms, max_profiles=80, max_results=25):
    if not url.startswith(("http://", "https://")):
        raise ValueError("Faculty directory URL must use HTTP(S)")
    final_url, page = fetch(url)
    links = teacher_links(page, final_url, url)
    if len(links) < 3:
        for label, href in page.links:
            if label.strip(" -·\t\r\n").casefold() not in ("教师", "师资", "师资队伍", "faculty", "teachers"):
                continue
            child_url = urllib.parse.urljoin(final_url, href)
            if not same_host(child_url, url) or child_url == final_url:
                continue
            child_url, child_page = fetch(child_url)
            child_links = teacher_links(child_page, child_url, url)
            if len(child_links) > len(links):
                final_url, links = child_url, child_links
    links_found = len(links)
    roster = [{"name": name, "url": profile_url} for name, profile_url in links]
    links = links[:max_profiles]
    with ThreadPoolExecutor(max_workers=min(4, len(links) or 1)) as pool:
        rows = list(pool.map(lambda item: inspect_person(item, terms), links))
    errors = [{"name": row["name"], "url": row["url"], "error": row["error"]}
              for row in rows if "error" in row]
    matches = [row for row in rows if row.get("matched_terms")]
    matches.sort(key=lambda row: (-len(row["matched_terms"]), row["name"]))
    return {"source": final_url, "terms": terms, "links_found": links_found,
            "profiles_scanned": len(rows), "unscanned_links": links_found - len(rows),
            "roster": roster, "matches_found": len(matches), "matches": matches[:max_results],
            "omitted_matches": max(0, len(matches) - max_results), "errors": errors[:10],
            "note": "Roster lists faculty-directory links; matches lists only the top research-keyword hits. Absence from matches does not mean absence from the roster or unscanned. Current admission and degree-supervisor eligibility require separate verification."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="Official department faculty-directory URL")
    parser.add_argument("--terms", required=True, help="Comma-separated research keywords")
    parser.add_argument("--max-profiles", type=int, default=80)
    parser.add_argument("--max-results", type=int, default=25)
    args = parser.parse_args()
    if not (1 <= args.max_profiles <= 150 and 1 <= args.max_results <= 50):
        parser.error("max-profiles must be 1–150 and max-results 1–50")
    terms = list(dict.fromkeys(x.strip() for x in args.terms.split(",") if x.strip()))
    if not terms:
        parser.error("at least one term is required")
    print(json.dumps(scan(args.url, terms, args.max_profiles, args.max_results), ensure_ascii=False,
                     separators=(",", ":")))


if __name__ == "__main__":
    main()
