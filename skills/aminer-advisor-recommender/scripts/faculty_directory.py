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
        profile_path = re.search(r"(?:/page\.(?:htm|html|php)$|/(?:person|people|teachers?|faculty|staff|szll)/|/info/\d+/\d+\.htm$)", path, re.I)
        faculty_home = (urllib.parse.urlsplit(url).hostname or "").startswith("faculty.") and path.endswith("/main.htm")
        if not profile_path and not faculty_home:
            continue
        seen.add(url)
        links.append((name, url))
    return links


def faculty_navigation(page, base_url, root, unit_terms=()):
    """Find faculty lists and directly linked institute sites at the same university."""
    seen = set()
    links = []
    for label, href in page.links:
        label = label.strip(" -·\t\r\n")
        url = public_link(base_url, href)
        if not url or not same_university_domain(url, root) or url == base_url or url in seen:
            continue
        path = urllib.parse.urlsplit(url).path.casefold()
        direct = label.casefold() in {"教师", "师资", "在职教师", "教师队伍", "师资力量", "师资队伍", "师资中心",
                                       "教师简介", "教师名单", "教职工名单", "教职工名录",
                                       "导师简介", "博导简介", "硕导简介", "教授", "副教授",
                                       "博导名单", "硕导名单", "faculty", "teachers"}
        category = label == "硕博导师" or label.endswith(("研究所", "研究院", "工程系"))
        unit_index = label in {"院部设置", "院系设置", "学院设置", "院系机构", "专业学院"}
        target_unit = (bool(unit_terms) and len(label) <= 35
                       and not re.search(r"(新闻|报告|论坛|通知|招聘|招生|会议|讲座|校友|研究生)", label)
                       and any(term in label for term in unit_terms)
                       and any(kind in label for kind in ("学院", "研究院", "研究所", "学部")))
        # University college sites often link the actual institute's separate
        # official subdomain as “研究所主页”; following that link is evidence-based
        # navigation, unlike guessing hostnames or faculty URL paths.
        institute_home = label.endswith(("研究所主页", "研究院主页", "研究中心主页"))
        if not direct and not category and not institute_home and not unit_index and not target_unit:
            continue
        if not direct and not institute_home and not unit_index and not target_unit and not re.search(r"/(?:szdw|szll|faculty|teachers?|staff|people|sz|ys)/", path):
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


def personal_homepage_key(text):
    """Use a person-specific homepage to join duplicate official bios safely."""
    match = re.search(r"个人主页\s*[:：]?\s*(?:https?://)?"
                      r"([a-z0-9.-]+\.edu\.cn/[a-z0-9_./~-]+)", text, re.I)
    if not match:
        return ""
    key = match.group(1).lower().rstrip("/")
    path = key.split("/", 1)[1]
    if path in {"index.htm", "index.html", "main.htm", "home", "faculty", "person"}:
        return ""
    return key


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
            "personal_homepage_key": personal_homepage_key(text),
            "role_excerpt": next((research_excerpt(text, role) for role in ("博士生导师", "硕士生导师", "教授", "研究员")
                                  if role in text), ""),
            "master_supervisor_explicit": profile_role(text, name, "master"),
            "doctor_supervisor_explicit": profile_role(text, name, "doctor"),
            "paper_excerpt": paper_excerpt(text)}


