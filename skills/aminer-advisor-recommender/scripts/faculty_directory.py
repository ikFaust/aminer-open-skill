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


def university_domain(host):
    parts = host.lower().split(".")
    if len(parts) >= 3 and parts[-2:] == ["edu", "cn"]:
        return ".".join(parts[-3:])
    return host.lower()


def same_university_domain(url, root):
    parsed = urllib.parse.urlsplit(url)
    root_host = urllib.parse.urlsplit(root).hostname
    if parsed.scheme not in ("http", "https") or not parsed.hostname or not root_host:
        return False
    domain = university_domain(root_host)
    return parsed.hostname == domain or parsed.hostname.endswith("." + domain)


def public_link(base_url, href):
    href = (href or "").strip()
    if not href or href.startswith("#") or re.match(r"(?i)^(javascript|mailto|tel|data):", href):
        return None
    url = urllib.parse.urljoin(base_url, href)
    if any(character.isspace() for character in url):
        return None
    return url


def teacher_links(page, base_url, root):
    seen = set()
    links = []
    for label, href in page.links:
        url = public_link(base_url, href)
        if not url or not same_university_domain(url, root) or url in seen:
            continue
        # Some university CMS cards use "姓名 职称 email" as the anchor label.
        name_match = re.match(r"^([\u4e00-\u9fff]{2,5})(?=\s|$)", label)
        if not name_match:
            continue
        name = name_match.group(1)
        if (name in {"在职教师", "退休教师", "荣退教师", "师资队伍", "教师风采", "师资力量",
                     "师资中心", "师资概况", "院士风采", "知名学者", "硕博导师", "导师简介",
                     "博导简介", "硕导简介", "教职工名单", "教职工名录"}
                or name.endswith(("学院", "研究所", "研究院", "工程系", "教学中心"))):
            continue
        path = urllib.parse.urlsplit(url).path
        if path.endswith(("/list.htm", "/list.html", "/list.php")):
            continue
        profile_path = re.search(r"(?:/page\.(?:htm|html|php)$|/(?:person|people|teachers?|faculty|staff|szll)/)", path, re.I)
        faculty_home = (urllib.parse.urlsplit(url).hostname or "").startswith("faculty.") and path.endswith("/main.htm")
        if not profile_path and not faculty_home:
            continue
        seen.add(url)
        links.append((name, url))
    return links


def faculty_navigation(page, base_url, root):
    """Find bounded faculty-list navigation, not news articles mentioning teachers."""
    seen = set()
    links = []
    for label, href in page.links:
        label = label.strip(" -·\t\r\n")
        url = public_link(base_url, href)
        if not url or not same_university_domain(url, root) or url == base_url or url in seen:
            continue
        path = urllib.parse.urlsplit(url).path.casefold()
        direct = label.casefold() in {"教师", "师资", "师资力量", "师资队伍", "师资中心",
                                       "教师简介", "教师名单", "教职工名单", "教职工名录",
                                       "导师简介", "博导简介", "硕导简介", "教授", "副教授",
                                       "faculty", "teachers"}
        category = label == "硕博导师" or label.endswith(("研究所", "研究院", "工程系"))
        if not direct and not category:
            continue
        if not direct and not re.search(r"/(?:szll|faculty|teachers?|staff|people|sz|ys)/", path):
            continue
        seen.add(url)
        links.append(url)
    return links


def faculty_pagination(page, base_url, root):
    """Follow bounded CMS list pages only when a faculty list exposes its last page."""
    path = urllib.parse.urlsplit(base_url).path
    match = re.fullmatch(r"(.*/list)(?:\d+)?\.htm", path)
    if not match:
        return []
    prefix = match.group(1)
    for label, href in page.links:
        if label not in {"尾页", "末页", "最后一页"}:
            continue
        last_url = public_link(base_url, href)
        if not last_url or not same_university_domain(last_url, root):
            continue
        last_path = urllib.parse.urlsplit(last_url).path
        last_match = re.fullmatch(re.escape(prefix) + r"(\d+)\.htm", last_path)
        if last_match:
            last_page = min(int(last_match.group(1)), 12)
            return [urllib.parse.urljoin(base_url, prefix + str(index) + ".htm")
                    for index in range(2, last_page + 1)]
    return []


def research_excerpt(text, term):
    position = text.casefold().find(term.casefold())
    if position < 0:
        return ""
    return text[max(0, position - 55):position + len(term) + 115]


def research_text(text):
    match = re.search(r"研究(?:方向|主题|领域|兴趣)\s*[:：]?", text)
    if not match:
        return text, "page_text"
    start = match.start()
    end = min(len(text), start + 800)
    for marker in ("个人简历", "个人简介", "教育经历", "工作经历", "版权所有"):
        position = text.find(marker, match.end())
        if position >= 0:
            end = min(end, position)
    return text[start:end], "research_section"


def profile_role(text, name, degree):
    for match in re.finditer(re.escape(name), text):
        nearby = text[max(0, match.start() - 100):match.end() + 1500]
        if degree == "master" and ("硕士生导师" in nearby or "硕导" in nearby or
                                   re.search(r"(?:报考本人|招收|招生).{0,25}硕士研究生", nearby)):
            return True
        if degree == "doctor" and ("博士生导师" in nearby or "博导" in nearby or
                                   re.search(r"(?:报考本人|招收|招生).{0,25}博士研究生", nearby)):
            return True
    return False


def paper_excerpt(text):
    for pattern in (r"代表作", r"代表性论文", r"论文\s*\d+[.、]"):
        match = re.search(pattern, text)
        if match:
            excerpt = text[match.start():match.start() + 750]
            if re.search(r"20(?:2[1-9]|3\d)", excerpt):
                return excerpt
    return ""


