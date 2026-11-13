#!/usr/bin/env python3
"""Fail-closed source check for a proposed advisor shortlist.

This checks identity, an official faculty page and one attributed, dated paper
per person. A specified degree additionally requires an official supervisor-role
statement. Current employment, current-cycle eligibility, research fit and
openings still need separate evidence.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
from html.parser import HTMLParser
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from aminer_client import AMinerClient, unwrap


CHINA_TZ = ZoneInfo("Asia/Shanghai")
HARD_RECOMMENDATION_FAILURES = (
    "paper author affiliation does not establish the requested university",
    "AMiner paper does not attribute the work to this person ID",
    "paper title/year does not match the AMiner paper ID",
    "person ID cannot be uniquely resolved",
    "name does not match AMiner person ID",
    "AMiner English profile cannot be bound",
    "school domain is outside the requested institutions",
)


def partition_checked(checked: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Separate missing-source leads from hard identity/attribution failures."""
    approved: list[dict] = []
    leads: list[dict] = []
    rejected: list[dict] = []
    for row in checked:
        if row.get("approved"):
            approved.append(row)
        elif any(any(marker in str(problem) for marker in HARD_RECOMMENDATION_FAILURES)
                 for problem in row.get("problems", [])):
            row["recommendation_eligible"] = False
            row["rejection_type"] = "identity_or_paper_attribution"
            rejected.append(row)
        else:
            leads.append(row)
    return approved, leads, rejected


def today_in_china() -> date:
    return datetime.now(CHINA_TZ).date()


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


def school_name_list(row: dict) -> list[str]:
    """Accept the documented list and a single school label without splitting it."""
    raw = row.get("school_names")
    values = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return [value.strip() for value in values if isinstance(value, str) and value.strip()]


UMBRELLA_FOCUS_TERMS = {"ai", "artificial intelligence", "machine learning",
                        "deep learning", "multimodal", "多模态", "人工智能", "机器学习",
                        "mllm", "vlm", "lvlm", "vision-language", "视觉语言",
                        "image-text", "text-image", "图文", "跨模态",
                        "robot", "robotics", "机器人", "具身智能", "embodied ai",
                        "embodied intelligence"}


def title_focus_matches(title: str, terms: list[str]) -> list[str]:
    title_key = normalized(title)
    matched = []
    for term in terms:
        term_key = normalized(term)
        if not term_key or term.casefold() in UMBRELLA_FOCUS_TERMS:
            continue
        # Image-text and text-image describe the same modality pair, but a
        # generic multimodal token never establishes task-specific overlap.
        words = re.split(r"[-\s]+", term.casefold())
        reversed_pair = normalized("".join(reversed(words))) if len(words) == 2 else ""
        if term_key in title_key or (reversed_pair and reversed_pair in title_key):
            matched.append(term)
    return matched


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


def fetch_profile_with_retry(url: str, school_domain: str, name: str,
                             degree: str, has_roster: bool) -> str:
    """Retry one transient empty/partial official profile; never waive evidence."""
    best = ""
    last_error: Exception | None = None
    for _ in range(2):
        try:
            page = fetch_official(url, school_domain)
        except Exception as exc:
            last_error = exc
            continue
        role_ok = degree == "unspecified" or has_role_for_name(page, name, degree, roster=False)
        best_role_ok = degree == "unspecified" or has_role_for_name(best, name, degree, roster=False)
        score = (name in page[:2500], role_ok, len(page))
        best_score = (name in best[:2500], best_role_ok, len(best))
        if score > best_score:
            best = page
        if score[0] and (score[1] or has_roster):
            return page
    if best:
        return best
    raise last_error or ValueError("official profile returned no content")


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
                                   re.search(r"硕士生?\s*[/、和及与]\s*博士生?导师", nearby) or
                                   re.search(r"(?:报考本人|招收|招募|招生|欢迎.{0,5}(?:报考|申请)).{0,25}硕士(?:研究生|生)", nearby) or
                                   re.search(r"\b(?:looking for|recruiting|accepting).{0,80}\bmaster'?s?\s+(?:students?|applicants?)\b", nearby, re.I)):
            return True
        if degree == "doctor" and ("博士生导师" in nearby or "博导" in nearby or
                                   re.search(r"(?:报考本人|招收|招募|招生|欢迎.{0,5}(?:报考|申请)).{0,25}博士(?:研究生|生)", nearby) or
                                   re.search(r"\b(?:looking for|recruiting|accepting).{0,80}\b(?:ph\.?d\.?s?|doctoral)(?:\s+(?:students?|applicants?|candidates?))?\b", nearby, re.I)):
            return True
    return False