def scan(url, terms, max_profiles=150, max_results=25, degree=None, unit_terms=()):
    if not url.startswith(("http://", "https://")):
        raise ValueError("Faculty directory URL must use HTTP(S)")
    fallback_from = ""
    http_retry_from = ""
    parsed_url = urllib.parse.urlsplit(url)
    host = parsed_url.hostname or ""
    domain = university_domain(host)
    college_host = domain.endswith(".edu.cn") and host not in {domain, "www." + domain}
    first_error = None
    try:
        final_url, page = fetch(url)
    except Exception as exc:
        first_error = exc
        final_url, page = "", None
    redirected_to_root = bool(page and college_host and
                              (urllib.parse.urlsplit(final_url).hostname or "") in {domain, "www." + domain}
                              and urllib.parse.urlsplit(final_url).path in {"", "/"})
    if parsed_url.scheme == "https" and college_host and (page is None or redirected_to_root):
        http_url = urllib.parse.urlunsplit(("http", parsed_url.netloc, parsed_url.path,
                                           parsed_url.query, parsed_url.fragment))
        try:
            alternate_url, alternate_page = fetch(http_url)
            if (same_university_domain(alternate_url, url) and
                    not ((urllib.parse.urlsplit(alternate_url).hostname or "") in {domain, "www." + domain}
                         and urllib.parse.urlsplit(alternate_url).path in {"", "/"})):
                final_url, page = alternate_url, alternate_page
                http_retry_from = url
        except Exception:
            pass
    if page is None:
        if not college_host:
            raise first_error or ValueError("official university page could not be fetched")
        # A guessed or temporarily unavailable college host must not make the
        # whole university look unreachable. Follow the university's real
        # unit index from its official root instead.
        root_url = "https://www." + domain + "/"
        final_url, page = fetch(root_url)
        fallback_from = url
    elif redirected_to_root and not http_retry_from:
        fallback_from = url
    queue = [(final_url, page, 0)]
    visited = set()
    links = []
    seen_profiles = set()
    best_source = final_url
    best_count = 0
    directory_pages = []
    navigation_errors = []
    unit_directory_links = []
    seen_unit_links = set()
    while queue and len(visited) < 26:
        page_url, current_page, depth = queue.pop(0)
        if page_url in visited:
            continue
        visited.add(page_url)
        directory_pages.append(page_url)
        page_links = teacher_links(current_page, page_url, url)
        for label, href in current_page.links:
            unit_url = public_link(page_url, href)
            if (unit_url and same_university_domain(unit_url, url)
                    and len(label) <= 35
                    and any(kind in label for kind in ("学院", "研究院", "研究所", "学部"))
                    and not re.search(r"(新闻|报告|论坛|通知|招聘|招生|会议|讲座|校友)", label)
                    and unit_url not in seen_unit_links):
                seen_unit_links.add(unit_url)
                unit_directory_links.append({"label": label, "url": unit_url})
        if len(page_links) > best_count:
            best_source, best_count = page_url, len(page_links)
        for item in page_links:
            if item[1] not in seen_profiles:
                seen_profiles.add(item[1])
                links.append(item)
        child_urls = faculty_pagination(current_page, page_url, url) if depth < 4 else []
        if depth < 4:
            child_urls = faculty_navigation(current_page, page_url, url, unit_terms) + child_urls
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
    official_split_evidence_leads = []
    seen_ready_names = set()
    if role_field:
        by_identity = {}
        for row in rows:
            if row.get("personal_homepage_key"):
                by_identity.setdefault((row["name"], row["personal_homepage_key"]), []).append(row)
        for (name, homepage), bios in by_identity.items():
            if len(bios) < 2 or not any(bio.get("matched_terms") for bio in bios):
                continue
            role_bio = next((bio for bio in bios if bio.get(role_field)), None)
            paper_bio = next((bio for bio in bios if bio.get("paper_excerpt") and
                              bio["url"] != (role_bio or {}).get("url")), None)
            if role_bio and paper_bio:
                official_split_evidence_leads.append({
                    "name": name, "personal_homepage_key": homepage,
                    "official_profile_url": paper_bio["url"],
                    "official_supervisor_url": role_bio["url"],
                    "paper_excerpt": paper_bio["paper_excerpt"],
                    "matched_terms": list(dict.fromkeys(role_bio.get("matched_terms", []) +
                                                        paper_bio.get("matched_terms", []))),
                })
        for row in matches:
            if row.get(role_field) and row.get("paper_excerpt") and row["name"] not in seen_ready_names:
                seen_ready_names.add(row["name"])
                official_role_paper_leads.append(row)
            elif row.get(role_field) and not row.get("paper_excerpt"):
                official_role_without_dated_paper.append(row)
    return {"source": best_source, "fallback_from": fallback_from,
            "http_retry_from": http_retry_from,
            "directory_pages": directory_pages,
            "unit_directory_links": unit_directory_links[:60],
            "navigation_errors": navigation_errors[:10], "terms": terms, "links_found": links_found,
            "profiles_scanned": len(rows), "unscanned_links": links_found - len(rows),
            "unvisited_directory_pages": len(queue),
            "official_role_paper_leads": official_role_paper_leads[:max_results],
            "official_role_without_dated_paper": official_role_without_dated_paper[:max_results],
            "official_split_evidence_leads": official_split_evidence_leads[:max_results],
            "omitted_official_split_evidence_leads": max(0, len(official_split_evidence_leads) - max_results),
            "omitted_official_role_paper_leads": max(0, len(official_role_paper_leads) - max_results),
            "omitted_official_role_without_dated_paper": max(0, len(official_role_without_dated_paper) - max_results),
            "matches_found": len(matches), "matches": matches[:max_results],
            "omitted_matches": max(0, len(matches) - max_results), "roster": roster,
            "errors": errors[:10],
            "note": "official_split_evidence_leads join separate official bios only when they share an exact name and person-specific homepage; pass the paper page and role page separately to shortlist_gate. All lead groups still need the gate and topic review. Roster lists all discovered faculty links; absence from matches does not mean absence from the roster or unscanned."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="Official department faculty-directory URL")
    parser.add_argument("--terms", required=True, help="Comma-separated research keywords")
    parser.add_argument("--max-profiles", type=int, default=150)
    parser.add_argument("--max-results", type=int, default=25)
    parser.add_argument("--degree", choices=("master", "doctor"), help="Prioritize profiles explicitly stating this degree-supervisor role")
    parser.add_argument("--unit-terms", default="",
                        help="When starting at a university homepage, follow official college links matching these comma-separated names")
    args = parser.parse_args()
    if not (1 <= args.max_profiles <= 150 and 1 <= args.max_results <= 50):
        parser.error("max-profiles must be 1–150 and max-results 1–50")
    terms = list(dict.fromkeys(x.strip() for x in args.terms.split(",") if x.strip()))
    unit_terms = list(dict.fromkeys(x.strip() for x in args.unit_terms.split(",") if x.strip()))
    if not terms:
        parser.error("at least one term is required")
    print(json.dumps(scan(args.url, terms, args.max_profiles, args.max_results, args.degree, unit_terms), ensure_ascii=False,
                     separators=(",", ":")))


if __name__ == "__main__":
    main()
