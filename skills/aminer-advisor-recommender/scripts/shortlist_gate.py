#!/usr/bin/env python3
"""Fail-closed source check for a proposed advisor shortlist.

This checks identity, current program, supervisor evidence and one attributed,
dated paper per person. Research fit and current openings still need judgment.
"""

from __future__ import annotations

import argparse
from datetime import date
from html.parser import HTMLParser
import json
import os
import re
import sys
import urllib.parse
import urllib.request

from aminer_client import AMinerClient, unwrap


class PageText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.skip:
            self.skip -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip and data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        return " ".join(" ".join(self.parts).split())


def normalized(value: str) -> str:
    return re.sub(r"[^\w\u3400-\u9fff]", "", value.casefold())


def official_host(url: str, school_domain: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    school_domain = school_domain.lower().strip(".")
    return (parts.scheme in {"http", "https"} and not parts.username and not parts.password
            and school_domain.endswith(".edu.cn") and
            (host == school_domain or host.endswith("." + school_domain)))


def fetch_official(url: str, school_domain: str) -> str:
    if not official_host(url, school_domain):
        raise ValueError("URL is not on the declared university's edu.cn domain")
    class UniversityOnlyRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            if not official_host(newurl, school_domain):
                raise ValueError("official URL redirected outside the university domain")
            return super().redirect_request(request, fp, code, msg, headers, newurl)

    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 AdvisorEvidenceGate/1.0"})
    with urllib.request.build_opener(UniversityOnlyRedirect()).open(request, timeout=18) as response:
        if not official_host(response.url, school_domain):
            raise ValueError("official URL redirected outside the university domain")
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("official page exceeds 2 MB")
        encoding = response.headers.get_content_charset() or "utf-8"
    page = PageText()
    page.feed(raw.decode(encoding, errors="replace"))
    return page.text()


def record_data(client: AMinerClient, api: str, record_id: str) -> dict:
    # Raw AMiner detail endpoints take `id`. The shared eval wrapper exposes
    # `person_id`/`paper_id`, but those names are not accepted by this client.
    rows = unwrap(client.call(api, {"id": record_id}))
    if len(rows) != 1 or str(rows[0].get("id") or "") != record_id:
        raise ValueError(f"{api} did not return the requested ID")
    return rows[0]


def has_role_for_name(page: str, name: str, degree: str, *, roster: bool) -> bool:
    width = 300 if roster else 1500
    for match in re.finditer(re.escape(name), page):
        nearby = page[max(0, match.start() - 100):match.end() + width]
        if degree == "master" and ("硕士生导师" in nearby or "硕导" in nearby or
                                   re.search(r"(?:报考本人|招收|招生).{0,25}硕士研究生", nearby)):
            return True
        if degree == "doctor" and ("博士生导师" in nearby or "博导" in nearby or
                                   re.search(r"(?:报考本人|招收|招生).{0,25}博士研究生", nearby)):
            return True
    return False


def official_paper_match(page: str, title: str, year: int) -> bool:
    title_key = normalized(title)
    page_key = normalized(page)
    start = page_key.find(title_key)
    return bool(title_key and start >= 0 and
                str(year) in page_key[max(0, start - 30):start + len(title_key) + 120])


def paper_affiliation_match(authors: list[dict], person_id: str, row: dict,
                            client: AMinerClient) -> str | None:
    """Resolve a Chinese school label to the same AMiner org ID and English name."""
    target_authors = [author for author in authors if str(author.get("id") or "") == person_id]
    school_names = [str(name) for name in row.get("school_names", []) if str(name).strip()]
    school_domain = str(row.get("school_domain") or "")
    for author in target_authors:
        affiliation = normalized(str(author.get("org") or ""))
        if (school_domain.split(".")[0] in affiliation or
                any(normalized(name) in affiliation for name in school_names if normalized(name))):
            return "declared school name in paper author affiliation"
    chinese_name = next((name for name in school_names if re.search(r"[\u3400-\u9fff]", name)), None)
    if not chinese_name:
        return None
    try:
        organizations = unwrap(client.call("org_search", {"orgs": [chinese_name]}))
    except Exception:
        return None
    for organization in organizations:
        org_id = str(organization.get("org_id") or "")
        canonical = normalized(str(organization.get("org_name") or ""))
        if not org_id or len(canonical) < 8:
            continue
        for author in target_authors:
            if (str(author.get("orgid") or "") == org_id and
                    canonical in normalized(str(author.get("org") or ""))):
                return "AMiner school org ID and canonical English name in paper author affiliation"
    return None


def verify_candidate(row: dict, program_terms: list[str], degree: str,
                     client: AMinerClient | None) -> dict:
    name = str(row.get("name") or "").strip()
    domain = str(row.get("school_domain") or "").strip()
    result = {"name": name, "approved": False, "problems": [], "verified": {}}
    problems = result["problems"]
    if not name or not domain:
        problems.append("name and school_domain are required")
        return result
    if row.get("band") or row.get("admission_probability"):
        problems.append("individual admission band/probability is unsupported")

    person_id = str(row.get("person_id") or "")
    english_alias_pending = ""
    if person_id:
        if client is None:
            problems.append("AMiner credential is missing for person ID check")
        else:
            try:
                person = record_data(client, "person_detail", person_id)
                chinese = str(person.get("name_zh") or "").strip()
                english = str(person.get("name") or "").strip()
                canonical = chinese or english
                result["verified"]["canonical_name"] = canonical
                if chinese and name != chinese:
                    problems.append(f"name does not match AMiner person ID; use {canonical}")
                elif not chinese and normalized(name) != normalized(english):
                    if re.search(r"[\u3400-\u9fff]", name):
                        english_alias_pending = english
                    else:
                        problems.append(f"name does not match AMiner person ID; use {canonical}")
            except Exception as exc:
                problems.append(f"person ID could not be checked: {exc}")

    url = str(row.get("official_profile_url") or "")
    page = ""
    if not url:
        problems.append("official faculty profile URL is missing")
    else:
        try:
            page = fetch_official(url, domain)
            result["verified"]["official_profile_url"] = url
            if name not in page[:2500]:
                problems.append("exact name is absent from the official profile heading/body")
            if program_terms and not any(term in page[:2500] for term in program_terms):
                problems.append("requested program is absent from the official profile")
            if english_alias_pending:
                if normalized(english_alias_pending) not in normalized(page):
                    problems.append("AMiner English profile cannot be bound to the official Chinese-name page")
                else:
                    result["verified"]["canonical_name"] = name
        except Exception as exc:
            problems.append(f"official faculty profile could not be checked: {exc}")

    role_verified = has_role_for_name(page, name, degree, roster=False)
    role_url = str(row.get("official_supervisor_url") or "")
    if role_url:
        try:
            role_page = fetch_official(role_url, domain)
            result["verified"]["official_supervisor_url"] = role_url
            role_verified = role_verified or has_role_for_name(role_page, name, degree, roster=True)
        except Exception as exc:
            if role_verified:
                result.setdefault("warnings", []).append(f"optional supervisor roster unavailable; profile role used: {exc}")
            else:
                problems.append(f"official supervisor roster could not be checked: {exc}")
    if not role_verified:
        problems.append(f"official {degree} supervisor evidence is missing")

    paper = row.get("paper") or {}
    title = str(paper.get("title") or "").strip()
    paper_id = str(paper.get("aminer_id") or "")
    year = paper.get("year")
    if not title or not isinstance(year, int) or not (date.today().year - 5 <= year <= date.today().year):
        problems.append("one titled direction paper dated within the past five years is required")
    else:
        official_paper = bool(page and official_paper_match(page, title, year))
        if paper_id and client and person_id:
            try:
                source = record_data(client, "paper_detail", paper_id)
                authors = source.get("authors") or []
                paper_problem_count = len(problems)
                if (normalized(str(source.get("title") or "")) != normalized(title)
                        or source.get("year") != year):
                    problems.append("paper title/year does not match the AMiner paper ID")
                if not any(str(a.get("id") or "") == person_id for a in authors):
                    problems.append("AMiner paper does not attribute the work to this person ID")
                affiliation_method = paper_affiliation_match(authors, person_id, row, client)
                if not affiliation_method:
                    problems.append("paper author affiliation does not establish the requested university")
                if len(problems) == paper_problem_count:
                    result["verified"]["aminer_paper_id"] = paper_id
                    result["verified"]["paper_affiliation"] = affiliation_method
            except Exception as exc:
                if official_paper:
                    result.setdefault("warnings", []).append(f"AMiner paper check unavailable; official faculty paper used: {exc}")
                else:
                    problems.append(f"AMiner paper could not be checked: {exc}")
        elif official_paper:
            result["verified"]["paper_on_official_profile"] = title
        elif paper_id and not person_id:
            problems.append("AMiner paper ID alone does not attribute a paper to this person; official page title/year absent")
        elif page:
            problems.append("paper title/year is absent or not adjacent on the official faculty profile")
        else:
            problems.append("paper attribution could not be checked")

    result["approved"] = not problems
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates-json", required=True, help="JSON array of proposed main-list rows")
    parser.add_argument("--program-terms", default="", help="Exact official program names, only when the user specified a program or college")
    parser.add_argument("--allowed-domains", required=True, help="Comma-separated edu.cn domains of the requested schools")
    parser.add_argument("--degree", choices=("master", "doctor"), required=True)
    args = parser.parse_args()
    candidates = json.loads(args.candidates_json)
    if not isinstance(candidates, list) or not candidates or not all(isinstance(c, dict) for c in candidates):
        parser.error("--candidates-json must be a nonempty JSON array of objects")
    terms = [s.strip() for s in args.program_terms.split(",") if s.strip()]
    domains = [s.strip().lower() for s in args.allowed_domains.split(",") if s.strip()]
    if not domains or any(not domain.endswith(".edu.cn") for domain in domains):
        parser.error("--allowed-domains needs university edu.cn domains")
    token = os.getenv("AMINER_API_KEY", "")
    client = AMinerClient(token, timeout=18) if token else None
    checked = []
    for row in candidates:
        if str(row.get("school_domain") or "").lower() not in domains:
            checked.append({"name": row.get("name"), "approved": False,
                            "problems": ["school domain is outside the requested institutions"], "verified": {}})
        else:
            checked.append(verify_candidate(row, terms, args.degree, client))
    print(json.dumps({"approved": [r for r in checked if r["approved"]],
                      "leads": [r for r in checked if not r["approved"]],
                      "aminer_cost": client.cost.summary() if client else None}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