def official_paper_match(page: str, title: str, year: int) -> bool:
    title_key = normalized(title)
    page_key = normalized(page)
    start = page_key.find(title_key)
    return bool(title_key and start >= 0 and
                str(year) in page_key[max(0, start - 30):start + len(title_key) + 120])


def profile_title_context(page: str, name: str) -> str:
    """Return a title close to this name on the individual official page."""
    title = re.compile(r"(?:长聘)?(?:副教授|助理教授|教授|副研究员|助理研究员|研究员|讲师)"
                       r"|\b(?:Assistant|Associate|Research|Full)?\s*Professor\b|\b(?:Researcher|Lecturer)\b", re.I)
    names = [name]
    parts = name.split()
    if len(parts) == 2 and all(part.isascii() for part in parts):
        names.append(" ".join(reversed(parts)))
    for candidate in names:
        for match in re.finditer(re.escape(candidate), page[:6000], re.I):
            after = page[match.end():match.end() + 100]
            found = title.search(after)
            if found and found.start() <= 50:
                return page[match.start():match.end() + min(len(after), found.end() + 30)]
    return ""


def displayed_page_date(page: str) -> str | None:
    """Read a publication/update date near an official page's heading, if shown."""
    match = re.search(r"(?:时间|发布日期|更新日期)\s*[:：]\s*(20\d{2}-\d{2}-\d{2})",
                      page[:2500])
    return match.group(1) if match else None


def academic_title_near_name(context: str) -> str | None:
    """Extract only a title shown in the person's own heading/bio excerpt."""
    match = re.search(r"长聘教授|特聘教授|副教授|助理教授|教授|副研究员|助理研究员|研究员|讲师"
                      r"|\b(?:Assistant|Associate|Research|Full)?\s*Professor\b|\b(?:Researcher|Lecturer)\b",
                      context, re.I)
    return match.group(0).strip() if match else None


def date_over_two_years_old(value: str) -> bool:
    try:
        return (today_in_china() - date.fromisoformat(value)).days > 730
    except ValueError:
        return False


def compact_university_abbreviation(name: str) -> str:
    """Match the common journal byline form without accepting a different org ID."""
    value = re.sub(r"\bUniversity of\b", "Univ", name, flags=re.I)
    value = re.sub(r"\band\b", "&", value, flags=re.I)
    value = re.sub(r"\bAeronautics\b", "Aeronaut", value, flags=re.I)
    value = re.sub(r"\bAstronautics\b", "Astronaut", value, flags=re.I)
    return re.sub(r"\bTechnology\b", "Technol", value, flags=re.I)


def paper_affiliation_match(authors: list[dict], person_id: str, row: dict,
                            client: AMinerClient) -> str | None:
    """Resolve a Chinese school label to the same AMiner org ID and English name."""
    target_authors = [author for author in authors if str(author.get("id") or "") == person_id]
    school_names = school_name_list(row)
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
        org_name = str(organization.get("org_name") or "")
        canonical = normalized(org_name)
        if not org_id or len(canonical) < 4:
            continue
        # AMiner often abbreviates "University" as "Univ" in paper author
        # affiliations while org_search returns the full canonical label.
        # Require the same org ID as well as one text form to avoid accepting
        # a contradictory author affiliation solely because an ID matches.
        english_forms = []
        for school_name in school_names:
            if not re.search(r"\bUniversity\b", school_name, re.I):
                continue
            english_forms.extend((
                school_name,
                re.sub(r"\bUniversity\b", "Univ", school_name, flags=re.I),
                re.sub(r"\bUniversity of Science and Technology\b",
                       "Univ Sci & Technol", school_name, flags=re.I),
                compact_university_abbreviation(school_name),
            ))
        canonical_forms = {canonical, normalized(re.sub(r"\bUniversity\b", "Univ", org_name, flags=re.I)),
                           normalized(compact_university_abbreviation(org_name)),
                           *(normalized(value) for value in english_forms)}
        for author in target_authors:
            author_org_id = str(author.get("orgid") or "")
            if not any(form in normalized(str(author.get("org") or ""))
                       for form in canonical_forms if len(form) >= 8):
                continue
            if author_org_id == org_id:
                return "AMiner school org ID and canonical English name/abbreviation in paper author affiliation"
            if not author_org_id:
                # Some paper_detail author records omit orgid altogether even
                # when their affiliation explicitly names the normalized
                # university. The exact person ID and live official faculty
                # page are checked separately by verify_candidate.
                return "AMiner canonical English school name/abbreviation in paper author affiliation; author org ID absent"
    return None