def inspect_person(item, terms):
    name, url = item
    try:
        final_url, page = fetch(url)
    except Exception as exc:
        return {"name": name, "url": url, "error": type(exc).__name__ + ": " + str(exc)[:120]}
    text = " ".join(" ".join(page.words).split())
    focus, scope = research_text(text)
    matched = [term for term in terms if term.casefold() in focus.casefold()]
    return {"name": name, "url": final_url, "matched_terms": matched,
            "match_scope": scope, "snippets": {term: research_excerpt(focus, term) for term in matched},
            "role_excerpt": next((research_excerpt(text, role) for role in ("博士生导师", "硕士生导师", "教授", "研究员")
                                  if role in text), ""),
            "master_supervisor_explicit": profile_role(text, name, "master"),
            "doctor_supervisor_explicit": profile_role(text, name, "doctor"),
            "paper_excerpt": paper_excerpt(text)}


def scan(url, terms, max_profiles=150, max_results=25, degree=None):
    if not url.startswith(("http://", "https://")):
        raise ValueError("Faculty directory URL must use HTTP(S)")
    final_url, page = fetch(url)
    queue = [(final_url, page, 0)]
    visited = set()
    links = []
    seen_profiles = set()
    best_source = final_url
    best_count = 0
    directory_pages = []
    navigation_errors = []
    while queue and len(visited) < 26:
        page_url, current_page, depth = queue.pop(0)
        if page_url in visited:
            continue
        visited.add(page_url)
        directory_pages.append(page_url)
        page_links = teacher_links(current_page, page_url, url)
        if len(page_links) > best_count:
            best_source, best_count = page_url, len(page_links)
        for item in page_links:
            if item[1] not in seen_profiles:
                seen_profiles.add(item[1])
                links.append(item)
        child_urls = faculty_pagination(current_page, page_url, url) if depth < 3 else []
        if depth < 2:
            child_urls = faculty_navigation(current_page, page_url, url) + child_urls
        for child_url in child_urls:
            if child_url in visited or any(child_url == queued[0] for queued in queue):
                continue
            try:
                child_url, child_page = fetch(child_url)
            except Exception as exc:
                navigation_errors.append({"url": child_url, "error": type(exc).__name__ + ": " + str(exc)[:120]})
                continue
            queue.append((child_url, child_page, depth + 1))
    links_found = len(links)
    roster = [{"name": name, "url": profile_url} for name, profile_url in links]
    links = links[:max_profiles]
    with ThreadPoolExecutor(max_workers=min(4, len(links) or 1)) as pool:
        rows = list(pool.map(lambda item: inspect_person(item, terms), links))
    errors = [{"name": row["name"], "url": row["url"], "error": row["error"]}
              for row in rows if "error" in row]
    matches = [row for row in rows if row.get("matched_terms")]
    role_field = {"master": "master_supervisor_explicit",
                  "doctor": "doctor_supervisor_explicit"}.get(degree)
    matches.sort(key=lambda row: (not row.get(role_field, False) if role_field else False,
                                  row["match_scope"] != "research_section", -len(row["matched_terms"]), row["name"]))
    official_role_paper_leads = []
    official_role_without_dated_paper = []
    seen_ready_names = set()
    if role_field:
        for row in matches:
            if row.get(role_field) and row.get("paper_excerpt") and row["name"] not in seen_ready_names:
                seen_ready_names.add(row["name"])
                official_role_paper_leads.append(row)
            elif row.get(role_field) and not row.get("paper_excerpt"):
                official_role_without_dated_paper.append(row)
    return {"source": best_source, "directory_pages": directory_pages,
            "navigation_errors": navigation_errors[:10], "terms": terms, "links_found": links_found,
            "profiles_scanned": len(rows), "unscanned_links": links_found - len(rows),
            "unvisited_directory_pages": len(queue),
            "official_role_paper_leads": official_role_paper_leads[:max_results],
            "official_role_without_dated_paper": official_role_without_dated_paper[:max_results],
            "omitted_official_role_paper_leads": max(0, len(official_role_paper_leads) - max_results),
            "omitted_official_role_without_dated_paper": max(0, len(official_role_without_dated_paper) - max_results),
            "matches_found": len(matches), "matches": matches[:max_results],
            "omitted_matches": max(0, len(matches) - max_results), "roster": roster,
            "errors": errors[:10],
            "note": "official_role_paper_leads have an explicit degree role and dated paper excerpt; official_role_without_dated_paper have the role but need a targeted paper check. Neither group is certified until shortlist_gate. Roster lists all discovered faculty links; absence from matches does not mean absence from the roster or unscanned."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="Official department faculty-directory URL")
    parser.add_argument("--terms", required=True, help="Comma-separated research keywords")
    parser.add_argument("--max-profiles", type=int, default=150)
    parser.add_argument("--max-results", type=int, default=25)
    parser.add_argument("--degree", choices=("master", "doctor"), help="Prioritize profiles explicitly stating this degree-supervisor role")
    args = parser.parse_args()
    if not (1 <= args.max_profiles <= 150 and 1 <= args.max_results <= 50):
        parser.error("max-profiles must be 1–150 and max-results 1–50")
    terms = list(dict.fromkeys(x.strip() for x in args.terms.split(",") if x.strip()))
    if not terms:
        parser.error("at least one term is required")
    print(json.dumps(scan(args.url, terms, args.max_profiles, args.max_results, args.degree), ensure_ascii=False,
                     separators=(",", ":")))


if __name__ == "__main__":
    main()