def verify_candidate(row: dict, program_terms: list[str], degree: str,
                     client: AMinerClient | None, focus_terms: list[str] | None = None) -> dict:
    name = str(row.get("name") or "").strip()
    domain = str(row.get("school_domain") or "").strip()
    result = {"name": name, "approved": False, "problems": [],
              "verified": {"current_faculty_and_cycle_status": "not_verified_by_gate"}}
    problems = result["problems"]
    if not name or not domain:
        problems.append("name and school_domain are required")
        return result
    if row.get("band") or row.get("admission_probability"):
        problems.append("individual admission band/probability is unsupported")

    paper = row.get("paper") if isinstance(row.get("paper"), dict) else {}
    paper_id = str(paper.get("aminer_id") or "")
    person_id = str(row.get("person_id") or "")
    if paper_id and client and not school_name_list(row):
        problems.append("school_names is required for AMiner paper affiliation; supply the first-level university name")
        return result
    # A blocked or missing official page makes approval impossible. Check it
    # before any paid person/paper detail call, then reuse the fetched text.
    url = str(row.get("official_profile_url") or "")
    if not url:
        problems.append("official faculty profile URL is missing")
        return result
    try:
        page = fetch_profile_with_retry(url, domain, name, degree,
                                        bool(row.get("official_supervisor_url")))
    except Exception as exc:
        problems.append(f"official faculty profile could not be checked: {exc}")
        return result
    prefetched_paper = None
    if not person_id and paper_id and client:
        school_names = school_name_list(row)
        school = next((value for value in school_names if re.search(r"[\u3400-\u9fff]", value)),
                      school_names[0] if school_names else "")
        if school:
            try:
                prefetched_paper = record_data(client, "paper_detail", paper_id)
                author_ids = {str(author.get("id") or "")
                              for author in prefetched_paper.get("authors") or []}
                people = unwrap(client.call("person_search", {"name": name, "org": school, "size": 10}))
                matching_ids = {str(person.get("id") or "") for person in people
                                if str(person.get("id") or "") in author_ids
                                and (str(person.get("name_zh") or "").strip() == name or
                                     normalized(str(person.get("name") or "")) == normalized(name))}
                if len(matching_ids) == 1:
                    person_id = matching_ids.pop()
                    result["verified"]["resolved_person_id"] = person_id
                else:
                    problems.append("person ID cannot be uniquely resolved from exact name, school, and paper authors")
            except Exception as exc:
                problems.append(f"person ID resolution from paper failed: {exc}")
        else:
            problems.append("school_names is required to resolve a paper author without person_id")
    english_alias_pending = ""
    official_english_alias = ""
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
                if chinese and name == chinese and english:
                    official_english_alias = english
                if chinese and name != chinese:
                    if normalized(name) == normalized(english):
                        result["verified"]["official_page_english_name"] = english
                    else:
                        problems.append(f"name does not match AMiner person ID; use {canonical}")
                elif not chinese and normalized(name) != normalized(english):
                    if re.search(r"[\u3400-\u9fff]", name):
                        english_alias_pending = english
                    else:
                        problems.append(f"name does not match AMiner person ID; use {canonical}")
            except Exception as exc:
                problems.append(f"person ID could not be checked: {exc}")

    profile_name = name
    profile_date = None
    profile_title = None
    if page:
        try:
            result["verified"]["official_profile_url"] = url
            if name not in page[:2500] and official_english_alias and \
                    normalized(official_english_alias) in normalized(page[:2500]):
                # The ID-verified AMiner record binds the Chinese and English
                # names; the university page must still state an actual role.
                profile_name = official_english_alias
                result["verified"]["official_page_english_name"] = official_english_alias
            title_context = profile_title_context(page, profile_name)
            if title_context:
                result["verified"]["official_profile_title_context"] = title_context
                profile_title = academic_title_near_name(title_context)
                if profile_title:
                    result["verified"]["official_profile_title"] = profile_title
            profile_date = displayed_page_date(page)
            if profile_date:
                result["verified"]["official_profile_page_date"] = profile_date
                if date_over_two_years_old(profile_date):
                    result.setdefault("warnings", []).append(
                        "official profile displays a date over two years old; verify current faculty and degree status")
            if profile_name not in page[:2500]:
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

    role_verified = has_role_for_name(page, profile_name, degree, roster=False)
    role_url = str(row.get("official_supervisor_url") or "")
    if role_url and degree != "unspecified":
        try:
            role_page = fetch_official(role_url, domain)
            result["verified"]["official_supervisor_url"] = role_url
            role_date = displayed_page_date(role_page)
            if role_date:
                result["verified"]["supervisor_role_page_date"] = role_date
                if date_over_two_years_old(role_date):
                    result.setdefault("warnings", []).append(
                        "separate supervisor-role page is over two years old; verify current-cycle eligibility")
            role_title = academic_title_near_name(profile_title_context(role_page, name))
            if role_title:
                result["verified"]["supervisor_role_page_title"] = role_title
                if profile_title and role_title != profile_title:
                    if profile_date and role_date and profile_date > role_date:
                        result["verified"]["preferred_title_source"] = url
                        result.setdefault("warnings", []).append(
                            f"title conflict: newer personal profile says {profile_title}; older supervisor-role page says {role_title}. "
                            "Use the personal profile for title and the older page only for its dated degree-role statement")
                    else:
                        result.setdefault("warnings", []).append(
                            f"title conflict between personal profile ({profile_title}) and supervisor-role page ({role_title}); "
                            "do not assert a current title without checking page dates")
            role_verified = role_verified or has_role_for_name(role_page, name, degree, roster=True)
        except Exception as exc:
            if role_verified:
                result.setdefault("warnings", []).append(f"optional supervisor roster unavailable; profile role used: {exc}")
            else:
                problems.append(f"official supervisor roster could not be checked: {exc}")
    if degree == "unspecified" and page:
        faculty_role = (profile_title or
                        ("硕士生导师" if has_role_for_name(page, profile_name, "master", roster=False) else None) or
                        ("博士生导师" if has_role_for_name(page, profile_name, "doctor", roster=False) else None))
        if faculty_role:
            result["verified"]["official_faculty_role"] = faculty_role
        else:
            problems.append("official profile does not establish this person's faculty role")
    elif degree != "unspecified" and not role_verified:
        problems.append(f"official {degree} supervisor evidence is missing")

    title = str(paper.get("title") or "").strip()
    year = paper.get("year")
    official_project = row.get("official_project") if isinstance(row.get("official_project"), dict) else {}
    # This is a literal overlap diagnostic, not a semantic fit verdict. It
    # makes an adjacent credential paper visible instead of silently treating
    # any dated paper as evidence for the applicant's specific project.
    if focus_terms is not None:
        result["fit_audit"] = {
            "scope": "submitted_paper_title_only; research fit not certified",
            "paper_title": title,
            "matched_focus_terms": title_focus_matches(title, focus_terms),
            "ignored_umbrella_terms": [term for term in focus_terms
                                       if term.casefold() in UMBRELLA_FOCUS_TERMS],
        }
    current_year = today_in_china().year
    if degree == "unspecified" and not title and official_project:
        project_title = str(official_project.get("title") or "").strip()
        project_excerpt = str(official_project.get("excerpt") or "").strip()
        if (not page or not project_title or len(project_excerpt) < 35 or
                normalized(project_title) not in normalized(page) or
                normalized(project_excerpt) not in normalized(page)):
            problems.append("person-specific official project title and concrete quoted excerpt are not established on the faculty page")
        else:
            result["verified"]["official_project_title"] = project_title
            result["verified"]["official_project_excerpt"] = project_excerpt[:400]
    elif not title or not isinstance(year, int) or not (current_year - 5 <= year <= current_year):
        problems.append("one titled direction paper dated within the past five years is required")
    else:
        official_paper = bool(page and official_paper_match(page, title, year))
        if paper_id and client and person_id:
            try:
                source = prefetched_paper or record_data(client, "paper_detail", paper_id)
                authors = source.get("authors") or []
                paper_problem_count = len(problems)
                source_year = source.get("year")
                info_record = None
                if not isinstance(source_year, int):
                    info_rows = unwrap(client.call("paper_info", {"ids": [paper_id]}))
                    matching_info = [item for item in info_rows
                                     if str(item.get("id") or "") == paper_id]
                    info_record = matching_info[0] if len(matching_info) == 1 else None
                    source_year = info_record.get("year") if info_record else None
                if not isinstance(source_year, int):
                    def venue_label(item: dict) -> str:
                        venue = item.get("venue")
                        return str((venue.get("raw") if isinstance(venue, dict) else venue)
                                   or item.get("venue_name") or "")
                    venue_years = {int(value) for item in (source, info_record or {})
                                   for value in re.findall(r"\b20\d{2}\b", venue_label(item))}
                    if venue_years == {year}:
                        source_year = year
                        result["verified"]["paper_year_source"] = "AMiner venue label"
                        result.setdefault("warnings", []).append(
                            "paper year is supported by the AMiner venue label; publication status needs a primary-source check")
                if (normalized(str(source.get("title") or "")) != normalized(title)
                        or source_year != year):
                    problems.append("paper title/year does not match the AMiner paper ID")
                if not any(str(a.get("id") or "") == person_id for a in authors):
                    problems.append("AMiner paper does not attribute the work to this person ID")
                affiliation_method = paper_affiliation_match(authors, person_id, row, client)
                if not affiliation_method:
                    problems.append("paper author affiliation does not establish the requested university")
                if len(problems) == paper_problem_count:
                    result["verified"]["aminer_paper_id"] = paper_id
                    result["verified"]["paper_affiliation"] = affiliation_method
                    if affiliation_method and "org ID absent" in affiliation_method:
                        result.setdefault("warnings", []).append(
                            "paper author org ID is absent; university affiliation is supported by normalized name text")
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
    candidate_source = parser.add_mutually_exclusive_group(required=True)
    candidate_source.add_argument("--candidates-json", help="JSON array of proposed main-list rows")
    candidate_source.add_argument("--candidates-file", help="UTF-8 JSON file containing proposed main-list rows")
    candidate_source.add_argument("--candidates-stdin", action="store_true",
                                  help="Read a UTF-8 JSON candidate array from standard input")
    parser.add_argument("--program-terms", default="", help="Exact official program names, only when the user specified a program or college")
    parser.add_argument("--allowed-domains", required=True, help="Comma-separated edu.cn domains of the requested schools")
    parser.add_argument("--degree", choices=("master", "doctor", "unspecified"), required=True)
    parser.add_argument("--focus-terms", required=True,
                        help="Comma-separated, applicant-specific topic/task aliases; omit umbrella field names")
    args = parser.parse_args()
    if args.candidates_file:
        with open(args.candidates_file, encoding="utf-8") as source:
            candidates = json.load(source)
    elif args.candidates_stdin:
        candidates = json.load(sys.stdin)
    else:
        candidates = json.loads(args.candidates_json)
    if not isinstance(candidates, list) or not candidates or not all(isinstance(c, dict) for c in candidates):
        parser.error("--candidates-json must be a nonempty JSON array of objects")
    terms = [s.strip() for s in args.program_terms.split(",") if s.strip()]
    focus_terms = list(dict.fromkeys(s.strip() for s in args.focus_terms.split(",") if s.strip()))
    if not focus_terms or not any(term.casefold() not in UMBRELLA_FOCUS_TERMS for term in focus_terms):
        parser.error("--focus-terms needs applicant-specific topic/task aliases")
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
            checked.append(verify_candidate(row, terms, args.degree, client, focus_terms))
    approved, leads, rejected = partition_checked(checked)
    print(json.dumps({"approved": approved,
                      "leads": leads,
                      "rejected": rejected,
                      "rejected_note": "Identity or paper attribution failed. Exclude from both recommendation tiers unless corrected evidence is checked in a new gate run.",
                      "verification_scope": ("identity, an official faculty-role statement, and an attributed dated paper or quoted person-specific official project"
                                             if args.degree == "unspecified" else
                                             "identity, an official degree-role statement, and an attributed dated paper")
                                            + "; current employment, current-cycle eligibility, research fit, and openings are not certified",
                      "aminer_cost": client.cost.summary() if client else None}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
