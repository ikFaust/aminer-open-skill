#!/usr/bin/env python3
"""Run evidence-backed AMiner school and prospective-advisor workflows."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from aminer_client import API_SPEC, AMinerAPIError, AMinerClient, unwrap


SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TIERS = SKILL_ROOT / "references" / "school-tiers.json"
DIRECTORY_SEEDS = SKILL_ROOT / "references" / "official-directory-seeds.json"
PROFILE_SEEDS = SKILL_ROOT / "references" / "official-profile-seeds.json"
CHINA_TZ = ZoneInfo("Asia/Shanghai")
CURRENT_YEAR = datetime.now(CHINA_TZ).year
FACULTY_TERMS = (
    "professor", "associate professor", "assistant professor", "researcher", "principal investigator",
    "教授", "副教授", "研究员", "副研究员", "博导", "博士生导师", "硕导", "硕士生导师",
)
NON_FACULTY_TERMS = ("student", "phd candidate", "postdoc", "postdoctoral", "学生", "博士生", "硕士生", "博士后")
JUNIOR_POSITION_TERMS = (
    "assistant professor", "lecturer", "tenure-track", "助理教授", "助理研究员", "讲师", "准聘", "特聘研究员",
)
HIRE_YEAR_PATTERNS = (
    re.compile(r"(20\d{2})\s*年(?:\s*\d{1,2}\s*月)?[^。；;]{0,12}?(?:加入|入职|受聘|任教)"),
    re.compile(r"(?:joined|joining)[^.;]{0,60}?in\s+(20\d{2})", re.IGNORECASE),
)
COMPUTING_DIRECTION_TERMS = (
    "machine learning", "deep learning", "computer vision", "natural language", "large language model",
    "artificial intelligence", "robot", "embodied", "机器学习", "深度学习", "计算机视觉", "自然语言",
    "大模型", "人工智能", "机器人", "具身",
)
CROSS_DISCIPLINE_AFFILIATION_TERMS = (
    "hospital", "medical", "medicine", "clinical", "public health", "economics", "business school",
    "sports", "literature", "philosophy", "archaeology", "chemistry", "chemical engineering",
    "医院", "医学院", "临床", "公共卫生", "经济学院", "商学院", "体育学院", "文学院", "哲学",
    "考古", "化学学院", "化工学院", "资源环境",
)
COMPUTING_AFFILIATION_TERMS = (
    "computer", "computing", "informatics", "software", "artificial intelligence",
    "automation", "robot", "计算机", "软件", "人工智能", "自动化", "机器人",
)

# Off-field topics that appear in a scholar's OWN interests (not affiliation).
# Blacklist of clearly-unrelated disciplines seen contaminating recall via
# multi-author papers. Kept deliberately conservative — only add a term when it
# is almost never a CS/AI subfield, so real subfields are never removed.
CROSS_DISCIPLINE_INTEREST_TERMS = (
    "clinical", "prenatal", "cancer", "tumor", "surgery", "gut microbiota", "epidemiology",
    "reactor", "nuclear reactor", "nuclear physics", "nuclear energy", "monte carlo", "combustion", "pyrolysis", "catalysis", "photocatalysis",
    "urban planning", "architecture", "seagrass", "climate change", "batteries", "lithium",
    "nanocryst", "quantum dot", "supramolecular", "metal-organic", "silicon", "photonic",
    "临床", "产前", "肿瘤", "癌", "反应堆", "核工程", "核物理", "燃烧", "催化", "城市规划", "建筑", "电池", "材料",
)


def normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def normalized_person_name(value: Any) -> str:
    """Canonicalize a person name: paper bylines use \"Last, First\" while AMiner
    profiles use \"First Last\" — fold both to the same key."""
    name = normalized_text(value)
    if "," in name:
        last, _, first = name.partition(",")
        last, first = last.strip(), first.strip()
        if first and last:
            name = f"{first} {last}"
    return name.replace(",", " ").replace("  ", " ").strip()


def tokens(text: str) -> set[str]:
    value = normalized_text(text)
    latin = set(re.findall(r"[a-z][a-z0-9+.-]{1,}", value))
    chinese = set(re.findall(r"[\u4e00-\u9fff]{2,}", value))
    return latin | chinese


def contains_any(text: str, terms: Iterable[str]) -> bool:
    haystack = normalized_text(text)
    return any(normalized_text(term) in haystack for term in terms if term)


def direction_aliases(direction: str, aliases: list[str]) -> list[str]:
    return list(dict.fromkeys(x.strip() for x in [direction, *aliases] if x and x.strip()))


def school_search_terms(direction: str, aliases: list[str], applicant: dict[str, Any] | None) -> list[str]:
    """Keep an ambiguous sensor term from crowding out task-specific seeds.

    Bare LiDAR returns atmospheric and optical-instrument papers as well as
    robotics papers. For a robotics applicant, prefer a third specific alias
    such as SLAM when one is available; keep bare LiDAR for LiDAR research
    requests or when there are fewer than three alternatives.
    """
    terms = direction_aliases(direction, aliases)
    if applicant and ("robot" in normalized_text(direction) or "机器人" in direction):
        specific = [term for term in terms if normalized_text(term) != "lidar"]
        if len(specific) >= 3:
            terms = specific
    return terms[:3]


def official_directory_hints(schools: list[str], direction: str) -> dict[str, list[dict[str, str]]]:
    """Surface checked university entrypoints as navigation hints, never proof."""
    try:
        seeds = json.loads(DIRECTORY_SEEDS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    query = normalized_text(direction)
    hints: dict[str, list[dict[str, str]]] = {}
    for row in seeds:
        school = str(row.get("school") or "")
        if school not in schools or not any(normalized_text(term) in query for term in row.get("direction_terms", [])):
            continue
        hints.setdefault(school, []).append({
            "unit": str(row.get("unit") or ""),
            "url": str(row.get("url") or ""),
            "checked_on": str(row.get("checked_on") or ""),
            "scope": "official navigation seed; re-fetch the live faculty page and verify each person's role",
        })
    return hints


def official_profile_hints(candidates: list[Candidate]) -> list[dict[str, str]]:
    """Return checked official URLs only for exact discovered people and schools.

    URLs are navigation aids. A live re-fetch must establish every role, date,
    topic and degree claim; the seed itself carries none of those assertions.
    """
    try:
        seeds = json.loads(PROFILE_SEEDS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    known = {(str(row.get("school") or ""), str(row.get("name") or "")): row
             for row in seeds if isinstance(row, dict)}
    result = []
    seen = set()
    for candidate in candidates:
        if candidate.identity_ambiguous:
            continue
        for school in candidate.source_schools:
            row = known.get((school, candidate.display_name))
            if not row or (school, candidate.display_name) in seen:
                continue
            seen.add((school, candidate.display_name))
            result.append({"school": school, "name": candidate.display_name,
                           "url": str(row["url"]), **official_url_seed_metadata(row),
                           "scope": "official profile navigation only; re-fetch live page and verify claims"})
    return result


def official_url_seed_metadata(row: dict[str, Any]) -> dict[str, str]:
    """Keep URL availability separate from the source page's own age."""
    result = {"url_checked_on": str(row.get("checked_on") or "")}
    published = str(row.get("source_page_published_on") or "")
    if published:
        result["source_page_published_on"] = published
        try:
            if (datetime.now(CHINA_TZ).date() - date.fromisoformat(published)).days > 730:
                result["stale_page_warning"] = (
                    f"Official page published {published}, over two years ago; "
                    "do not assert current faculty or degree status from it. Re-fetch and date-qualify claims."
                )
        except ValueError:
            pass
    return result


def official_seed_leads(schools: list[str], direction: str, aliases: list[str]) -> dict[str, list[dict[str, str]]]:
    """Independent official-page recall for sparse AMiner school samples.

    A curated URL is only a person to investigate. It cannot establish current
    employment, task fit, degree role, or a paper without a live page and
    independent attribution check.
    """
    try:
        seeds = json.loads(PROFILE_SEEDS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    query = normalized_text(" ".join([direction, *aliases]))
    leads: dict[str, list[dict[str, str]]] = {}
    for row in seeds:
        if not isinstance(row, dict) or row.get("school") not in schools:
            continue
        if not any(normalized_text(term) in query for term in row.get("topic_terms", [])):
            continue
        school = str(row["school"])
        leads.setdefault(school, []).append({
            "name": str(row["name"]), "url": str(row["url"]),
            **official_url_seed_metadata(row),
            "scope": "official URL lead only; re-fetch and independently verify topic paper and faculty role",
        })
    return {school: rows[:3] for school, rows in leads.items()}


def school_tier_facts(schools: list[str], tiers: dict[str, Any]) -> dict[str, dict[str, bool]]:
    """Expose exact national-list membership for named applicant schools."""
    groups = tiers.get("tiers") or {}
    return {school: {
        label: school in set((groups.get(label) or {}).get("schools") or [])
        for label in ("985", "211", "双一流")
    } for school in schools}


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def read_tiers(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass
class ResolvedOrganization:
    query: str
    org_id: str
    canonical_name: str
    aliases: list[str] = field(default_factory=list)


@dataclass
class Candidate:
    person_id: str
    name: str
    name_zh: str = ""
    org: str = ""
    org_zh: str = ""
    org_id: str = ""
    interests: list[str] = field(default_factory=list)
    n_citation: int | None = None
    author_affiliations: set[str] = field(default_factory=set)
    papers: dict[str, dict[str, Any]] = field(default_factory=dict)
    collaboration_orgs: dict[str, str] = field(default_factory=dict)
    collaboration_types: dict[str, str] = field(default_factory=dict)
    collaboration_papers: dict[str, set[str]] = field(default_factory=dict)
    scores: dict[str, float | None] = field(default_factory=dict)
    confidence: str = "low"
    recommendation_band: str | None = None
    source_schools: set[str] = field(default_factory=set)
    alternate_profile_ids: list[str] = field(default_factory=list)
    identity_ambiguous: bool = False
    identity_resolution: str = "org_constrained_name"
    role: str | None = None
    role_unverified: bool = True
    hire_assessment: dict[str, Any] | None = None
    career: list[dict[str, Any]] = field(default_factory=list)
    education: list[dict[str, Any]] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.name_zh or self.name

    def to_dict(self) -> dict[str, Any]:
        verification = ["current department affiliation", "graduate-supervisor eligibility for the requested degree",
                        "current recruitment status", "admissions requirements and deadline"]
        if self.role_unverified:
            verification.insert(0, "current faculty position")
        if self.identity_ambiguous:
            verification.append("multiple AMiner profiles or name-based identity resolution require disambiguation")
        return {
            "person_id": self.person_id,
            "name": self.display_name,
            "name_en": self.name,
            "organization": self.org_zh or self.org,
            "organization_id": self.org_id,
            "author_affiliations": sorted(self.author_affiliations),
            "source_schools": sorted(self.source_schools),
            "aminer_url": None if self.person_id.startswith("unresolved:") else f"https://www.aminer.cn/profile/{self.person_id}",
            "alternate_profile_ids": self.alternate_profile_ids,
            "identity_ambiguous": self.identity_ambiguous,
            "identity_resolution": self.identity_resolution,
            "role": self.role,
            "role_unverified": self.role_unverified,
            "hire_assessment": self.hire_assessment,
            "career": self.career,
            "education": self.education,
            "interests": self.interests,
            "n_citation": self.n_citation,
            "evidence_papers": list(self.papers.values()),
            "collaboration_organizations": [
                {"id": key, "name": value, "type": self.collaboration_types.get(key, "unknown"),
                 "is_university": is_university_affiliation(value),
                 "paper_urls": [f"https://www.aminer.cn/pub/{paper_id}"
                                for paper_id in sorted(self.collaboration_papers.get(key, set()))]}
                for key, value in self.collaboration_orgs.items()
            ],
            "collaboration_evidence_paper_count": len(set().union(
                *self.collaboration_papers.values())) if self.collaboration_papers else 0,
            "scores": self.scores,
            "confidence": self.confidence,
            "recommendation_band": self.recommendation_band,
            "needs_verification": verification,
        }


SCHOOL_ALIASES = {"浙大": "浙江大学", "清华": "清华大学", "北大": "北京大学", "复旦": "复旦大学", "南开": "南开大学"}


def resolve_organization(client: AMinerClient, school: str) -> ResolvedOrganization | None:
    lookup = SCHOOL_ALIASES.get(school.strip(), school.strip())
    # Free search can return only a department ID for a compound affiliation.
    # The pro endpoint separates the university and department in that case.
    compound = bool(re.search(r"大学.+(?:学院|系|研究所)$", lookup)) or bool(
        re.search(r"(?:department|school|college).+university", lookup, re.IGNORECASE)
    )
    parent_id = ""
    pro_attempted = False
    if compound:
        pro_attempted = True
        normalized = unwrap(client.call("org_disambiguate_pro", {"org": lookup}))
        for item in normalized:
            if item.get("一级ID") and item.get("一级"):
                parent_id = str(item["一级ID"])
                lookup = str(item["一级"])
                break
        if not parent_id:
            # Never use a department search hit as the university ID. Extract
            # the explicit parent name and resolve that independently.
            chinese_parent = re.search(r"^(.+?大学)", lookup)
            english_parent = extract_institution_name(lookup)
            parent_name = chinese_parent.group(1) if chinese_parent else english_parent
            if not parent_name or normalized_text(parent_name) == normalized_text(lookup):
                return None
            lookup = parent_name
    rows = unwrap(client.call("org_search", {"orgs": [lookup]}))
    if not rows:
        if parent_id:
            return ResolvedOrganization(school, parent_id, lookup, [lookup])
        # A successful empty free result is not a normalized ID. Try the
        # documented hierarchy endpoint once, then stop if it is also empty.
        normalized = [] if pro_attempted else unwrap(client.call("org_disambiguate_pro", {"org": lookup}))
        for item in normalized:
            if item.get("一级ID") and item.get("一级"):
                return ResolvedOrganization(school, str(item["一级ID"]), str(item["一级"]), [])
        return None
    query = normalized_text(lookup)
    rows.sort(
        key=lambda row: (
            normalized_text(row.get("org_name")) == query,
            any(normalized_text(alias) == query for alias in row.get("aliases") or []),
        ),
        reverse=True,
    )
    row = rows[0]
    # A fuzzy org_search hit is not proof that it is the requested school.
    if normalized_text(row.get("org_name")) != query and not any(
        normalized_text(alias) == query for alias in row.get("aliases") or []
    ):
        if not parent_id and not pro_attempted:
            normalized = unwrap(client.call("org_disambiguate_pro", {"org": lookup}))
            for item in normalized:
                if item.get("一级ID") and item.get("一级"):
                    parent_id = str(item["一级ID"])
                    lookup = str(item["一级"])
                    break
        if not parent_id:
            return None
    org_id = parent_id or str(row.get("org_id") or "")
    name = str(row.get("org_name") or lookup) if not parent_id or str(row.get("org_id") or "") == parent_id else lookup
    if not org_id:
        return None
    aliases = [str(x) for x in row.get("aliases") or []] if str(row.get("org_id") or "") == org_id else []
    return ResolvedOrganization(school, org_id, name, aliases)


def paper_search(client: AMinerClient, direction: str, organization: ResolvedOrganization | str | None,
                 size: int) -> list[dict[str, Any]]:
    if isinstance(organization, ResolvedOrganization):
        rows = unwrap(client.call("paper_qa_search", {
            "use_topic": True, "query": direction,
            "org_id": [organization.org_id], "size": size, "offset": 0,
            "force_year_sort": True,
        }))
        if rows:
            return rows
        org_name = paper_search_org_name(organization)
    else:
        org_name = organization
    # query searches the title as well as keywords. keyword alone missed the
    # verified MARCO code-reasoning paper in the controlled S2 comparison.
    params: dict[str, Any] = {"query": direction, "order": "year", "page": 0, "size": size}
    if org_name:
        params["org"] = org_name
    return unwrap(client.call("paper_search_pro", params))


def paper_search_org_name(organization: ResolvedOrganization) -> str:
    """Prefer a verified English university spelling for name-filtered search."""
    # org_search aliases are often word-order variants, not equally indexed
    # institution names.  An English canonical name is the indexed spelling:
    # replacing "Nanjing University of Posts and Telecommunications" with its
    # first alias ("nanjing posts telecommunications university") made a
    # directly relevant code-generation paper disappear from every name-
    # filtered search despite the organization ID being correct.
    if re.search(r"\b(?:university|institute of technology)\b", organization.canonical_name, re.I):
        return organization.canonical_name
    return next((name for name in organization.aliases
                 if re.search(r"\b(?:university|institute of technology)\b", name, re.I)),
                organization.canonical_name)


def school_affiliation_names(organization: ResolvedOrganization) -> list[str]:
    """Include full institution names and their common exact Univ abbreviation.

    An org ID alone is insufficient: AMiner sometimes assigns a university ID
    to an author whose explicit paper affiliation names a different school.
    """
    names = [organization.canonical_name, organization.query, *organization.aliases]
    return list(dict.fromkeys(
        variant for name in names if name
        for variant in (name, re.sub(r"\bUniversity\b", "Univ", name, flags=re.I))
        if len(variant) > 4
    ))


def learn_english_school_alias(
    client: AMinerClient, organization: ResolvedOrganization,
    rows_by_term: list[list[dict[str, Any]]], warnings: list[str],
) -> None:
    """Use an ID-linked paper author profile to bridge split Chinese/English orgs.

    AMiner can place the Chinese university and its English spelling under
    different organization IDs. The alias is accepted only when a paper author
    carries the resolved Chinese ID and the exact person profile confirms it.
    """
    if paper_search_org_name(organization) != organization.canonical_name:
        return
    if not re.search(r"[一-鿿]", organization.canonical_name):
        return
    profile_lookups = 0
    for paper_id in bounded_paper_ids(rows_by_term, 3):
        try:
            details = unwrap(client.call("paper_detail", {"id": paper_id}))
        except AMinerAPIError as exc:
            warnings.append(f"{organization.query}: English alias probe failed ({exc.message})")
            continue
        for detail in details:
            for author in detail.get("authors") or []:
                if (not isinstance(author, dict) or
                        str(author.get("orgid") or "") != organization.org_id or
                        not author.get("id") or not author.get("name")):
                    continue
                if profile_lookups >= 8:
                    return
                profile_lookups += 1
                try:
                    profiles = unwrap(client.call("person_search", {
                        "name": author["name"], "org_id": [organization.org_id], "size": 10,
                    }))
                except AMinerAPIError:
                    continue
                for profile in profiles:
                    if (str(profile.get("id") or "") != str(author["id"]) or
                            str(profile.get("org_id") or "") != organization.org_id or
                            not contains_any(str(profile.get("org_zh") or ""),
                                             [organization.canonical_name])):
                        continue
                    alias = extract_institution_name(str(profile.get("org") or ""))
                    alias = re.sub(r"\s*&\s*", " and ", alias)
                    if (alias and alias != organization.canonical_name and
                            re.search(r"\b(?:university|institute of technology)\b", alias, re.I)):
                        organization.aliases.append(alias)
                        warnings.append(f"{organization.query}: recovered English search alias from exact ID-linked author profile: {alias}")
                        return


def title_search_seeds(terms: list[str], keep_primary_broad: bool = False) -> list[str]:
    """Turn narrow English aliases into complementary title-search phrases.

    QA topic search can bury a directly relevant paper below its small first page.
    Title search is an independent retrieval path, not evidence of author identity.
    """
    generic = {"ai", "artificial", "intelligence", "learning", "research", "analysis", "based"}
    seeds: list[str] = []
    for term in terms:
        words = re.findall(r"[a-z][a-z0-9-]*", term.lower())
        if not words or not re.search(r"[a-z]", term):
            continue
        distinctive = [word for word in words if word not in generic]
        if not distinctive:
            continue
        # Retain full four-word tasks such as "remote sensing object detection";
        # dropping the last word can substantially widen AMiner's OR-like box.
        # Other three-word tasks such as "oriented object detection" are intact.
        # suffixes in "embodied intelligence" and "robot learning" are dropped.
        seed = " ".join(distinctive[:4])
        if len(seed) < 4 or seed in seeds:
            continue
        seeds.append(seed)
    # A broad primary direction can otherwise consume 18 of the 20 reserved
    # paper-detail slots before the applicant's actual task gets inspected.
    broad = {"computer vision", "deep", "vision", "image", "artificial",
             "machine", "multimodal", "robot", "embodied"}
    specific = [seed for seed in seeds if seed not in broad]
    if keep_primary_broad and seeds and seeds[0] in broad:
        return [seeds[0], *[seed for seed in specific if seed != seeds[0]][:1]]
    if any("multimodal large language model" in normalized_text(term) or
           normalized_text(term) in {"mllm", "多模态大模型"} for term in terms):
        # AnyGPT and similar direct MLLM papers use "multimodal LLM" in the
        # title, not the long-form phrase. This live-tested alias recovers
        # their authors without naming a particular scholar or model family.
        return list(dict.fromkeys(["multimodal llm", *(specific or seeds)]))[:3]
    if keep_primary_broad:
        # Collaboration evidence may live in a specific third or fourth
        # branch (e.g. EEG decoding rather than the umbrella BCI phrase).
        return (specific or seeds)[:4]
    return (specific or seeds)[:2]


def remote_sensing_object_detection_title(title: str) -> bool:
    """Match the task, including titles that omit the redundant word 'object'."""
    value = normalized_text(title)
    domain = bool(re.search(r"\b(?:remote sensing|aerial imag\w*|satellite imag\w*|sar)\b", value))
    detection = bool(re.search(r"\bdetect(?:ion|or|ing)?\b", value))
    adjacent = bool(re.search(r"\b(?:change|cloud|anomaly|salient object) detection\b", value))
    return domain and detection and not adjacent


def complementary_title_search(
    client: AMinerClient, organization: ResolvedOrganization, terms: list[str],
    warnings: list[str], keep_primary_broad: bool = False,
) -> list[list[dict[str, Any]]]:
    """Recall school papers the org-ID QA head sample misses.

    As with the general academic skill, use the documented title/org/year and
    title/org/citation views. Details and exact author affiliations remain the
    downstream source of attribution; an org-filtered search hit alone is not.
    """
    samples: list[list[dict[str, Any]]] = []
    seeds = title_search_seeds(terms, keep_primary_broad)
    for index, seed in enumerate(seeds):
        orders = ("n_citation", "year") if index == 0 else ("n_citation",)
        keyword_seed = keep_primary_broad and index == 0
        for order in orders:
            try:
                rows = unwrap(client.call("paper_search_pro", {
                    ("keyword" if keyword_seed else "title"): seed,
                    "org": paper_search_org_name(organization),
                    "size": 100, "page": 0, "order": order,
                    "year_from": CURRENT_YEAR - 5, "year_to": CURRENT_YEAR,
                }))
            except AMinerAPIError as exc:
                warnings.append(f"{organization.query}: title search {seed}/{order} failed ({exc.message})")
                continue
            # A broad keyword lead can have no field phrase in its title (for
            # example AgentVLN has an embodied-AI keyword). Author affiliation
            # and topic are checked in paper_detail downstream. Narrow title
            # seeds still require the literal stem on the returned card.
            remote_object_focus = any("remote sensing object detection" in normalized_text(t)
                                      or "遥感图像目标检测" in str(t) for t in terms)
            samples.append([row for row in rows if isinstance(row.get("year"), int)
                            and CURRENT_YEAR - 5 <= row["year"] <= CURRENT_YEAR
                            and (keyword_seed or contains_any(str(row.get("title") or ""), [seed])
                                 or (remote_object_focus and remote_sensing_object_detection_title(
                                     str(row.get("title") or ""))))])
    return samples


def complementary_abstract_search(
    client: AMinerClient, organization: ResolvedOrganization, terms: list[str],
    warnings: list[str],
) -> list[list[dict[str, Any]]]:
    """Find school papers whose model or method name hides the field in the title.

    This is the same abstract+organization retrieval path used by the general
    academic skill. Search cards are leads only; paper_detail and author
    affiliation still decide whether a scholar can be recommended.
    """
    seed = next((term.strip() for term in terms
                 if 2 <= len(re.findall(r"[a-z][a-z0-9-]*", term.lower())) <= 8), None)
    if not seed:
        return []
    samples: list[list[dict[str, Any]]] = []
    for order in ("n_citation", "year"):
        try:
            rows = unwrap(client.call("paper_search_pro", {
                "abstract": seed, "org": paper_search_org_name(organization),
                "size": 100, "page": 0, "order": order,
            }))
        except AMinerAPIError as exc:
            warnings.append(f"{organization.query}: abstract search {seed}/{order} failed ({exc.message})")
            continue
        samples.append([row for row in rows if isinstance(row.get("year"), int)
                        and CURRENT_YEAR - 5 <= row["year"] <= CURRENT_YEAR])
    return samples


def discovery_abstract_seed(terms: list[str]) -> str | None:
    """Use a repeated field stem for broad institution discovery."""
    phrases = [re.findall(r"[a-z][a-z0-9-]*", term.lower()) for term in terms]
    pairs = Counter(pair for words in phrases for pair in set(zip(words, words[1:])))
    shared = [pair for pair, count in pairs.items() if count >= 2]
    if shared:
        return " ".join(max(shared, key=lambda pair: (pairs[pair], sum(map(len, pair)))))
    return next((" ".join(words) for words in phrases if len(words) >= 2), None)


def batch_paper_info(client: AMinerClient, paper_ids: list[str], warnings: list[str]) -> list[dict[str, Any]]:
    if not paper_ids:
        return []
    if len(paper_ids) > 100:
        warnings.append(f"paper_info supports 100 IDs; {len(paper_ids) - 100} papers were omitted")
    return unwrap(client.call("paper_info", {"ids": paper_ids[:100]}))


def clean_paper(row: dict[str, Any], detail: dict[str, Any] | None, terms: list[str]) -> dict[str, Any] | None:
    title = str((detail or {}).get("title") or row.get("title") or "").strip()
    year = (detail or {}).get("year") or row.get("year")
    if not title or not isinstance(year, int) or year > CURRENT_YEAR:
        return None
    keywords = " ".join(str(x) for x in ((detail or {}).get("keywords") or []))
    abstract = " ".join([str(row.get("abstract_slice") or ""), str((detail or {}).get("abstract") or "")])
    title_terms = [term for term in terms if contains_any(title, [term])]
    if any("remote sensing object detection" in normalized_text(term)
           or "遥感图像目标检测" in term for term in terms):
        if remote_sensing_object_detection_title(title) and not title_terms:
            title_terms.append("remote sensing object detection (task-equivalent title)")
    keyword_terms = [term for term in terms if contains_any(keywords, [term])]
    abstract_terms = [term for term in terms if contains_any(abstract, [term])]
    matched = list(dict.fromkeys([*title_terms, *keyword_terms, *abstract_terms]))
    if not matched:
        return None
    paper_id = str(row.get("id") or (detail or {}).get("id") or "")
    return {
        "id": paper_id,
        "title": title,
        "year": year,
        "aminer_url": f"https://www.aminer.cn/pub/{paper_id}",
        "direction_term_match": True,
        "matched_terms": matched,
        "topic_evidence": "title" if title_terms else "keywords" if keyword_terms else "abstract_only",
        "n_citation_bucket": row.get("n_citation_bucket") or (detail or {}).get("n_citation_bucket"),
    }


def multimodal_llm_paper(row: dict[str, Any], detail: dict[str, Any] | None) -> bool:
    """Require model evidence, not a generic 'multimodal' keyword hit."""
    title = normalized_text((detail or {}).get("title") or row.get("title"))
    keywords = normalized_text(" ".join(str(x) for x in ((detail or {}).get("keywords") or [])))
    direct = (
        "multimodal large language model", "multi-modal large language model",
        "multimodal llm", "multi-modal llm", "mllm", "vision-language model",
        "visual language model", "large vision-language model", "vlm",
    )
    if contains_any(title + " " + keywords, direct):
        return True
    modality_in_title = contains_any(title, (
        "multimodal", "multi-modal", "vision-language", "visual-linguistic",
        "image-text", "video-language", "audio-language",
    ))
    model_in_title = contains_any(title, ("language model", "foundation model", "llm"))
    return modality_in_title and model_in_title


def medical_imaging_title_focus(title: str) -> bool:
    """Distinguish a medical-imaging method from a generic model with a broad keyword."""
    return contains_any(title, (
        "medical", "biomedical", "clinical", "radiolog", "radiograph", "ultrasound",
        "echocardi", "dermoscop", "dermatolog", "patholog", "tumor", "tumour",
        "lesion", "retina", "ophthalm", "samed", "medsam", "medmnist",
        "医学", "影像", "病理", "肿瘤", "超声",
    )) or bool(re.search(r"\b(?:mri|ct|pet|x-ray|xray)\b", title, re.IGNORECASE))


def medical_segmentation_paper(row: dict[str, Any], detail: dict[str, Any] | None) -> bool:
    """Count a medical-segmentation paper only when both task and domain are explicit.

    A broad ``medical image`` search also retrieves classification, encryption,
    diagnosis and fusion papers. Those are useful leads, not segmentation output.
    """
    title = str((detail or {}).get("title") or row.get("title") or "")
    keywords = " ".join(str(k) for k in (detail or {}).get("keywords") or [])
    task_pattern = r"\bsegment(?:ation|ing|ed|er|ers)?\b|分割|勾画"
    title_has_task = bool(re.search(task_pattern, title, re.I))
    keywords_have_task = bool(re.search(task_pattern, keywords, re.I))
    if re.search(r"\b(?:crop|plant|agricultur\w*|leaf|seedling)\b|作物|农作物|叶片", title, re.I):
        return False
    if not title_has_task and re.search(
        r"\b(?:classification|diagnosis|encryption|reconstruction|registration|detection|fusion|quantification)\b"
        r"|分类|诊断|加密|重建|配准|检测|融合", title, re.I
    ):
        return False
    medical_context = medical_imaging_title_focus(title) or contains_any(
        title + " " + keywords,
        ("medical imag", "biomedical", "clinical", "radiolog", "ultrasound", "echocardi",
         "patholog", "histolog", "tumor", "tumour", "lesion", "retina", "fundus",
         "ophthalm", "abdomin", "brain", "stroke", "fetal", "cardiac",
         "lung", "liver", "kidney", "breast", "prostate", "医学影像", "医学图像",
         "病理", "肿瘤", "器官", "脑", "超声"),
    )
    return medical_context and (title_has_task or keywords_have_task)


def industrial_few_shot_defect_paper(row: dict[str, Any], detail: dict[str, Any] | None) -> bool:
    """Require the industrial defect task and a low-data method in one paper.

    School-level OR searches also retrieve few-shot machine-fault diagnosis and
    zero-shot industrial anomaly detection. Neither alone answers a request for
    few-shot visual defect detection, so keep them as adjacent leads.
    """
    title = normalized_text((detail or {}).get("title") or row.get("title"))
    keywords = normalized_text(" ".join(str(k) for k in (detail or {}).get("keywords") or []))
    combined = title + " " + keywords
    context = combined + " " + normalized_text((detail or {}).get("abstract") or row.get("abstract_slice"))
    if "zero-shot" in title and not re.search(r"few[- ]shot|few[- ]data|small[- ]samples?", title):
        return False
    # Generating synthetic anomalies can improve a detector, but a synthesis
    # paper by itself is not evidence that this advisor studies detection.
    if re.search(r"\b(?:anomal(?:y|ies)|defect) synthesis\b", title) and not re.search(
        r"\b(?:detect(?:ion)?|segment(?:ation)?|localiz(?:ation)?|inspection)\b", title
    ):
        return False
    defect = bool(re.search(r"\b(?:defect|anomal(?:y|ies))\b|缺陷|异常检测", combined))
    industrial = bool(re.search(
        r"\b(?:industr\w*|manufactur\w*|surface|product|fabric|textile|steel|"
        r"quality inspection|mvtec|btad|mpdd)\b|工业|制造|表面|产品|纺织|钢板|质量检测", context))
    low_data = bool(re.search(
        r"\b(?:few[- ]shot|few[- ]data|small[- ]samples?|limited data|low[- ]shot|"
        r"low[- ]data|one[- ]shot)\b|少样本|小样本|有限样本", combined))
    return defect and industrial and low_data


def industrial_few_shot_focus(terms: list[str]) -> bool:
    return (
        any(contains_any(term, ("defect detection", "industrial anomaly detection", "缺陷检测"))
            for term in terms)
        and any(contains_any(term, ("few-shot", "few shot", "few-data", "少样本", "小样本"))
                for term in terms)
    )


def retain_joint_topic_candidates(
    candidates: dict[str, Candidate], details: dict[str, dict[str, Any]], warnings: list[str],
) -> dict[str, Candidate]:
    """Keep only people with an attributed paper at the requested intersection."""
    retained = {
        person_id: candidate for person_id, candidate in candidates.items()
        if any(industrial_few_shot_defect_paper(paper, details.get(str(paper.get("id"))))
               for paper in candidate.papers.values())
    }
    if len(retained) < len(candidates):
        warnings.append(
            f"compound task excluded {len(candidates) - len(retained)} one-sided candidate(s); "
            "their separate defect or few-shot work is a lead, not joint-topic mentor evidence"
        )
    return retained


def fetch_paper_details(
    client: AMinerClient, paper_ids: list[str], max_details: int, warnings: list[str]
) -> dict[str, dict[str, Any]]:
    selected = paper_ids[:max_details]
    if len(paper_ids) > len(selected):
        warnings.append(f"paper detail cap omitted {len(paper_ids) - len(selected)} papers")
    details: dict[str, dict[str, Any]] = {}
    if not selected:
        return details
    def fetch(paper_id: str) -> list[dict[str, Any]]:
        return unwrap(client.call("paper_detail", {"id": paper_id}))
    with ThreadPoolExecutor(max_workers=min(4, len(selected))) as executor:
        for paper_id, rows in zip(selected, executor.map(fetch, selected)):
            if rows:
                details[paper_id] = rows[0]
    return details


def bounded_paper_ids(rows_by_term: list[list[dict[str, Any]]], limit: int) -> list[str]:
    """Take a small, de-duplicated round-robin sample across direction aliases."""
    selected: list[str] = []
    seen: set[str] = set()
    for position in range(max(map(len, rows_by_term), default=0)):
        for rows in rows_by_term:
            if position >= len(rows):
                continue
            paper_id = str(rows[position].get("id") or "")
            if paper_id and paper_id not in seen:
                selected.append(paper_id)
                seen.add(paper_id)
                if len(selected) >= limit:
                    return selected
    return selected


def ranked_abstract_ids(samples: list[list[dict[str, Any]]], limit: int) -> list[str]:
    """Keep enough citation-ranked papers while reserving a few recent leads."""
    if not samples:
        return []
    recent_reserve = min(6, limit // 4) if len(samples) > 1 else 0
    cited = bounded_paper_ids(samples[:1], limit - recent_reserve)
    recent = bounded_paper_ids(samples[1:], recent_reserve)
    return list(dict.fromkeys([*cited, *recent]))[:limit]


def resolve_people(
    client: AMinerClient,
    papers: list[dict[str, Any]],
    details: dict[str, dict[str, Any]],
    organization: ResolvedOrganization,
    direction_terms: list[str],
    max_author_lookups: int,
    allow_name_fallback: bool,
    warnings: list[str],
) -> dict[str, Candidate]:
    paper_by_id = {str(row.get("id")): row for row in papers if row.get("id")}
    school_names = [name for name in (organization.canonical_name, organization.query, *organization.aliases) if name]
    author_papers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    author_affiliations: dict[str, set[str]] = defaultdict(set)
    author_org_ids: dict[str, set[str]] = defaultdict(set)
    author_profile_ids: dict[str, set[str]] = defaultdict(set)
    senior_authors: set[str] = set()
    abstract_senior_authors: set[str] = set()
    skipped_without_org = 0

    for paper_id, row in paper_by_id.items():
        detail = details.get(paper_id)
        evidence = clean_paper(row, detail, direction_terms)
        if not evidence or not detail:
            continue
        school_author_keys: list[str] = []
        for author in detail.get("authors") or []:
            if not isinstance(author, dict):
                continue
            name = str(author.get("name") or "").strip()
            org_id = str(author.get("orgid") or "")
            org_name = str(author.get("org") or "").strip()
            if not name:
                continue
            exact_org = bool(org_id and org_id == organization.org_id)
            # Paper authors are usually signed with a department-level orgid that differs
            # from the school-level ID; accept an affiliation whose text names the school.
            # Identity is still resolved through the org_id-constrained person_search below.
            text_org = contains_any(org_name, school_names)
            if not exact_org and not text_org:
                skipped_without_org += 1
                continue
            key = normalized_person_name(name)
            school_author_keys.append(key)
            author_papers[key].append(evidence)
            if org_name:
                author_affiliations[key].add(org_name)
            if org_id:
                author_org_ids[key].add(org_id)
            if author.get("id"):
                author_profile_ids[key].add(str(author["id"]))
        if school_author_keys:
            senior_key = school_author_keys[-1]
            senior_authors.add(senior_key)
            if evidence.get("topic_evidence") == "abstract_only":
                abstract_senior_authors.add(senior_key)

    def author_priority(key: str) -> tuple[int, int, int, int, int]:
        papers = author_papers[key]
        strength = max({"title": 2, "keywords": 1}.get(p.get("topic_evidence"), 0) for p in papers)
        citation_floors = [int(match.group()) for p in papers
                           if (match := re.search(r"\d+", str(p.get("n_citation_bucket") or "")))]
        return (strength, int(bool(author_profile_ids[key])), max(citation_floors, default=0),
                len({p["id"] for p in papers}), max(p["year"] for p in papers))

    ordered_authors = sorted(author_papers, key=author_priority, reverse=True)
    # First/last authorship does not prove a faculty role, but sampling the
    # last school-affiliated author stops large-team papers from filling the
    # lookup cap with students before a likely supervising author is checked.
    senior_quota = max_author_lookups // 3
    abstract_quota = max_author_lookups // 6
    selected_authors = list(dict.fromkeys([
        *sorted(senior_authors, key=author_priority, reverse=True)[:senior_quota],
        *sorted(abstract_senior_authors, key=author_priority, reverse=True)[:abstract_quota],
        *ordered_authors,
    ]))[:max_author_lookups]
    if len(ordered_authors) > max_author_lookups:
        warnings.append(f"author lookup cap omitted {len(ordered_authors) - max_author_lookups} organization-matched authors")
    if skipped_without_org:
        warnings.append(
            f"skipped {skipped_without_org} paper-author entries whose affiliation did not match the target organization; "
            "entries can repeat across search passes and are not distinct people"
        )

    by_id: dict[str, Candidate] = {}
    fallback_count = 0
    def lookup_author(author_key: str) -> tuple[str, str, list[dict[str, Any]], str, int]:
        display_name = next(
            str(author.get("name"))
            for detail in details.values()
            for author in detail.get("authors") or []
            if isinstance(author, dict) and normalized_person_name(author.get("name")) == author_key
        )
        query_name = display_name
        if "," in query_name:
            last, _, first = query_name.partition(",")
            if first.strip() and last.strip():
                query_name = f"{first.strip()} {last.strip()}"
        people = unwrap(
            client.call("person_search", {"name": query_name, "org_id": [organization.org_id], "size": 10})
        )
        # Person profiles carry department-level org_ids too; accept a profile whose
        # organization text names the school when the ID does not match exactly.
        def school_profile_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                row for row in rows
                if str(row.get("org_id") or "") == organization.org_id
                or contains_any(f"{row.get('org')} {row.get('org_zh')}", school_names)
            ]

        org_people = school_profile_rows(people)
        author_ids = author_profile_ids[author_key]
        if not org_people or (author_ids and not any(
            str(row.get("id") or "") in author_ids for row in org_people
        )):
            # The server-side school-level org_id filter excludes department-level
            # profiles even when it returns other same-name scholars. Retry by
            # organization text whenever the paper's exact author ID is absent.
            text_people = unwrap(
                client.call("person_search", {"name": query_name, "org": organization.canonical_name, "size": 10})
            )
            people = list({str(row.get("id") or ""): row for row in [*people, *text_people]
                           if row.get("id")}.values())
            org_people = school_profile_rows(people)
        exact_people = [
            row for row in org_people
            if author_key in (normalized_person_name(row.get("name")), normalized_person_name(row.get("name_zh")))
        ]
        # paper_detail may give the exact AMiner author ID. When name search
        # yields multiple same-name profiles, use that ID only if its profile
        # also passes the school and exact-name checks above.
        linked = [row for row in exact_people if str(row.get("id") or "") in author_profile_ids[author_key]]
        if len(linked) == 1:
            exact_people = linked
        resolution = "org_constrained_exact_name"
        fallback_used = 0
        if not exact_people and allow_name_fallback:
            exact_people = [
                row for row in people
                if contains_any(f"{row.get('org')} {row.get('org_zh')}", [organization.canonical_name, organization.query])
            ]
            fallback_used = len(exact_people)
            resolution = "name_fallback"
        return author_key, display_name, exact_people, resolution, fallback_used

    if selected_authors:
        with ThreadPoolExecutor(max_workers=min(4, len(selected_authors))) as executor:
            lookups = list(executor.map(lookup_author, selected_authors))
    else:
        lookups = []
    for author_key, display_name, exact_people, resolution, fallback_used in lookups:
        fallback_count += fallback_used
        if not exact_people:
            continue
        if len(exact_people) > 1:
            unresolved_id = "unresolved:" + hashlib.sha1(
                f"{author_key}|{organization.org_id}".encode("utf-8")
            ).hexdigest()[:16]
            candidate = Candidate(
                person_id=unresolved_id,
                name=display_name,
                org=next(iter(author_affiliations[author_key]), organization.canonical_name),
                org_id=organization.org_id,
                identity_ambiguous=True,
                identity_resolution="unresolved_name_collision",
                author_affiliations=set(author_affiliations[author_key]),
                alternate_profile_ids=[str(row.get("id")) for row in exact_people if row.get("id")],
            )
            candidate.papers = {p["id"]: p for p in author_papers[author_key]}
            by_id[unresolved_id] = candidate
            continue
        for person in exact_people:
            person_id = str(person.get("id") or "")
            if not person_id:
                continue
            candidate = Candidate(
                person_id=person_id,
                name=str(person.get("name") or display_name),
                name_zh=str(person.get("name_zh") or ""),
                org=str(person.get("org") or ""),
                org_zh=str(person.get("org_zh") or ""),
                org_id=str(person.get("org_id") or ""),
                interests=[str(x) for x in (person.get("interests") or [])],
                n_citation=person.get("n_citation") if isinstance(person.get("n_citation"), int) else None,
                identity_ambiguous=resolution == "name_fallback",
                identity_resolution=resolution,
                author_affiliations=set(author_affiliations[author_key]),
            )
            candidate.papers = {p["id"]: p for p in author_papers[author_key]}
            by_id[person_id] = candidate
    if fallback_count:
        warnings.append(f"{fallback_count} candidates used name/organization text fallback and require identity verification")
    return by_id


def classify_org(name: str) -> str:
    value = normalized_text(name)
    industry_terms = (
        "company", "corporation", "corp", "inc", "ltd", "laboratories", "microsoft", "google",
        "meta", "alibaba", "tencent", "huawei", "bytedance", "字节", "腾讯", "阿里", "华为", "百度", "商汤", "旷视",
    )
    academic_terms = ("university", "univ", "college", "institute", "academy", "school", "大学", "学院", "研究院", "科学院", "实验室")
    def classification_match(term: str) -> bool:
        normalized = normalized_text(term)
        if normalized.isascii():
            return re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", value) is not None
        return normalized in value

    if any(classification_match(term) for term in industry_terms):
        return "industry"
    if any(classification_match(term) for term in academic_terms):
        return "academic"
    return "unknown"


def is_university_affiliation(name: str) -> bool:
    """Conservative distinction between a university and another academic institute."""
    value = normalized_text(name)
    return "大学" in value or bool(re.search(
        r"\b(universit\w*|univ|college|institute of technology|polytechnic)\b", value
    ))


def enrich_collaboration(
    client: AMinerClient, candidates: dict[str, Candidate], details: dict[str, dict[str, Any]],
    warnings: list[str], home_organization: ResolvedOrganization | None = None,
) -> None:
    candidate_names: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates.values():
        for name in (candidate.name, candidate.name_zh):
            if name:
                candidate_names[normalized_person_name(name)].append(candidate)
    for detail in details.values():
        authors = [row for row in detail.get("authors") or [] if isinstance(row, dict)]
        involved: dict[str, Candidate] = {}
        for author in authors:
            for candidate in candidate_names.get(normalized_person_name(author.get("name")), []):
                if detail.get("id") in candidate.papers:
                    involved[candidate.person_id] = candidate
        for candidate in involved.values():
            for author in authors:
                org_name = str(author.get("org") or "").strip()
                # Authors without an orgid get a "name:" pseudo-key so they are never
                # sent to org_detail as if the display name were an organization ID.
                org_id = str(author.get("orgid") or "") or f"name:{normalized_text(org_name)}"
                if not org_name:
                    continue
                # Some paper records put the university-level ID on an author
                # while the profile uses a department ID. Compare the explicit
                # affiliation text too, so home departments are not partners.
                if home_organization and contains_any(
                    org_name, [home_organization.query, home_organization.canonical_name,
                               *home_organization.aliases]
                ):
                    continue
                if not home_organization and candidate.org_id and org_id == candidate.org_id:
                    continue
                candidate.collaboration_orgs[org_id] = org_name
                if detail.get("id"):
                    candidate.collaboration_papers.setdefault(org_id, set()).add(str(detail["id"]))

    org_ids = list(dict.fromkeys(
        org_id for candidate in candidates.values() for org_id in candidate.collaboration_orgs
        if org_id and not org_id.startswith("name:")
    ))
    verified_types: dict[str, str] = {}
    if org_ids:
        # A failed org_detail call must not discard already-collected candidates;
        # degrade to name-based classification and report the failure.
        try:
            rows = unwrap(client.call("org_detail", {"ids": org_ids[:100]}))
        except AMinerAPIError as exc:
            warnings.append(
                f"org_detail failed ({exc.message}); collaboration types fall back to name-based classification"
            )
            rows = []
        for row in rows:
            org_id = str(row.get("id") or row.get("org_id") or "")
            raw_type = normalized_text(row.get("type"))
            if any(term in raw_type for term in ("enterprise", "company", "industry")):
                verified_types[org_id] = "industry"
            elif any(term in raw_type for term in ("university", "academic", "institute", "education")):
                verified_types[org_id] = "academic"
    for candidate in candidates.values():
        candidate.collaboration_types = {
            org_id: verified_types.get(org_id, classify_org(name)) for org_id, name in candidate.collaboration_orgs.items()
        }


def supplement_collaboration_papers(
    client: AMinerClient, candidates: dict[str, Candidate],
    details: dict[str, dict[str, Any]], organization: ResolvedOrganization,
    terms: list[str], warnings: list[str],
) -> None:
    """Recover partner papers hidden beyond institution-scoped topic samples.

    Search a few exact scholar names without an org-string filter, then demand
    the selected person ID and university affiliation in paper_detail. This
    widens recall without converting a same-name paper into collaboration.
    """
    eligible = [c for c in candidates.values() if not c.identity_ambiguous
                and not c.person_id.startswith("unresolved:") and c.name]
    eligible.sort(key=lambda c: (
        sum(p.get("topic_evidence") in {"title", "keywords"} for p in c.papers.values()),
        c.n_citation or 0), reverse=True)
    selected = eligible[:6]
    focus = [term for term in terms if re.search(r"[a-z]", term, re.I)][:4]
    if not selected or not focus:
        return
    rows_by_id: dict[str, dict[str, Any]] = {}
    owners: dict[str, set[str]] = defaultdict(set)
    for candidate in selected:
        for term in focus:
            try:
                rows = unwrap(client.call("paper_search_pro", {
                    "author": candidate.name, "query": term, "order": "n_citation",
                    "page": 0, "size": 12, "year_from": CURRENT_YEAR - 5,
                    "year_to": CURRENT_YEAR,
                }))
            except AMinerAPIError as exc:
                warnings.append(f"{candidate.display_name}: collaboration author pass {term} failed ({exc.message})")
                continue
            for row in rows[:3]:
                paper_id = str(row.get("id") or "")
                if not paper_id or paper_id in candidate.papers or len(rows_by_id) >= 72:
                    continue
                rows_by_id.setdefault(paper_id, row)
                owners[paper_id].add(candidate.person_id)
    fetched = fetch_paper_details(client, list(rows_by_id), 72, warnings)
    school_names = [organization.query, organization.canonical_name, *organization.aliases]
    added = 0
    for paper_id, detail in fetched.items():
        for candidate_id in owners[paper_id]:
            candidate = candidates[candidate_id]
            own_author = next((author for author in detail.get("authors") or []
                               if isinstance(author, dict)
                               and str(author.get("id") or "") == candidate.person_id
                               and (str(author.get("orgid") or "") == organization.org_id
                                    or contains_any(str(author.get("org") or ""), school_names))), None)
            if own_author is None:
                continue
            evidence = clean_paper(rows_by_id[paper_id], detail, terms)
            if evidence is None:
                continue
            candidate.papers[paper_id] = evidence
            details[paper_id] = detail
            added += 1
    if added:
        warnings.append(f"author-and-university-verified collaboration pass added {added} direction-paper attribution(s)")


def score_candidates(
    candidates: dict[str, Candidate], direction_terms: list[str], school: str, department: str, profile: dict[str, Any] | None
) -> None:
    profile_text = json.dumps(
        {key: profile.get(key, []) for key in ("research_interests", "research_projects", "publications", "internships")},
        ensure_ascii=False,
    ) if profile else ""
    profile_tokens = tokens(profile_text)
    for candidate in candidates.values():
        matched_papers = [paper for paper in candidate.papers.values() if paper.get("direction_term_match")]
        direct_papers = [paper for paper in matched_papers
                         if paper.get("topic_evidence") in {"title", "keywords"}]
        direction_score = min(100.0, 35.0 + 20.0 * len(direct_papers)
                              + 5.0 * (len(matched_papers) - len(direct_papers))
                              + 5.0 * len({t for p in matched_papers for t in p.get("matched_terms", [])}))
        recent = [p for p in matched_papers if int(p["year"]) >= CURRENT_YEAR - 5]
        activity_score = min(100.0, len(recent) * 25.0)
        affiliation_text = " ".join([candidate.org, candidate.org_zh, *candidate.author_affiliations])
        affiliation_score = 100.0 if contains_any(affiliation_text, [school]) or candidate.org_id else 0.0
        if department:
            affiliation_score = 100.0 if contains_any(affiliation_text, [department]) else 40.0
        categories = list(candidate.collaboration_types.values())
        academic_score = min(100.0, categories.count("academic") * 20.0) if categories else None
        industry_score = min(100.0, categories.count("industry") * 25.0) if categories else None
        evidence_text = " ".join([candidate.name, candidate.org, candidate.org_zh, *candidate.interests] + [p["title"] for p in matched_papers])
        applicant_score = None
        if profile_tokens:
            applicant_score = min(100.0, 20.0 + len(profile_tokens & tokens(evidence_text)) * 12.5)
        components = [
            (direction_score, 0.35), (activity_score, 0.20), (affiliation_score, 0.15),
            (academic_score, 0.10), (industry_score, 0.10), (applicant_score, 0.10),
        ]
        known_weight = sum(weight for value, weight in components if value is not None)
        overall = sum(float(value) * weight for value, weight in components if value is not None) / known_weight
        candidate.scores = {
            "overall": round(overall, 1), "direction_fit": round(direction_score, 1),
            "recent_activity": round(activity_score, 1), "institution_department_fit": round(affiliation_score, 1),
            "academic_collaboration": academic_score, "industry_collaboration": industry_score,
            "applicant_experience_fit": applicant_score,
        }
        if candidate.role_unverified:
            candidate.confidence = "medium" if candidate.identity_resolution.startswith("org_constrained") and len(matched_papers) >= 2 else "low"
        else:
            candidate.confidence = "high" if len(matched_papers) >= 2 else "medium"


def filter_discipline_conflicts(
    candidates: dict[str, Candidate], direction_terms: list[str], allow_cross_discipline: bool, warnings: list[str]
) -> dict[str, Candidate]:
    if allow_cross_discipline or not any(contains_any(term, COMPUTING_DIRECTION_TERMS) for term in direction_terms):
        return candidates
    kept: dict[str, Candidate] = {}
    removed: list[str] = []
    for person_id, candidate in candidates.items():
        primary = [value for value in (candidate.org, candidate.org_zh) if value]
        # A single paper may carry an old or joint hospital/chemistry affiliation.
        # It must not veto an exact scholar profile at a computing faculty.
        primary_on_field = any(contains_any(value, COMPUTING_AFFILIATION_TERMS) for value in primary)
        primary_off_field = any(contains_any(value, CROSS_DISCIPLINE_AFFILIATION_TERMS) for value in primary)
        all_paper_affiliations_off_field = bool(candidate.author_affiliations) and all(
            contains_any(value, CROSS_DISCIPLINE_AFFILIATION_TERMS)
            for value in candidate.author_affiliations)
        if not primary_on_field and (primary_off_field or all_paper_affiliations_off_field):
            removed.append(f"{candidate.display_name} ({candidate.org_zh or candidate.org})")
        else:
            kept[person_id] = candidate
    if removed:
        preview = "; ".join(removed[:5])
        suffix = f"; and {len(removed) - 5} more" if len(removed) > 5 else ""
        warnings.append(f"filtered {len(removed)} obvious cross-discipline affiliations: {preview}{suffix}")
    return kept


def filter_by_interest_mismatch(
    candidates: dict[str, Candidate], direction_terms: list[str], warnings: list[str],
) -> dict[str, Candidate]:
    """Drop paper-recalled candidates whose OWN interests hit a clearly off-field topic.

    Paper-reverse recall pulls every author of a direction-matched paper, so a
    co-author from another discipline slips in (a 城市规划/核工程/临床 author on a
    paper whose title mentions the direction; the affiliation blacklist misses
    them when their org string isn't obviously 'hospital' etc.).

    Design note (learned the hard way): we use a BLACKLIST here, not a whitelist.
    Whitelisting ("keep only if interests positively match the direction") is a
    disaster for broad fields — for CV it wrongly deletes SLAM / pose / tracking /
    3D / re-id / super-resolution scholars, because "what counts as CV" is an open
    set impossible to enumerate. "What is clearly NOT this field" (clinical
    medicine, nuclear, urban planning, materials, …) is a small closed set, so a
    blacklist removes true noise without harming subfields. Empty interests and
    roster candidates are left untouched.
    """
    kept: dict[str, Candidate] = {}
    removed: list[str] = []
    for person_id, candidate in candidates.items():
        if candidate.identity_resolution == "roster" or not candidate.interests:
            kept[person_id] = candidate
            continue
        interests_text = " ".join(candidate.interests)
        # Remove only when interests clearly hit an off-field discipline AND show
        # no sign of the requested direction.
        if (contains_any(interests_text, CROSS_DISCIPLINE_INTEREST_TERMS)
                and not interest_matches_direction(candidate.interests, direction_terms)):
            removed.append(f"{candidate.display_name}[{','.join(candidate.interests[:2])}]")
        else:
            kept[person_id] = candidate
    if removed:
        preview = "; ".join(removed[:5])
        suffix = f"; and {len(removed) - 5} more" if len(removed) > 5 else ""
        warnings.append(f"filtered {len(removed)} candidate(s) whose own interests are clearly off-field: {preview}{suffix}")
    return kept


def assess_recent_hire(profile_row: dict[str, Any], since: int) -> dict[str, Any]:
    """Classify hire recency from AMiner profile signals.

    AMiner has no hire-date field, so this never asserts a new hire without
    positive evidence: an explicit join year in the bio, a fresh doctorate,
    or a junior title. Established scholars hired recently are undetectable
    and stay in no_hire_evidence.
    """
    bio = " ".join(str(profile_row.get(key) or "") for key in ("bio_zh", "bio"))
    edu = " ".join(str(profile_row.get(key) or "") for key in ("edu_zh", "edu"))
    position = str(profile_row.get("position_zh") or profile_row.get("position") or "").strip()
    signals: list[str] = []

    join_years = [int(m.group(1)) for pattern in HIRE_YEAR_PATTERNS for m in pattern.finditer(bio)]
    if join_years:
        year = max(join_years)
        if year >= since:
            return {"status": "likely_recent", "signals": [f"bio states joining in {year}"]}
        return {"status": "hired_earlier", "signals": [f"bio states joining in {year}, before {since}"]}

    degree_years = [int(y) for y in re.findall(r"(?:19|20)\d{2}", edu)]
    if degree_years and max(degree_years) >= since - 1:
        signals.append(f"latest degree year {max(degree_years)}")
    if position and contains_any(position, JUNIOR_POSITION_TERMS):
        signals.append(f"junior position: {position}")
    if signals:
        return {"status": "likely_recent", "signals": signals}
    return {"status": "no_hire_evidence", "signals": []}


def enrich_from_portrait(client: AMinerClient, candidate: Candidate, hired_since: int) -> None:
    """Pull Scholar Portrait (person/figure, paid) for structured career/education.

    person_detail only exposes a free-text bio; the portrait endpoint returns
    structured `works` (org + start_year/end_year + position) and `edus`. This
    (a) records a real career/education timeline — the task-history depth AMiner
    was wrongly assumed unable to provide — and (b) derives a precise join year
    for hire screening from the works entry matching the candidate's institution,
    far more reliable than regex over the bio.
    """
    try:
        rows = unwrap(client.call("person_figure", {"id": candidate.person_id}))
    except AMinerAPIError:
        return
    if not rows:
        return
    fig = rows[0]

    def clean(entry: dict[str, Any], role_key: str) -> dict[str, Any]:
        return {
            "org": str(entry.get("org") or ""),
            "department": str(entry.get("department") or ""),
            "position": str(entry.get(role_key) or entry.get("position_extra") or ""),
            "start_year": entry.get("start_year"),
            "end_year": entry.get("end_year"),
        }

    candidate.career = [clean(w, "position_extra") for w in (fig.get("works") or []) if isinstance(w, dict)][:8]
    candidate.education = [clean(e, "position_extra") for e in (fig.get("edus") or []) if isinstance(e, dict)][:6]

    if not hired_since or not candidate.org_id:
        return
    # Precise join year: earliest works entry whose org matches this institution.
    org_names = [normalized_text(x) for x in (candidate.org, candidate.org_zh) if x]
    join_years = [
        w["start_year"] for w in candidate.career
        if isinstance(w.get("start_year"), int)
        and any(name and (name in normalized_text(w["org"]) or normalized_text(w["org"]) in name) for name in org_names)
    ]
    if join_years:
        year = min(join_years)
        if year >= hired_since:
            candidate.hire_assessment = {"status": "likely_recent",
                                         "signals": [f"portrait: joined {candidate.org} in {year}"]}
        else:
            candidate.hire_assessment = {"status": "hired_earlier",
                                         "signals": [f"portrait: joined {candidate.org} in {year}, before {hired_since}"]}


def verify_candidate_roles(
    client: AMinerClient, candidates: dict[str, Candidate], limit: int, warnings: list[str], hired_since: int = 0,
    with_portrait: bool = False,
) -> None:
    ranked = sorted(candidates.values(), key=lambda row: (len(row.papers), row.n_citation or 0), reverse=True)[:limit]
    for candidate in ranked:
        if candidate.person_id.startswith("unresolved:"):
            continue
        # A failed person_detail lookup keeps the candidate with an unverified role
        # instead of aborting the whole run.
        try:
            rows = unwrap(client.call("person_detail", {"id": candidate.person_id}))
        except AMinerAPIError as exc:
            warnings.append(
                f"person_detail failed for {candidate.display_name} ({exc.message}); role remains unverified"
            )
            continue
        if not rows:
            continue
        row = rows[0]
        role = str(row.get("position_zh") or row.get("position") or "").strip()
        candidate.role = role or None
        if hired_since:
            candidate.hire_assessment = assess_recent_hire(row, hired_since)
        if with_portrait:
            # Structured career/education + precise join year; may override the
            # bio-regex hire_assessment above with a portrait-derived one.
            enrich_from_portrait(client, candidate, hired_since)
        if role and contains_any(role, NON_FACULTY_TERMS):
            candidate.role_unverified = True
            candidate.scores["role_conflict"] = 1.0
        elif role and contains_any(role, FACULTY_TERMS):
            candidate.role_unverified = False
        else:
            candidate.role_unverified = True
    if limit:
        warnings.append(f"role verification requested for {len(ranked)} shortlisted profiles; empty AMiner position fields remain unverified")


def applicant_readiness(profile: dict[str, Any]) -> float:
    score = 20.0
    percentile = profile.get("rank_percentile")
    if isinstance(percentile, (int, float)):
        score += max(0.0, min(30.0, (50.0 - float(percentile)) * 0.75))
    gpa, scale = profile.get("gpa"), profile.get("gpa_scale")
    if isinstance(gpa, (int, float)) and isinstance(scale, (int, float)) and scale:
        score += max(0.0, min(20.0, float(gpa) / float(scale) * 20.0))
    score += min(10.0, len(profile.get("research_projects") or []) * 3.0)
    score += min(10.0, len(profile.get("publications") or []) * 5.0)
    score += min(5.0, len(profile.get("internships") or []) * 2.0)
    return min(100.0, score)


def institution_level(name: str, tiers: dict[str, Any]) -> int:
    value = normalized_text(name).replace("’", "'")
    # Map well-known English school names to the Chinese tier-list entries; AMiner
    # canonical names are often English and org_search aliases are not guaranteed
    # to include the Chinese name.
    for english, chinese in (tiers.get("english_names") or {}).items():
        if normalized_text(english).replace("’", "'") == value:
            value = normalized_text(chinese)
            break
    for tier_name, level in (("985", 3), ("华五", 3), ("211", 2), ("双一流", 1)):
        if any(normalized_text(school) == value for school in tiers["tiers"].get(tier_name, {}).get("schools", [])):
            return level
    # Applicants often describe their institution generically ("某211大学",
    # "双非一本院校") instead of naming it; read the tier phrase instead of
    # collapsing everything unmatched to the lowest level.
    if "双非" in value or "非985" in value or "非211" in value:
        return 1 if "一本" in value else 0
    if "985" in value or "华五" in value:
        return 3
    if "211" in value:
        return 2
    if "双一流" in value or "一本" in value:
        return 1
    # Do not silently promote an unlisted institution above an ordinary undergraduate
    # institution. Strong non-211 institutions can be supplied in an explicit tier later.
    return 0


def school_difficulty(school: str, tiers: dict[str, Any]) -> int:
    return institution_level(school, tiers)


def school_level_map(
    schools: Iterable[str], organization_cache: dict[str, ResolvedOrganization | None], tiers: dict[str, Any]
) -> dict[str, int]:
    """Level each queried school using every resolved name variant (English canonical,
    Chinese aliases), so an English school name still matches the Chinese tier lists."""
    levels: dict[str, int] = {}
    for school in schools:
        names = {school}
        organization = organization_cache.get(school)
        if organization:
            names.update([organization.canonical_name, *organization.aliases])
        levels[school] = max(institution_level(name, tiers) for name in names if name)
    return levels


def assign_bands(
    candidates: dict[str, Candidate], profile: dict[str, Any], tiers: dict[str, Any],
    school_levels: dict[str, int] | None = None,
) -> None:
    readiness = applicant_readiness(profile)
    undergraduate_level = institution_level(str(profile.get("undergraduate_institution") or ""), tiers)
    levels = school_levels or {}
    for candidate in candidates.values():
        target_level = max(
            [levels.get(school, school_difficulty(school, tiers)) for school in candidate.source_schools] or [2]
        )
        threshold = {3: 82.0, 2: 72.0, 1: 62.0, 0: 55.0}[target_level]
        gap_penalty = max(0, target_level - undergraduate_level) * 7.0
        fit_adjustment = (float(candidate.scores.get("applicant_experience_fit") or 40.0) - 50.0) * 0.15
        adjusted = readiness - gap_penalty + fit_adjustment
        if adjusted >= threshold + 10:
            candidate.recommendation_band = "相对稳妥（启发式）"
        elif adjusted >= threshold - 3:
            candidate.recommendation_band = "匹配（启发式）"
        else:
            candidate.recommendation_band = "冲刺（启发式）"


def mark_duplicate_names(candidates: dict[str, Candidate]) -> dict[str, Candidate]:
    groups: dict[tuple[str, tuple[str, ...]], list[Candidate]] = defaultdict(list)
    for candidate in candidates.values():
        groups[(normalized_person_name(candidate.display_name), tuple(sorted(candidate.source_schools)))].append(candidate)
    for rows in groups.values():
        if len(rows) <= 1:
            continue
        ids = [row.person_id for row in rows]
        for row in rows:
            row.identity_ambiguous = True
            row.alternate_profile_ids = [person_id for person_id in ids if person_id != row.person_id]
    return candidates


def backfill_chinese_names(client: AMinerClient, candidates: list[Candidate], warnings: list[str]) -> None:
    """Fill a Chinese name only when it belongs to the selected person ID."""
    filled = 0
    unresolved = 0
    for cand in candidates:
        if cand.name_zh.strip() or cand.person_id.startswith("unresolved:"):
            continue
        try:
            rows = unwrap(client.call("person_search", {"name": cand.name, "size": 5}))
        except AMinerAPIError:
            continue
        same_person = next((r.get("name_zh") for r in rows
                            if str(r.get("id") or "") == cand.person_id and r.get("name_zh")), None)
        if same_person:
            cand.name_zh = str(same_person)
            filled += 1
            continue
        unresolved += 1
    if filled:
        warnings.append(f"backfilled Chinese names from exact person IDs for {filled} candidate(s)")
    if unresolved:
        warnings.append(f"{unresolved} candidate(s) have no Chinese name in AMiner; shown by English name")


def select_profile_portfolio(candidates: list[Candidate], limit: int,
                             named_schools: list[str] | None = None) -> list[Candidate]:
    """Expose a strong lead from each named school before filling by fit band."""
    labels = ("冲刺（启发式）", "匹配（启发式）", "相对稳妥（启发式）")
    ratios = {labels[0]: 0.4, labels[1]: 0.4, labels[2]: 0.2}
    groups = {label: [row for row in candidates if row.recommendation_band == label] for label in labels}
    selected: list[Candidate] = []
    selected_ids: set[str] = set()
    if named_schools:
        requested = set(named_schools)
        covered: set[str] = set()
        # candidates are already sorted by quality. Each selected lead adds a
        # school absent from the visible shortlist, when the pool has one.
        for row in candidates:
            new_schools = (row.source_schools & requested) - covered
            if not new_schools:
                continue
            selected.append(row)
            selected_ids.add(row.person_id)
            covered.update(new_schools)
            if len(selected) >= limit or covered == requested:
                break
    for label in labels:
        quota = max(1, round(limit * ratios[label]))
        for row in groups[label][:quota]:
            if len(selected) >= limit:
                break
            if row.person_id in selected_ids:
                continue
            selected.append(row)
            selected_ids.add(row.person_id)
    for row in candidates:
        if len(selected) >= limit:
            break
        if row.person_id not in selected_ids:
            selected.append(row)
            selected_ids.add(row.person_id)
    return selected[:limit]


def school_research_leads(candidates: list[Candidate], schools: list[str],
                          per_school_limit: int = 5) -> dict[str, list[dict[str, Any]]]:
    """Keep a small, explicitly unverified search pool visible for every school.

    The overall portfolio can hide a school's second or third candidate. These
    rows are paper/interest leads, never claims of current faculty or eligibility.
    """
    result: dict[str, list[dict[str, Any]]] = {school: [] for school in schools}
    for school in schools:
        rows = [candidate for candidate in candidates if school in candidate.source_schools]
        chosen: list[tuple[Candidate, dict[str, Any] | None]] = []
        seen_papers: set[str] = set()
        citation_rows = sorted(
            (row for row in rows if not row.identity_ambiguous and row.n_citation is not None
             and any(p.get("topic_evidence") in {"title", "keywords"} for p in row.papers.values())),
            key=lambda row: row.n_citation or 0, reverse=True,
        )
        # Surface a school's strong but low-scoring senior scholar as a lead;
        # broad portfolio scores otherwise hide a one-paper field landmark.
        # Citation volume never verifies faculty status or task fit.
        for pool, quota, distinct_paper_only in (
            (rows, min(3, per_school_limit), True),
            (citation_rows, per_school_limit, False),
            (rows, per_school_limit, False),
        ):
            for candidate in pool:
                if len(chosen) >= quota:
                    break
                if any(existing.person_id == candidate.person_id for existing, _ in chosen):
                    continue
                first_paper = next((paper for paper in candidate.papers.values()
                                    if paper.get("direction_term_match")), None)
                paper_key = str((first_paper or {}).get("id") or (first_paper or {}).get("title") or "")
                if distinct_paper_only and paper_key in seen_papers:
                    continue
                chosen.append((candidate, first_paper))
                if paper_key:
                    seen_papers.add(paper_key)
                if len(chosen) >= quota:
                    break
        result[school] = [{
            "name": candidate.display_name,
            "person_id": candidate.person_id,
            "aminer_url": None if candidate.person_id.startswith("unresolved:")
            else f"https://www.aminer.cn/profile/{candidate.person_id}",
            "interests": candidate.interests[:2],
            "evidence_paper": {key: paper.get(key) for key in ("id", "title", "year", "aminer_url")}
            if paper else None,
            "identity_ambiguous": candidate.identity_ambiguous,
            "status": "research lead only; verify current faculty identity, degree role and task fit",
        } for candidate, paper in chosen]
    return result


def screen_recent_hires(ranked: list[Candidate], window_start: int) -> tuple[list[Candidate], dict[str, Any]]:
    """Split candidates for a recent-hire query: only faculty with positive hire
    evidence enter the pool; students/postdocs are excluded even when their fresh
    degree year looks like a hire signal."""
    def status(candidate: Candidate) -> str:
        return (candidate.hire_assessment or {}).get("status", "unassessed")

    def non_faculty(candidate: Candidate) -> bool:
        return bool(candidate.role and contains_any(candidate.role, NON_FACULTY_TERMS))

    pool = [c for c in ranked if status(c) == "likely_recent" and not non_faculty(c)]
    screening = {
        "window_start": window_start,
        "likely_recent_count": len(pool),
        "excluded_non_faculty": [
            {"name": c.display_name, "position": c.role}
            for c in ranked if status(c) == "likely_recent" and non_faculty(c)
        ],
        "excluded_hired_earlier": [
            {"name": c.display_name, "signals": (c.hire_assessment or {}).get("signals", [])}
            for c in ranked if status(c) == "hired_earlier"
        ],
        "no_hire_evidence": [
            {"name": c.display_name, "position": c.role}
            for c in ranked if status(c) in ("no_hire_evidence", "unassessed")
        ][:20],
        "method_note": (
            "AMiner has no hire-date field. likely_recent requires positive evidence: a bio join "
            "year in the window, a doctorate finished near it, or a junior title. Established "
            "scholars hired recently are undetectable here and stay under no_hire_evidence — "
            "never present them as confirmed new hires; verify against official department "
            "announcements instead."
        ),
    }
    return pool, screening


def build_rank_key(mode: str, collaboration_type: str, rank_by: str):
    """Return a sort key for the requested ranking dimension (used with reverse=True)."""
    def rank_key(candidate: Candidate) -> tuple[float, ...]:
        overall = float(candidate.scores.get("overall") or 0)
        if mode == "collaboration":
            count = len(candidate.collaboration_orgs) if collaboration_type == "all" else sum(
                value == collaboration_type for value in candidate.collaboration_types.values()
            )
            partner_papers = set().union(*(
                candidate.collaboration_papers.get(org_id, set())
                for org_id in candidate.collaboration_orgs
                if collaboration_type == "all" or candidate.collaboration_types.get(org_id) == collaboration_type
            ))
            independent_papers = len(partner_papers)
            direct = sum(paper.get("topic_evidence") in {"title", "keywords"}
                         for paper in candidate.papers.values())
            if rank_by == "citation":
                return (float(candidate.n_citation or 0), float(direct > 0),
                        float(independent_papers), float(count), overall)
            if rank_by == "recent":
                recent = sum(int(paper.get("year") or 0) >= CURRENT_YEAR - 3
                             for paper in candidate.papers.values())
                return (float(direct > 0), float(recent), float(independent_papers),
                        float(count), overall)
            if rank_by == "rising":
                recent = min(3, sum(int(paper.get("year") or 0) >= CURRENT_YEAR - 3
                                    for paper in candidate.papers.values()))
                return (float(direct > 0), float(recent), -float(candidate.n_citation or 0),
                        float(independent_papers), float(count), overall)
            return (float(direct > 0), float(independent_papers >= 2),
                    float(independent_papers), float(count), overall)
        years = [int(paper["year"]) for paper in candidate.papers.values()]
        if rank_by == "citation":
            return (float(candidate.n_citation or 0), overall)
        if rank_by == "recent":
            recent = sum(1 for year in years if year >= CURRENT_YEAR - 3)
            return (float(recent), float(max(years, default=0)), overall)
        if rank_by == "rising":
            # Heuristic rising-star view: require recent direction evidence (capped so a
            # high-volume established PI cannot win on count alone), then prefer the
            # smaller citation base — established PIs rank below earlier-career scholars.
            recent = min(3, sum(1 for year in years if year >= CURRENT_YEAR - 3))
            return (float(recent), float(max(years, default=0)), -float(candidate.n_citation or 0))
        return (overall, float(len(candidate.papers)))
    return rank_key


def collaboration_alternate_leads(
    candidates: list[Candidate], displayed: list[Candidate], limit: int = 6,
) -> list[dict[str, Any]]:
    """Expose established authors hidden by single-paper coauthor-count rank.

    Citation count is only a discovery signal. These rows still need an official
    faculty/degree check and individual collaboration-paper verification.
    """
    shown = {candidate.person_id for candidate in displayed}
    eligible = [candidate for candidate in candidates
                if candidate.person_id not in shown and not candidate.identity_ambiguous
                and any(paper.get("topic_evidence") in {"title", "keywords"}
                        for paper in candidate.papers.values())
                and isinstance(candidate.n_citation, int)]
    selected = sorted(eligible, key=lambda candidate: candidate.n_citation or 0, reverse=True)[:limit]
    return [{
        "name": candidate.display_name,
        "person_id": candidate.person_id,
        "aminer_url": f"https://www.aminer.cn/profile/{candidate.person_id}",
        "n_citation": candidate.n_citation,
        "evidence_papers": [
            {key: paper.get(key) for key in ("id", "title", "year", "aminer_url")}
            for paper in list(candidate.papers.values())[:2]
        ],
        "observed_partner_organizations": len(candidate.collaboration_orgs),
        "status": "exploratory research lead; current faculty, degree role and collaboration breadth unverified",
    } for candidate in selected]


def task_specific_alternate_leads(
    candidates: list[Candidate], displayed: list[Candidate], schools: list[str],
    direction_terms: list[str], per_school_limit: int = 2,
) -> dict[str, list[dict[str, Any]]]:
    """Surface exact task specialists hidden by a broad portfolio score.

    A coauthor with one direct paper and several precise interest tags can rank
    below a prolific broad-field author. These are discovery leads, not verified
    faculty or degree supervisors.
    """
    shown = {candidate.person_id for candidate in displayed}
    generic_terms = {"vision", "computer vision", "deep learning", "machine learning",
                     "artificial intelligence", "ai", "机器人", "人工智能", "深度学习",
                     "计算机视觉", "图像", "多模态"}
    specific_focus = [term for term in direction_terms[1:]
                      if term.strip().casefold() not in generic_terms]
    focus = specific_focus or direction_terms[1:] or direction_terms
    result: dict[str, list[dict[str, Any]]] = {}
    for school in schools:
        ranked: list[tuple[float, int, Candidate]] = []
        for candidate in candidates:
            if school not in candidate.source_schools or candidate.person_id in shown or candidate.identity_ambiguous:
                continue
            if not any(p.get("topic_evidence") in {"title", "keywords"} for p in candidate.papers.values()):
                continue
            matching_interests = [interest for interest in candidate.interests
                                  if contains_any(interest, focus)]
            if not matching_interests:
                continue
            ranked.append((len(matching_interests) / max(1, len(candidate.interests)),
                           len(matching_interests), candidate))
        ranked.sort(key=lambda item: (item[0], item[1], item[2].scores.get("overall") or 0), reverse=True)
        result[school] = [{
            "name": candidate.display_name,
            "person_id": candidate.person_id,
            "aminer_url": f"https://www.aminer.cn/profile/{candidate.person_id}",
            "matching_interests": [interest for interest in candidate.interests
                                   if contains_any(interest, focus)][:4],
            "evidence_paper": next(({
                key: paper.get(key) for key in ("id", "title", "year", "aminer_url")
            } for paper in candidate.papers.values()
                if paper.get("topic_evidence") in {"title", "keywords"}), None),
            "status": "task-specific research lead; current faculty and requested degree role require official proof",
        } for _, _, candidate in ranked[:per_school_limit]]
    return result


def estimate_cost(
    mode: str, school_count: int, alias_count: int, paper_limit: int, verify_roles: int,
    profile_discovery: bool = False, discovery_alias_count: int = 0,
) -> dict[str, Any]:
    search_calls = alias_count * max(1, school_count) if mode != "discover" or school_count else alias_count
    # Each school can make an ID-scoped QA pass, a name fallback, and one
    # bounded global pass. The estimate must include these possible calls.
    global_search_calls = search_calls if mode != "discover" else 0
    qa_search_calls = (2 * search_calls if mode == "discover" and school_count else search_calls)
    if mode == "discover" and not school_count:
        qa_search_calls = 0
    if mode == "discover" and school_count:
        qa_search_calls += school_count  # possible medical-image topic breadth pass
    if mode in {"profile", "collaboration"} and school_count:
        qa_search_calls += school_count * (min(2, alias_count - 1) if alias_count > 1 else 1)
    pro_search_calls = (2 * search_calls if mode != "discover" or school_count else search_calls) + global_search_calls
    if mode == "discover" and school_count:
        pro_search_calls += 6 * school_count  # abstract/title views plus possible keyword breadth pass
    if mode in {"named", "tier", "collaboration", "profile"}:
        pro_search_calls += (8 if mode == "collaboration" else 6) * school_count
    details_per_school = 84
    alias_probe_details = 3 if mode in {"named", "tier", "collaboration", "profile"} else 0
    detail_calls = ((school_count * (min(36, max(16, 4 * paper_limit)) if mode == "discover"
                                     else 2 * details_per_school + alias_probe_details))
                    if school_count else alias_count * paper_limit)
    if mode == "collaboration":
        pro_search_calls += 24 * school_count  # six exact scholars, four branch terms
        detail_calls += 72 * school_count     # bounded author-and-school checks
    org_search_calls = school_count
    # Collaboration enrichment and role verification run independently for each school.
    # Discover currently aggregates author affiliations directly and does not call org_detail.
    org_detail_calls = school_count if mode == "collaboration" else 0
    person_detail_calls = verify_roles * school_count if mode != "discover" else 0
    discovery_search_calls = (discovery_alias_count or alias_count) if profile_discovery else 0
    discovery_detail_calls = discovery_search_calls * paper_limit
    estimate = (
        (pro_search_calls + discovery_search_calls) * API_SPEC["paper_search_pro"][2]
        + qa_search_calls * API_SPEC["paper_qa_search"][2]
        + (detail_calls + discovery_detail_calls) * API_SPEC["paper_detail"][2]
        + org_search_calls * API_SPEC["org_search"][2]
        + org_search_calls * API_SPEC["org_disambiguate_pro"][2]
        + org_detail_calls * API_SPEC["org_detail"][2]
        + person_detail_calls * API_SPEC["person_detail"][2]
    )
    return {
        "worst_case_cny": round(estimate, 2), "paper_search_pro_calls": pro_search_calls + discovery_search_calls,
        "paper_qa_search_calls": qa_search_calls, "org_disambiguate_pro_calls": org_search_calls,
        "paper_detail_calls": detail_calls + discovery_detail_calls, "org_search_calls": org_search_calls,
        "org_detail_calls": org_detail_calls, "person_detail_calls": person_detail_calls,
        "profile_discovery_calls": discovery_search_calls,
    }


def estimate_person_cost(name_count: int, paper_limit: int, deep: bool, focus_alias_count: int,
                         broad_context: bool = False) -> dict[str, Any]:
    """Bound a focused name search, author checks, and optional ID-linked fallback."""
    searches = min(6, focus_alias_count) + 1 if focus_alias_count else 1
    detail_calls = name_count * paper_limit if focus_alias_count else 0
    relation_calls = name_count  # only used when current-org search has no usable recent paper
    cost = name_count * searches * API_SPEC["paper_search_pro"][2]
    cost += detail_calls * API_SPEC["paper_detail"][2]
    cost += relation_calls * API_SPEC["person_paper_relation"][2]
    cost += name_count * int(deep) * API_SPEC["person_detail"][2]
    if broad_context and focus_alias_count:
        cost += name_count * (2 * API_SPEC["paper_search_pro"][2]
                              + 40 * API_SPEC["paper_detail"][2]
                              + API_SPEC["paper_qa_search"][2])
        searches += 2
        detail_calls += name_count * 40
        # Robotics field recall adds at most three searches and 24 checks.
        cost += name_count * (3 * API_SPEC["paper_search_pro"][2]
                              + 24 * API_SPEC["paper_detail"][2])
        searches += 3
        detail_calls += name_count * 24
    return {"worst_case_cny": round(cost, 2), "paper_search_pro_calls": name_count * searches,
            "paper_qa_search_calls": name_count if broad_context and focus_alias_count else 0,
            "paper_detail_calls": detail_calls, "person_paper_relation_calls": relation_calls,
            "person_detail_calls": name_count * int(deep)}


DEPARTMENT_SEGMENT = re.compile(
    r"^(school|department|dept\.?|faculty|institute|college|laboratory|lab|key lab|center|centre|research)\b",
    re.IGNORECASE,
)


def extract_institution_name(affiliation: str) -> str:
    """Reduce a paper-affiliation string to its university main body.

    "School of CS, Beijing Technology and Business University, Beijing, PR China"
    -> "Beijing Technology and Business University".
    """
    text = str(affiliation or "").strip()
    match = re.search(r"[一-鿿]{2,14}(?:大学|学院)", text)
    if match:
        return match.group(0)
    for segment in re.split(r"[,;]", text):
        segment = segment.strip()
        if not segment or DEPARTMENT_SEGMENT.match(segment):
            continue
        if re.search(r"\b(university|institute of technology|academy of sciences)\b", segment, re.IGNORECASE):
            return segment
    return text


def is_mainland_china_institution(affiliation: str) -> bool:
    """Heuristic mainland filter on the raw paper-affiliation string.

    AMiner also returns Chinese aliases for overseas organizations, so resolved
    aliases must not be used as a domestic signal.
    """
    text = str(affiliation or "")
    lowered = normalized_text(text)
    if any(term in lowered for term in ("hong kong", "macau", "macao", "taiwan", "香港", "澳门", "台湾")):
        return False
    if re.search(r"[一-鿿]", text):
        return True
    return re.search(r"\bchina\b", lowered) is not None


def choose_profile_expansion_schools(
    profile: dict[str, Any], current: list[str], institutions: list[dict[str, Any]],
    tiers: dict[str, Any], limit: int,
) -> list[str]:
    """Add direction-evidenced schools below the user's hardest target level."""
    if len(current) >= limit:
        return []
    existing = {normalized_text(school) for school in current}
    undergraduate_level = institution_level(str(profile.get("undergraduate_institution") or ""), tiers)
    hardest_target = max([school_difficulty(school, tiers) for school in current] or [undergraduate_level + 1])
    eligible: list[tuple[int, int, str]] = []
    for index, row in enumerate(institutions):
        name = str(row.get("organization_main") or row.get("organization") or "").strip()
        aliases = [str(value) for value in row.get("organization_aliases") or []]
        if not aliases:
            continue  # never add a school AMiner could not resolve to an organization
        identity_names = [name, *aliases]
        if not name or any(normalized_text(value) in existing for value in identity_names):
            continue
        level = max(school_difficulty(value, tiers) for value in identity_names)
        # Domestic signal: the raw affiliation names mainland China, or the school
        # matches a known tier list (covers resolved names without a country marker).
        if level == 0 and not is_mainland_china_institution(str(row.get("organization") or "")):
            continue
        if classify_org(" ".join(identity_names)) != "academic":
            continue
        if not contains_any(" ".join(identity_names), ("university", "college", "大学", "学院")):
            continue
        if level >= hardest_target and current:
            continue
        eligible.append((level, index, name))

    selected: list[str] = []
    # Prefer one school near the applicant's level, then stronger lower-tier schools;
    # preserve AMiner evidence order within a level.
    for desired_level in (undergraduate_level, 1, 2, 3):
        for level, _, name in eligible:
            if level == desired_level and name not in selected:
                selected.append(name)
                break
        if len(current) + len(selected) >= limit:
            return selected
    for _, _, name in sorted(eligible, key=lambda item: (-item[0], item[1])):
        if name not in selected:
            selected.append(name)
        if len(current) + len(selected) >= limit:
            break
    return selected


def should_auto_expand_profile(mode: str, explicit_schools: bool,
                               no_auto_expand: bool, force_auto_expand: bool) -> bool:
    """A named school set is a boundary unless expansion is explicitly requested."""
    return (mode == "profile" and not no_auto_expand
            and (force_auto_expand or not explicit_schools))


def interest_matches_direction(interests: list[str], direction_terms: list[str]) -> bool:
    """Looser match for roster augmentation: a scholar's interest tag counts as
    on-direction if it shares enough salient word-stems with a direction term.

    AMiner interest tags use different word forms than query aliases
    ("Language Modeling" vs "large language model"), so exact substring matching
    misses real matches. Compare on stemmed content words instead: require every
    content word of some (>=2-word) direction phrase to appear as a token stem in
    the interests, or a direct substring hit for short/Chinese terms.
    """
    if not interests:
        return False
    blob = normalized_text(" ".join(interests))
    if contains_any(blob, direction_terms):
        return True
    STOP = {"the", "a", "of", "for", "and", "based", "large", "deep", "using"}

    def stems(text: str) -> set[str]:
        out = set()
        for w in re.findall(r"[a-z]{3,}", normalized_text(text)):
            if w in STOP:
                continue
            out.add(w[:-1] if w.endswith("s") else w)          # crude singular
            out.add(w[:-3] if w.endswith("ing") and len(w) > 5 else w)  # model(ing)
        return out
    itok = stems(blob)
    for term in direction_terms:
        words = [w for w in re.findall(r"[a-z]{3,}", normalized_text(term)) if w not in STOP]
        if len(words) >= 2 and all(any(s.startswith(w[:5]) or w.startswith(s[:5]) for s in itok) for w in words):
            return True
    return False


def augment_from_roster(
    client: AMinerClient, organization: ResolvedOrganization, direction_terms: list[str],
    existing: dict[str, Candidate], warnings: list[str], *, max_pages: int, direction_evidence: bool,
) -> None:
    """Augment paper-reverse recall with the institution scholar roster.

    Paper-reverse recall misses in-faculty PIs whose papers didn't fall into the
    sampled set (the head-scholar recall gap). This pages the free person_search
    endpoint by org_id (undocumented: offset paging works and reaches high-citation
    PIs the paper sample misses) and adds direction-matched scholars not already
    found. Direction is judged by each scholar's own interests, so cross-discipline
    noise is excluded by construction. Added candidates are tagged
    identity_resolution='roster' and require the same downstream verification.
    Free: person_search costs nothing, so paging carries no API charge.
    """
    if not organization.org_id or max_pages <= 0:
        return
    seen_ids = set(existing)
    seen_names = {normalized_person_name(c.name) for c in existing.values()}
    seen_names |= {normalized_person_name(c.name_zh) for c in existing.values() if c.name_zh}
    added = 0
    empty_streak = 0
    for offset in range(0, max_pages * 10, 10):
        try:
            rows = unwrap(client.call("person_search", {"org_id": [organization.org_id], "size": 10, "offset": offset}))
        except AMinerAPIError as exc:
            warnings.append(f"roster augmentation stopped at offset {offset} ({exc.message})")
            break
        new_rows = [r for r in rows if str(r.get("id") or "") not in seen_ids]
        if not rows or (not new_rows and offset > 0):
            empty_streak += 1
            if empty_streak >= 2:  # roster exhausted (paging returns only dupes)
                break
            continue
        empty_streak = 0
        for person in rows:
            pid = str(person.get("id") or "")
            if str(person.get("org_id") or "") != organization.org_id:
                continue
            display = str(person.get("name_zh") or person.get("name") or "")
            key = normalized_person_name(display)
            if not pid or pid in seen_ids or key in seen_names:
                continue
            interests = [str(x) for x in (person.get("interests") or [])]
            if not interest_matches_direction(interests, direction_terms):
                continue
            cand = Candidate(
                person_id=pid,
                name=str(person.get("name") or display),
                name_zh=str(person.get("name_zh") or ""),
                org=str(person.get("org") or ""),
                org_zh=str(person.get("org_zh") or ""),
                org_id=str(person.get("org_id") or organization.org_id),
                interests=interests,
                n_citation=person.get("n_citation") if isinstance(person.get("n_citation"), int) else None,
                identity_resolution="roster",
            )
            existing[pid] = cand
            seen_ids.add(pid)
            seen_names.add(key)
            added += 1
    if added:
        warnings.append(
            f"roster augmentation added {added} in-faculty scholar(s) matched by interest but missed by paper recall; "
            "verify direction on the profile page"
        )


def recommend_for_school(
    client: AMinerClient, school: str, department: str, direction: str, aliases: list[str], paper_limit: int,
    profile: dict[str, Any] | None, max_author_lookups: int, allow_name_fallback: bool,
    verify_roles: int, warnings: list[str], organization_cache: dict[str, ResolvedOrganization | None],
    include_collaboration: bool, allow_cross_discipline: bool, hired_since: int = 0, roster_pages: int = 0,
    with_portrait: bool = False, candidate_limit: int = 10, citation_seed: bool = False,
) -> dict[str, Candidate]:
    organization = organization_cache.get(school)
    if school not in organization_cache:
        organization = resolve_organization(client, school)
        organization_cache[school] = organization
    if organization is None:
        warnings.append(f"organization not found in AMiner: {school}; try an official English name or discover mode")
        return {}
    terms = school_search_terms(direction, aliases, profile)
    # Limit paid QA queries, not the vocabulary used to judge retrieved papers.
    # The first three aliases can omit a genuine subfield (e.g. navigation in an
    # embodied-intelligence request), silently deleting its authors after recall.
    evidence_terms = direction_aliases(direction, aliases)
    compound_focus = industrial_few_shot_focus(evidence_terms)
    if compound_focus:
        evidence_terms = list(dict.fromkeys([
            *evidence_terms, "defect detection", "anomaly detection", "few-shot",
            "few-shot anomaly detection", "few-shot defect detection",
            "few-data defect detection", "少样本异常检测",
        ]))
    if include_collaboration and ("embodied" in normalized_text(direction) or "具身智能" in direction):
        # The broad keyword pass can recover a genuinely embodied-agent paper
        # titled without the literal phrase "embodied intelligence". Retain its
        # explicit subfield phrase through the downstream topic check.
        evidence_terms = direction_aliases(direction, [*aliases, "embodied agent"])
    search_rows: dict[str, dict[str, Any]] = {}
    rows_by_term: list[list[dict[str, Any]]] = []
    for term in terms:
        term_rows = paper_search(client, term, organization, paper_limit)
        rows_by_term.append(term_rows)
        for row in term_rows:
            if row.get("id"):
                search_rows[str(row["id"])] = row
    if citation_seed:
        # Mix recent citation-ranked papers with the newest-year slice so
        # established faculty can enter the bounded identity checks.
        seed_terms = terms[1:3] if len(terms) > 1 else terms
        for term in seed_terms:
            cited_rows = unwrap(client.call("paper_qa_search", {
                "use_topic": True, "query": term, "org_id": [organization.org_id],
                "size": paper_limit, "offset": 0, "force_citation_sort": True,
            }))
            cited_rows = [row for row in cited_rows if isinstance(row.get("year"), int)
                          and CURRENT_YEAR - 5 <= row["year"] <= CURRENT_YEAR]
            rows_by_term.append(cited_rows)
            for row in cited_rows:
                if row.get("id"):
                    search_rows.setdefault(str(row["id"]), row)
    if not search_rows and normalized_text(organization.canonical_name) != normalized_text(school):
        warnings.append(f"no papers found with canonical name {organization.canonical_name}; retried original name {school}")
        for term in terms:
            term_rows = paper_search(client, term, school, paper_limit)
            rows_by_term.append(term_rows)
            for row in term_rows:
                if row.get("id"):
                    search_rows[str(row["id"])] = row
    learn_english_school_alias(client, organization, rows_by_term, warnings)
    title_samples = complementary_title_search(client, organization, evidence_terms, warnings,
                                                keep_primary_broad=include_collaboration)
    abstract_samples = complementary_abstract_search(client, organization, evidence_terms, warnings)
    for sample in title_samples:
        for row in sample:
            if row.get("id"):
                search_rows.setdefault(str(row["id"]), row)
    for sample in abstract_samples:
        for row in sample:
            if row.get("id"):
                search_rows.setdefault(str(row["id"]), row)
    detail_cap = 84
    # Reserve independent inspection slots for all three retrieval paths.
    title_limit = 40
    abstract_limit = 26
    qa_limit = detail_cap - title_limit - abstract_limit
    primary_count = 30
    title_ids = list(dict.fromkeys(
        [*bounded_paper_ids(title_samples[:1], primary_count),
         *bounded_paper_ids(title_samples[1:], title_limit)]
    ))[:title_limit]
    abstract_ids = ranked_abstract_ids(abstract_samples, abstract_limit)
    qa_ids = bounded_paper_ids(rows_by_term, qa_limit)
    selected_ids = list(dict.fromkeys([*title_ids, *abstract_ids, *qa_ids]))[:detail_cap]
    if len(search_rows) > len(selected_ids):
        warnings.append(f"{school}: inspected {len(selected_ids)} of {len(search_rows)} matched papers across direction terms")
    info = batch_paper_info(client, selected_ids, warnings)
    info_by_id = {str(row.get("id")): row for row in info if row.get("id")}
    details = fetch_paper_details(client, selected_ids, detail_cap, warnings)
    papers = [dict(search_rows[paper_id], **info_by_id.get(paper_id, {})) for paper_id in selected_ids]
    candidates = resolve_people(
        client, papers, details, organization, evidence_terms, max_author_lookups, allow_name_fallback, warnings
    )
    # One bounded global pass is enough to recover useful authors when the
    # institution-scoped sample is sparse. Identity is still checked below.
    if not candidates:
        global_size = max(paper_limit, 20)
        global_rows: dict[str, dict[str, Any]] = dict(search_rows)
        global_by_term: list[list[dict[str, Any]]] = []
        for term in terms:
            term_rows = paper_search(client, term, None, global_size)
            global_by_term.append(term_rows)
            for row in term_rows:
                pid = str(row.get("id") or "")
                if pid:
                    global_rows.setdefault(pid, row)
        if len(global_rows) > len(search_rows):
            new_ids = [pid for pid in bounded_paper_ids(global_by_term, detail_cap) if pid not in search_rows]
            if len(global_rows) - len(search_rows) > len(new_ids):
                warnings.append(f"{school}: global fallback inspected {len(new_ids)} new papers; remaining hits were not assessed")
            g_info = batch_paper_info(client, new_ids, warnings)
            g_info_by_id = dict(info_by_id)
            g_info_by_id.update({str(row.get("id")): row for row in g_info if row.get("id")})
            g_details = dict(details)
            g_details.update(fetch_paper_details(client, new_ids, detail_cap, warnings))
            g_papers = [dict(global_rows[pid], **g_info_by_id.get(pid, {})) for pid in selected_ids + new_ids]
            g_candidates = resolve_people(
                client, g_papers, g_details, organization, evidence_terms, max_author_lookups,
                allow_name_fallback, warnings,
            )
            details = g_details
            added = 0
            for key, cand in g_candidates.items():
                if key not in candidates:
                    candidates[key] = cand
                    added += 1
            if added:
                warnings.append(
                    f"global direction fallback added {added} author(s) at {school} "
                    f"(org-scoped paper search returned too few)"
                )
    for candidate in candidates.values():
        candidate.source_schools.add(school)
    candidates = filter_discipline_conflicts(candidates, evidence_terms, allow_cross_discipline, warnings)
    if not allow_cross_discipline:
        candidates = filter_by_interest_mismatch(candidates, evidence_terms, warnings)
    if roster_pages:
        augment_from_roster(
            client, organization, evidence_terms, candidates, warnings,
            max_pages=roster_pages, direction_evidence=False,
        )
        for candidate in candidates.values():
            candidate.source_schools.add(school)
    if include_collaboration:
        supplement_collaboration_papers(client, candidates, details, organization, evidence_terms, warnings)
        enrich_collaboration(client, candidates, details, warnings, organization)
    if compound_focus:
        candidates = retain_joint_topic_candidates(candidates, details, warnings)
    score_candidates(candidates, evidence_terms, school, department, profile)
    if verify_roles:
        verify_candidate_roles(client, candidates, verify_roles, warnings, hired_since, with_portrait)
        score_candidates(candidates, evidence_terms, school, department, profile)
    return candidates


def discover_institutions(
    client: AMinerClient, direction: str, aliases: list[str], paper_limit: int, warnings: list[str],
    schools: list[str] | None = None,
) -> list[dict[str, Any]]:
    terms = direction_aliases(direction, aliases)[:6]
    if schools:
        compared: list[dict[str, Any]] = []
        detail_cap = min(36, max(16, 4 * paper_limit))
        representative_limit, recent_example_limit = (2, 1) if len(schools) >= 9 else (3, 2)
        multimodal_llm_focus = any(contains_any(term, ("multimodal large language model", "多模态大模型"))
                                   for term in terms)
        # A paper's title and keyword list can express the two halves of this
        # compound topic separately (CogVLM: VLM in title; Multimodal Learning
        # and Large Language Model as separate keywords). These extra terms
        # affect local evidence matching, not the paid search-query budget.
        evidence_terms = [*terms, "multimodal", "vlm", "visual-linguistic", "vision-language"] \
            if multimodal_llm_focus else terms
        medical_image_focus = any("medical imag" in term.lower() or "医学影像" in term
                                  for term in terms)
        medical_segmentation_focus = medical_image_focus and contains_any(
            direction, ("segmentation", "segmenting", "分割", "勾画"))
        industrial_few_shot_task = industrial_few_shot_focus(terms)
        if industrial_few_shot_task:
            evidence_terms = list(dict.fromkeys([
                *evidence_terms, "defect detection", "anomaly detection", "few-shot",
                "few-shot anomaly detection", "few-shot defect detection",
                "few-data defect detection", "少样本异常检测",
            ]))
        abstract_seed = "medical image" if medical_image_focus else discovery_abstract_seed(terms)
        if abstract_seed and abstract_seed not in evidence_terms:
            evidence_terms = [*evidence_terms, abstract_seed]
        for school in schools:
            organization = resolve_organization(client, school)
            if organization is None:
                warnings.append(f"discover: could not resolve first-level organization ID for {school}")
                compared.append({"organization": school, "organization_id": None,
                                 "matched_paper_count": 0, "inspected_paper_count": 0,
                                 "evidence_papers": [], "coverage": "unresolved_organization"})
                continue
            rows_by_term: list[list[dict[str, Any]]] = []
            search_rows: dict[str, dict[str, Any]] = {}
            def retrieve_both_orders(term: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
                recent = paper_search(client, term, organization, paper_limit)
                influential = unwrap(client.call("paper_qa_search", {
                    "use_topic": True, "query": term, "org_id": [organization.org_id],
                    "size": paper_limit, "offset": 0, "force_citation_sort": True,
                }))
                if multimodal_llm_focus:
                    # QA citation sorting can still return pure language-model
                    # hits ahead of the direct multimodal work. Preserve its
                    # citation order within each relevance group.
                    influential = sorted(influential, key=lambda row: multimodal_llm_paper(row, None), reverse=True)
                return recent, influential
            # These independent searches are the latency bottleneck in a
            # multi-school comparison; keep their original term order while
            # using a small bounded pool for network waits.
            with ThreadPoolExecutor(max_workers=min(4, len(terms))) as executor:
                paired_samples = list(executor.map(retrieve_both_orders, terms))
            for recent, influential in paired_samples:
                for sample in (recent, influential):
                    current = [row for row in sample if isinstance(row.get("year"), int)
                               and CURRENT_YEAR - 5 <= row["year"] <= CURRENT_YEAR]
                    rows_by_term.append(current)
                    for row in current:
                        paper_id = str(row.get("id") or "")
                        if not paper_id:
                            continue
                        stored = search_rows.setdefault(paper_id, dict(row))
                        if row.get("n_citation_bucket"):
                            stored["n_citation_bucket"] = row["n_citation_bucket"]
            abstract_samples = complementary_abstract_search(
                client, organization, [abstract_seed] if abstract_seed else [], warnings)
            for sample in abstract_samples:
                for row in sample:
                    if row.get("id"):
                        search_rows.setdefault(str(row["id"]), row)
            title_samples = [] if medical_image_focus else complementary_title_search(
                client, organization, terms, warnings)
            for sample in title_samples:
                for row in sample:
                    if row.get("id"):
                        search_rows.setdefault(str(row["id"]), row)
            paired_samples: list[list[dict[str, Any]]] = []
            if industrial_few_shot_task:
                # An OR-like per-term head sample can miss the actual
                # intersection. The documented grouped-topic query is an
                # independent lead path; detail attribution and the local
                # compound predicate still decide what counts.
                try:
                    grouped = json.dumps([
                        ["industrial defect detection", "surface defect detection", "anomaly detection"],
                        ["few-shot", "few-shot learning", "few-shot segmentation", "few-data"],
                    ])
                    rows = unwrap(client.call("paper_qa_search", {
                        "use_topic": True, "topic_high": grouped,
                        "org_id": [organization.org_id], "size": max(20, paper_limit),
                    }))
                    current = [row for row in rows if isinstance(row.get("year"), int)
                               and CURRENT_YEAR - 5 <= row["year"] <= CURRENT_YEAR]
                    paired_samples.append(current)
                    for row in current:
                        if row.get("id"):
                            search_rows.setdefault(str(row["id"]), row)
                except AMinerAPIError as exc:
                    warnings.append(f"{school}: grouped few-shot defect search failed ({exc.message})")
            keyword_rows: list[dict[str, Any]] = []
            recent_topic_rows: list[dict[str, Any]] = []
            keyword_total_hits: int | None = None
            recent_topic_total_hits: int | None = None
            if medical_image_focus:
                # The general academic workflow exposes broader school-level
                # totals and highly cited papers that a short org-ID QA head
                # sample can miss. These totals are fuzzy retrieval signals;
                # only inspected author affiliations count as evidence below.
                try:
                    payload = client.call("paper_search_pro", {
                        "keyword": "medical image", "org": organization.canonical_name,
                        "size": 30, "page": 0, "order": "n_citation",
                    })
                    keyword_total_hits = payload.get("total") if isinstance(payload.get("total"), int) else None
                    keyword_rows = [row for row in unwrap(payload) if isinstance(row.get("year"), int)
                                    and CURRENT_YEAR - 5 <= row["year"] <= CURRENT_YEAR]
                except AMinerAPIError as exc:
                    warnings.append(f"{school}: keyword school breadth search failed ({exc.message})")
                topic_groups = json.dumps([
                    ["medical image", "medical imaging"],
                    ["deep learning", "neural network", "machine learning", "artificial intelligence"],
                ])
                try:
                    payload = client.call("paper_qa_search", {
                        "use_topic": True, "topic_high": topic_groups,
                        "org_id": [organization.org_id],
                        "year": list(range(CURRENT_YEAR - 3, CURRENT_YEAR + 1)),
                        "size": 20, "force_year_sort": True,
                    })
                    recent_topic_total_hits = payload.get("total") if isinstance(payload.get("total"), int) else None
                    recent_topic_rows = [row for row in unwrap(payload) if isinstance(row.get("year"), int)
                                         and CURRENT_YEAR - 5 <= row["year"] <= CURRENT_YEAR]
                except AMinerAPIError as exc:
                    warnings.append(f"{school}: recent topic breadth search failed ({exc.message})")
                for row in [*keyword_rows, *recent_topic_rows]:
                    if row.get("id"):
                        search_rows.setdefault(str(row["id"]), row)
            if medical_image_focus:
                selected_ids = list(dict.fromkeys([
                    *bounded_paper_ids([keyword_rows], min(8, detail_cap // 4)),
                    *bounded_paper_ids([recent_topic_rows], min(8, detail_cap // 4)),
                    *ranked_abstract_ids(abstract_samples, min(12, detail_cap // 3)),
                    *bounded_paper_ids(rows_by_term, detail_cap),
                ]))[:detail_cap]
            else:
                title_limit = min(12, detail_cap // 3)
                abstract_limit = min(8, detail_cap // 4)
                # Draw QA up to the full cap so sparse title/abstract views do
                # not leave detail slots idle and lose otherwise reachable work.
                # The multimodal-LLM branch has an additional direct-topic
                # ordering and must retain its smaller QA quota: generic LLM
                # papers can otherwise consume the remaining sample.
                qa_limit = (detail_cap - title_limit - abstract_limit
                            if multimodal_llm_focus else detail_cap)
                selected_ids = list(dict.fromkeys([
                    *bounded_paper_ids(paired_samples, min(12, detail_cap // 3)),
                    *bounded_paper_ids(title_samples, title_limit),
                    *ranked_abstract_ids(abstract_samples, abstract_limit),
                    *bounded_paper_ids(rows_by_term, qa_limit),
                ]))[:detail_cap]
            info_by_id = {str(row.get("id")): row for row in batch_paper_info(client, selected_ids, warnings)
                          if row.get("id")}
            details = fetch_paper_details(client, selected_ids, detail_cap, warnings)
            evidence: list[dict[str, Any]] = []
            weak_topic: list[dict[str, Any]] = []
            adjacent_topic: list[dict[str, Any]] = []
            affiliation_conflicts = 0
            school_names = school_affiliation_names(organization)
            for paper_id in selected_ids:
                detail = details.get(paper_id)
                merged = dict(search_rows[paper_id], **info_by_id.get(paper_id, {}))
                cleaned = clean_paper(merged, detail, evidence_terms)
                if not cleaned or not (CURRENT_YEAR - 5 <= cleaned["year"] <= CURRENT_YEAR):
                    continue
                authors = (detail or {}).get("authors") or []
                affiliation_conflicts += sum(
                    1 for author in authors if isinstance(author, dict)
                    and str(author.get("orgid") or "") == organization.org_id
                    and str(author.get("org") or "").strip()
                    and not contains_any(str(author["org"]), school_names)
                )
                # AMiner sometimes assigns the same orgid to authors whose
                # explicit affiliations name several different institutions.
                # Count only an author whose affiliation text names this school.
                if not any(isinstance(author, dict) and
                           contains_any(str(author.get("org") or ""), school_names)
                           for author in authors):
                    continue
                if cleaned["topic_evidence"] == "abstract_only":
                    weak_topic.append(cleaned)
                elif medical_segmentation_focus and not medical_segmentation_paper(merged, detail):
                    adjacent_topic.append(cleaned)
                elif industrial_few_shot_task and not industrial_few_shot_defect_paper(merged, detail):
                    adjacent_topic.append(cleaned)
                elif medical_image_focus and cleaned["topic_evidence"] == "keywords" \
                        and not medical_imaging_title_focus(cleaned["title"]) \
                        and not contains_any(" ".join(str(k) for k in (detail or {}).get("keywords") or []), (
                            "medical image segmentation", "medical image classification",
                            "medical image reconstruction", "medical image registration",
                            "medical image detection", "computer-aided diagnosis",
                        )):
                    adjacent_topic.append(cleaned)
                elif multimodal_llm_focus and not multimodal_llm_paper(merged, detail):
                    adjacent_topic.append(cleaned)
                else:
                    evidence.append(cleaned)
            distinct: dict[str, dict[str, Any]] = {}
            def citation_floor(paper: dict[str, Any]) -> int:
                match = re.search(r"\d+", str(paper.get("n_citation_bucket") or ""))
                return int(match.group()) if match else 0
            for paper in evidence:
                key = normalized_text(paper["title"]).rstrip(". ")
                if key not in distinct or citation_floor(paper) > citation_floor(distinct[key]):
                    distinct[key] = paper
            unique_evidence = list(distinct.values())
            representative = sorted(
                unique_evidence, key=lambda paper: (citation_floor(paper), paper["year"]), reverse=True
            )[:representative_limit]
            representative_ids = {paper["id"] for paper in representative}
            recent_examples = [paper for paper in sorted(unique_evidence, key=lambda paper: paper["year"], reverse=True)
                               if paper["id"] not in representative_ids][:recent_example_limit]
            recent_count = sum(paper["year"] >= CURRENT_YEAR - 2 for paper in unique_evidence)
            influential_count = sum(citation_floor(paper) >= 201 for paper in unique_evidence)
            flagship_count = sum(citation_floor(paper) >= 1001 for paper in unique_evidence)
            compared.append({"organization": school,
                             "aminer_organization_name": organization.canonical_name,
                             "organization_id": organization.org_id,
                             "matched_paper_year_range": f"{CURRENT_YEAR - 5}-{CURRENT_YEAR}",
                             "recent_direction_paper_year_range": f"{CURRENT_YEAR - 2}-{CURRENT_YEAR}",
                             "keyword_total_hits": keyword_total_hits,
                             "recent_topic_total_hits": recent_topic_total_hits,
                             "matched_paper_count": len(unique_evidence),
                             "recent_direction_paper_count": recent_count,
                             "influential_direction_paper_count": influential_count,
                             "flagship_paper_count": flagship_count,
                             "inspected_paper_count": len(selected_ids),
                             "search_hit_count": len(search_rows),
                             "affiliation_conflicts": affiliation_conflicts,
                             "abstract_only_paper_count": len(weak_topic),
                             "adjacent_topic_paper_count": len(adjacent_topic),
                             "compound_focus": "industrial defect detection AND few-shot visual learning"
                             if industrial_few_shot_task else None,
                             "representative_papers": representative,
                             "evidence_papers": recent_examples,
                             "evidence_papers_omitted_count": len(unique_evidence) - len(representative) - len(recent_examples),
                             "coverage": "bounded_org_id_sample"})
        # The supplied comparison set is preserved. A fixed-size sample count
        # cannot support a national ranking, especially across different topics.
        return compared
    search_rows: dict[str, dict[str, Any]] = {}
    for term in terms:
        for row in paper_search(client, term, None, paper_limit):
            if row.get("id"):
                search_rows[str(row["id"])] = row
    info = batch_paper_info(client, list(search_rows), warnings)
    info_by_id = {str(row.get("id")): row for row in info if row.get("id")}
    details = fetch_paper_details(client, list(search_rows), len(search_rows), warnings)
    counts: Counter[str] = Counter()
    names: dict[str, str] = {}
    evidence: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for paper_id, detail in details.items():
        merged = dict(search_rows.get(paper_id, {}), **info_by_id.get(paper_id, {}))
        cleaned = clean_paper(merged, detail, terms)
        if not cleaned:
            continue
        seen: set[str] = set()
        for author in detail.get("authors") or []:
            if not isinstance(author, dict):
                continue
            org_id, org_name = str(author.get("orgid") or ""), str(author.get("org") or "").strip()
            if not org_id or not org_name or org_id in seen:
                continue
            seen.add(org_id)
            counts[org_id] += 1
            names[org_id] = org_name
            evidence[org_id][paper_id] = cleaned
    return [
        {"organization_id": org_id, "organization": names[org_id], "matched_paper_count": count,
         "evidence_papers": list(evidence[org_id].values()), "coverage": "global_seed_sample"}
        for org_id, count in counts.most_common()
    ]


def mentor_followup_plan(schools: list[str], institutions: list[dict[str, Any]]) -> dict[str, Any]:
    """Expose low-yield school samples that still merit person-level recall.

    The bounded paper inspection can miss a school's best-matching faculty. A large
    search pool with few verified sampled titles signals uncertainty, not a
    negative result, so these schools should not vanish before mentor search.
    """
    rows = []
    for school, institution in zip(schools, institutions):
        if institution.get("coverage") != "bounded_org_id_sample":
            continue
        rows.append({"school": school,
                     "matched_paper_count": int(institution.get("matched_paper_count") or 0),
                     "search_hit_count": int(institution.get("search_hit_count") or 0),
                     "inspected_paper_count": int(institution.get("inspected_paper_count") or 0)})
    if not rows:
        return {"thin_sample_schools": [], "minimum_thin_sample_probes": 0}
    counts = sorted(row["matched_paper_count"] for row in rows)
    cutoff = counts[max(0, len(counts) // 3 - 1)]
    thin = [row for row in rows
            if row["matched_paper_count"] <= cutoff and row["search_hit_count"] >= 20]
    thin.sort(key=lambda row: (row["search_hit_count"],
                               -row["matched_paper_count"]), reverse=True)
    return {
        "thin_sample_schools": thin,
        "minimum_thin_sample_probes": min(2, len(thin)) if len(rows) > 5 else 0,
        "note": "These schools had many retrieved hits but few verified titles in a bounded sample. "
                "Before selecting five schools, run named-school mentor searches on at least the "
                "indicated number; include a zero-count school when present. A low count is not "
                "evidence that no directly fitting faculty member exists.",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("named", "tier", "collaboration", "profile", "discover", "person"))
    parser.add_argument("--direction", default="", help="Required for all modes except person")
    parser.add_argument("--names", default="", help="Comma-separated scholar name(s) for person mode; multiple names = comparison")
    parser.add_argument("--deep", action="store_true", help="person mode: also fetch person_detail (¥1/person) for position/education/honors")
    parser.add_argument("--broad-context", action="store_true", help="person mode: add author-verified, citation-ranked recent work beyond the focus-query sample")
    parser.add_argument("--aliases", default="")
    parser.add_argument("--school", default="")
    parser.add_argument("--schools", default="")
    parser.add_argument("--department", default="")
    parser.add_argument("--tier", default="")
    parser.add_argument("--tiers-file", type=Path, default=DEFAULT_TIERS)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--paper-limit", type=int, default=10)
    parser.add_argument("--candidate-limit", type=int, default=10)
    parser.add_argument("--max-schools", type=int, default=5)
    parser.add_argument("--max-author-lookups", type=int, default=40)
    parser.add_argument("--collaboration-type", choices=("all", "academic", "industry"), default="all")
    parser.add_argument(
        "--rank-by", choices=("overall", "citation", "recent", "rising"), default="overall",
        help="Ranking dimension: overall score, total citations, recent activity, or heuristic rising stars",
    )
    parser.add_argument("--allow-name-fallback", action="store_true")
    parser.add_argument("--allow-cross-discipline", action="store_true")
    parser.add_argument("--verify-roles", type=int, default=0, help="Paid person_detail calls for top N candidates")
    parser.add_argument(
        "--hired-since", type=int, default=0,
        help="Screen for likely-recent hires since this year; implies person_detail verification for the shortlist",
    )
    parser.add_argument(
        "--roster-pages", type=int, default=0,
        help="Augment paper recall with up to N pages of the institution scholar roster (free person_search org paging, 10 scholars/page) to recover in-faculty PIs missed by paper sampling",
    )
    parser.add_argument(
        "--with-portrait", action="store_true",
        help="For verified candidates, fetch Scholar Portrait (person/figure, ¥0.5 each) for structured career/education history and a precise institution join year; implies --verify-roles",
    )
    parser.add_argument("--max-cost", type=float, default=5.0)
    parser.add_argument("--yes", action="store_true", help="Allow a worst-case estimate above --max-cost")
    expansion = parser.add_mutually_exclusive_group()
    expansion.add_argument("--no-auto-expand-profile", action="store_true",
                           help="Search only the selected profile schools")
    expansion.add_argument("--auto-expand-profile", action="store_true",
                           help="Explicitly add evidence-backed schools beyond named profile targets")
    parser.add_argument("--output", type=Path)
    return parser


def write_result(result: dict[str, Any], output: Path | None) -> None:
    # Multi-school discovery can exceed a model tool-result window even though
    # the actual evidence is bounded. Keep saved artifacts readable and use a
    # slightly tighter, still line-broken representation on stdout.
    if output:
        rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    visible = dict(result)
    if output:
        visible["saved_to"] = str(output)
    if result.get("workflow") in {"named", "tier", "collaboration", "profile"}:
        if result.get("workflow") == "collaboration":
            # Keep the overlooked-author and official-navigation channels ahead
            # of the large candidate portfolio in tool windows that clip stdout.
            visible = {key: visible[key] for key in (
                "workflow", "as_of_date", "query", "official_seed_leads",
                "alternate_collaboration_leads", "official_profile_hints",
            ) if key in visible} | {key: value for key, value in visible.items()
                                  if key not in {"workflow", "as_of_date", "query",
                                                 "official_seed_leads", "alternate_collaboration_leads",
                                                 "official_profile_hints"}}
        compact_candidates = []
        for candidate in result.get("candidates") or []:
            compact = {key: value for key, value in candidate.items()
                       if key not in {"needs_verification", "author_affiliations",
                                      "alternate_profile_ids", "career", "education"}
                       and value not in (None, [], {})}
            papers = compact.get("evidence_papers") or []
            paper_cap = {"profile": 2, "collaboration": 2}.get(result.get("workflow"), 3)
            if len(papers) > paper_cap:
                compact["evidence_paper_count"] = len(papers)
                compact["evidence_papers"] = sorted(
                    papers, key=lambda p: (
                        {"title": 2, "keywords": 1}.get(p.get("topic_evidence"), 0),
                        p.get("year") or 0,
                    ), reverse=True,
                )[:paper_cap]
            if result.get("workflow") == "collaboration":
                organizations = compact.get("collaboration_organizations") or []
                compact["collaboration_organization_count"] = len(organizations)
                compact["collaboration_university_count"] = sum(
                    bool(row.get("is_university")) for row in organizations)
                compact["collaboration_organizations"] = [
                    {key: row[key] for key in ("name", "type", "is_university", "paper_urls")
                     if key in row}
                    for row in sorted(organizations, key=lambda row: (
                        bool(row.get("is_university")), len(row.get("paper_urls") or [])),
                    reverse=True)[:4]
                ]
                if len(organizations) > 4:
                    compact["collaboration_organizations_omitted"] = len(organizations) - 4
                if len(compact.get("interests") or []) > 8:
                    compact["interests"] = compact["interests"][:8]
            compact_candidates.append(compact)
        visible["candidates"] = compact_candidates
    # Model tools often clip long stdout at 30k chars. Always return a compact
    # evidence view, even when a complete --output artifact was also saved.
    rendered = json.dumps(visible, ensure_ascii=False, separators=(",", ":")) + "\n"
    sys.stdout.write(rendered)


def paper_topic_relevant(title: str, interests: list[str]) -> bool:
    """Whether a paper title shares a token with the scholar's interest vocabulary.

    Used only as a soft signal to FLAG (not drop) possible same-name mixin — pure
    token overlap is too brittle to safely discard papers (an on-topic paper may
    use different words than the interest phrases), so callers keep all papers and
    surface the low-overlap ones for human check.
    """
    if not interests:
        return True
    itok: set[str] = set()
    for term in interests:
        itok |= tokens(term)
    return bool(tokens(title) & itok)


FOCUS_GENERIC_TOKENS = {
    "llm", "large", "language", "model", "models", "machine", "deep", "learning",
    "artificial", "intelligence", "research", "study", "studies",
    "for", "with", "and", "the", "from", "using", "via", "towards", "toward",
    "into", "over", "under", "based", "this", "that", "through", "between",
}


FOCUS_TOKEN_EQUIVALENTS = {
    "coding": "code", "coded": "code", "program": "code", "programming": "code",
    "programs": "code", "generating": "generation", "generated": "generation",
    "generate": "generation", "generative": "generation", "synthesis": "generation",
    "agents": "agent", "agentic": "agent", "reason": "reasoning",
}


def focus_tokens(text: str) -> set[str]:
    return {FOCUS_TOKEN_EQUIVALENTS.get(token, token) for token in tokens(text.replace("-", " "))}


def focus_topic_relevant(text: str, focus_terms: list[str]) -> bool:
    """Require the distinctive part of a named-scholar research focus.

    Broad recall aliases such as "LLM" can find papers, but must not make every
    generic LLM paper a positive match for a narrower "LLM code reasoning" ask.
    """
    paper_tokens = focus_tokens(text)
    def anchor(term: str) -> set[str]:
        return {token for token in focus_tokens(term) if token not in FOCUS_GENERIC_TOKENS
                and re.fullmatch(r"[a-z][a-z0-9+.-]{1,}", token)}
    primary_anchor = anchor(focus_terms[0]) if focus_terms else set()
    if focus_terms and not primary_anchor and contains_any(text, [focus_terms[0]]):
        return True
    if "code" in primary_anchor and re.search(r"\b(coding|programming)\b", text, re.I):
        # Coding-agent and long-horizon coding titles are relevant to code
        # generation, even when they do not repeat the word "generation".
        # Require a second software-task cue so qualitative "coding" is not enough.
        if paper_tokens & {"agent", "requirements", "software", "repository",
                           "completion", "reasoning", "generation", "llm"}:
            return True
    for term in focus_terms:
        term_anchor = anchor(term)
        # Generic aliases improve retrieval but cannot turn an unrelated paper
        # into a positive match. Keep aliases tied to a distinctive primary focus.
        # A two-part focus needs two specific pieces of evidence. A one-word
        # alias such as "code" is too broad and also matches unrelated acronyms.
        # A specific two-token alias can be a synonym without literal overlap
        # (program synthesis/code generation).
        if term_anchor and len(term_anchor) >= min(2, len(primary_anchor)):
            if term_anchor <= paper_tokens:
                return True
    return not any(anchor(term) for term in focus_terms) and contains_any(text, focus_terms)


def broad_context_topic_relevant(text: str, focus_terms: list[str]) -> bool:
    """Permit title-language variants only in the optional broad LLM portrait."""
    if focus_topic_relevant(text, focus_terms):
        return True
    primary = normalized_text(focus_terms[0]) if focus_terms else ""
    return primary in {"large language model", "large language models", "llm"} and bool(
        re.search(r"\b(?:language models?|llms?|mllms?)\b", text, re.I))


def broad_person_field_recall_terms(focus_terms: list[str], seed_titles: list[str]) -> list[str]:
    """Use broad robotics branches already visible in an attributed sample."""
    focus = normalized_text(" ".join(focus_terms))
    if not contains_any(focus, ("robot", "embodied", "机器人", "具身")):
        return []
    titles = normalized_text(" ".join(seed_titles))
    terms = ["robot"]
    if contains_any(titles, ("force", "tactile", "触觉", "力觉")):
        terms.append("force")
    if contains_any(titles, ("humanoid", "人形")):
        terms.append("humanoid")
    return terms


def _confident_person_pick(rows: list[dict[str, Any]], school: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Decide whether disambiguation is confident enough to deep-dive one scholar.

    Returns (chosen, contenders). chosen is None when the name is too ambiguous to
    pick safely (common surname resolving to many distinct people) — the caller then
    returns a disambiguation list instead of silently deep-diving a guess.

    Confident when: (a) a school was given and exactly one candidate's org matches it,
    or (b) no school but the top candidate's citations dominate (>= 3x the runner-up).
    """
    def cit(r: dict[str, Any]) -> int:
        return r.get("n_citation") if isinstance(r.get("n_citation"), int) else 0

    if school:
        matched = [r for r in rows if normalized_text(school) in normalized_text(f"{r.get('org')} {r.get('org_zh')}")]
        if len(matched) == 1:
            return matched[0], []
        if len(matched) > 1:
            matched.sort(key=cit, reverse=True)
            return None, matched[:6]          # school given but several match → ask
        # A citation lead cannot override an explicit school constraint.
        return None, sorted(rows, key=cit, reverse=True)[:6]
    ordered = sorted(rows, key=cit, reverse=True)
    top = ordered[0]
    runner = cit(ordered[1]) if len(ordered) > 1 else 0
    if len(ordered) == 1 or (cit(top) >= 200 and cit(top) >= 3 * max(runner, 1)):
        return top, []
    return None, ordered[:6]                   # ambiguous → ask


def linked_recent_papers(
    client: AMinerClient, person_id: str, focus_terms: list[str], limit: int,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Recover works linked to a scholar ID when current-org name search misses them.

    The relation endpoint has no year filter. Its IDs are ingestion-time-like, not
    publication dates, so inspect at most 200 recent-looking IDs and verify the
    actual years with batched paper_info before presenting any paper.
    """
    try:
        relation = unwrap(client.call("person_paper_relation", {"id": person_id}))
    except AMinerAPIError as exc:
        warnings.append(f"人物论文关系查询失败（{person_id}）：{exc.message}")
        return []
    relation_rows = [row for row in relation
                     if str(row.get("author_id") or "") == person_id and row.get("id")]
    ids = list(dict.fromkeys(str(row["id"]) for row in relation_rows))
    if not ids:
        return []
    # Relation IDs encode ingestion order only imperfectly. Reserve the first
    # part of the bounded paper_info sample for matching titles across the FULL
    # relation, so a prolific scholar's focused work is not lost below ID #200.
    focused = list(dict.fromkeys(
        str(row["id"]) for row in relation_rows
        if focus_terms and focus_topic_relevant(str(row.get("title") or ""), focus_terms)
    ))
    selected = list(dict.fromkeys(sorted(focused, reverse=True)[:100] + sorted(ids, reverse=True)))[:200]
    if len(ids) > len(selected):
        warnings.append(f"人物论文关系有 {len(ids)} 篇，仅核对其中 200 个较新入库 ID；不能据此断言其他年份无成果")
    info: list[dict[str, Any]] = []
    for offset in range(0, len(selected), 100):
        try:
            info.extend(unwrap(client.call("paper_info", {"ids": selected[offset:offset + 100]})))
        except AMinerAPIError as exc:
            warnings.append(f"人物关联论文年份核验失败（{person_id}）：{exc.message}")
            break
    papers = []
    for row in info:
        year = row.get("year")
        title = str(row.get("title") or "").strip()
        paper_id = str(row.get("id") or "")
        if not title or not paper_id or not isinstance(year, int):
            continue
        if year < CURRENT_YEAR - 5 or year > CURRENT_YEAR:
            continue
        # paper_info only gives a short abstract slice and no validated topic
        # label. Incidental wording there caused finance/education papers to be
        # mislabelled as code-generation work. Keep those as broad recent context.
        topic_match = focus_topic_relevant(title, focus_terms) if focus_terms else True
        papers.append({"year": year, "title": title, "recent": True,
                       "topic_match": topic_match, "source": "person_paper_relation",
                       "aminer_url": f"https://www.aminer.cn/pub/{paper_id}"})
    return select_representative_papers(papers, limit)


def select_representative_papers(papers: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Keep focus evidence first while covering more than the newest calendar year."""
    chosen: list[dict[str, Any]] = []
    unique: dict[str, dict[str, Any]] = {}
    for paper in papers:
        key = re.sub(r"[\W_]+", "", normalized_text(paper.get("title")))
        if key:
            unique.setdefault(key, paper)
    yearly_cap = max(2, (limit + 1) // 2)
    for on_topic in (True, False):
        group = sorted(
            (p for p in unique.values() if bool(p.get("topic_match")) == on_topic),
            key=lambda p: (bool(p.get("recent")),
                           p.get("year") if isinstance(p.get("year"), int) else 0,
                           str(p.get("title") or "")), reverse=True,
        )
        counts: Counter[int] = Counter()
        first_pass: list[dict[str, Any]] = []
        overflow: list[dict[str, Any]] = []
        for paper in group:
            year = paper.get("year") if isinstance(paper.get("year"), int) else 0
            if paper.get("recent") and counts[year] < yearly_cap:
                first_pass.append(paper)
                counts[year] += 1
            else:
                overflow.append(paper)
        chosen.extend((first_pass + overflow)[: max(0, limit - len(chosen))])
        if len(chosen) >= limit:
            break
    return chosen


def same_scholar_at_university(details: list[dict[str, Any]], chosen: dict[str, Any],
                               school: str, search_org: str) -> bool:
    # A name plus university does not identify a person. Paper records can
    # contain a same-name author at a different college, sometimes without an
    # AMiner person ID. Attribute work only to the selected exact person ID.
    chosen_id = str(chosen.get("id") or "")
    if not chosen_id:
        return False
    org_variants = [str(chosen.get("org") or ""), str(chosen.get("org_zh") or ""), school, search_org]
    org_variants += [item.replace("University", "Univ").replace("university", "univ")
                     for item in org_variants if item]
    return any(
        str(author.get("id") or "") == chosen_id
        and normalized_person_name(author.get("name")) == normalized_person_name(chosen.get("name"))
        and ((bool(chosen.get("org_id") and author.get("orgid"))
              and str(author["orgid"]) == str(chosen["org_id"]))
             or contains_any(str(author.get("org") or ""), org_variants))
        for detail in details for author in detail.get("authors") or [] if isinstance(author, dict)
    )


def deepdive_person(
    client: AMinerClient, name: str, school: str, deep: bool, paper_limit: int, warnings: list[str],
    focus_terms: list[str] | None = None,
    broad_context: bool = False,
) -> dict[str, Any]:
    """Resolve one scholar by name (+optional school) and return an evidence-bearing profile."""
    params = {"name": name, "size": 10}
    if school:
        params["org"] = school
    rows = unwrap(client.call("person_search", params))
    if not rows and school:
        rows = unwrap(client.call("person_search", {"name": name, "size": 10}))
    if not rows:
        return {"query_name": name, "query_school": school or None, "found": False,
                "note": "AMiner 未找到该学者；请确认姓名/学校或补充信息"}

    # person_search is fuzzy: 李戈 also returns 李戈辉. Never let a high citation
    # count or the shared university turn a different full name into the target.
    exact_rows = [row for row in rows if any(
        normalized_person_name(row.get(field)) == normalized_person_name(name)
        for field in ("name_zh", "name") if row.get(field)
    )]
    if exact_rows:
        rows = exact_rows
    else:
        warnings.append(f"'{name}' 仅检到近似姓名，未自动认定为该学者")
        return {"query_name": name, "query_school": school or None, "found": True,
                "needs_disambiguation": True,
                "note": "AMiner 仅返回近似姓名；请核对学者全名或提供主页",
                "candidates": [
                    {"person_id": str(r.get("id") or ""),
                     "name": r.get("name_zh") or r.get("name"),
                     "organization": r.get("org_zh") or r.get("org")}
                    for r in rows[:6]
                ]}

    chosen, contenders = _confident_person_pick(rows, school)
    if chosen is None:
        # Too ambiguous to deep-dive safely — return a disambiguation list to ask the user.
        warnings.append(f"'{name}' 对应多位不同学者，无法确定是哪一位，请补充学校/方向后再深挖")
        return {
            "query_name": name, "query_school": school or None, "found": True,
            "needs_disambiguation": True,
            "candidates": [
                {"person_id": str(r.get("id") or ""), "name": r.get("name_zh") or r.get("name"),
                 "organization": r.get("org_zh") or r.get("org"),
                 "n_citation": r.get("n_citation") if isinstance(r.get("n_citation"), int) else None,
                 "interests": [str(x) for x in (r.get("interests") or [])][:5],
                 "aminer_url": f"https://www.aminer.cn/profile/{r.get('id')}" if r.get("id") else None}
                for r in contenders
            ],
        }
    pid = str(chosen.get("id") or "")
    interests = [str(x) for x in (chosen.get("interests") or [])]
    profile: dict[str, Any] = {
        "query_name": name, "query_school": school or None, "found": True, "needs_disambiguation": False,
        "person_id": pid,
        "name": chosen.get("name_zh") or chosen.get("name"), "name_en": chosen.get("name"),
        "organization": chosen.get("org_zh") or chosen.get("org"), "organization_en": chosen.get("org"),
        "aminer_url": f"https://www.aminer.cn/profile/{pid}" if pid else None,
        "n_citation": chosen.get("n_citation") if isinstance(chosen.get("n_citation"), int) else None,
        "interests": interests,
    }
    if deep and pid:
        try:
            rows_d = unwrap(client.call("person_detail", {"id": pid}))
            if rows_d:
                d = rows_d[0]
                profile["position"] = str(d.get("position_zh") or d.get("position") or "").strip() or None
                profile["education"] = str(d.get("edu_zh") or d.get("edu") or "").strip() or None
                profile["honors_bio"] = str(d.get("bio_zh") or d.get("bio") or "").strip() or None
        except AMinerAPIError as exc:
            warnings.append(f"person_detail 失败（{name}）：{exc.message}")
    # Search the user's requested focus through the title+keyword query box.
    # A general by-name list is useful only when no specific focus was supplied.
    focus_terms = [term for term in (focus_terms or []) if term]
    search_focus_terms = focus_terms[:6]
    papers: list[dict[str, Any]] = []
    low_overlap = 0
    rows_by_term: list[list[dict[str, Any]]] = []
    search_rows: dict[str, dict[str, Any]] = {}
    search_org = extract_institution_name(str(chosen.get("org") or "")) or school
    for index, focus in enumerate(search_focus_terms or [""]):
        # The primary term gets both recent and citation-order samples. A
        # year-only top page misses influential recent foundations; the
        # citation page is still constrained to the same five-year window.
        for order in (("year", "n_citation") if focus and index == 0 else ("year",)):
            params = {"author": chosen.get("name"), "org": search_org,
                      "order": order, "page": 0, "size": paper_limit}
            if focus:
                params.update(query=focus, year_from=CURRENT_YEAR - 5, year_to=CURRENT_YEAR)
            try:
                found = unwrap(client.call("paper_search_pro", params))
            except AMinerAPIError as exc:
                warnings.append(f"论文检索失败（{name}，{focus or 'general'}，{order}）：{exc.message}")
                continue
            for row in found:
                paper_id = str(row.get("id") or "")
                if paper_id:
                    search_rows.setdefault(paper_id, row)
            rows_by_term.append(found)
    sampled_ids = bounded_paper_ids(rows_by_term, paper_limit)
    for r in (search_rows[pid] for pid in sampled_ids):
        title = str(r.get("title") or "").strip()
        if not title:
            continue
        if focus_terms:
            paper_id = str(r.get("id") or "")
            try:
                details = unwrap(client.call("paper_detail", {"id": paper_id})) if paper_id else []
            except AMinerAPIError as exc:
                warnings.append(f"论文作者核验失败（{name}，{paper_id}）：{exc.message}")
                details = []
            matching_author = same_scholar_at_university(details, chosen, school, search_org)
            if not matching_author:
                warnings.append(f"{name}: {paper_id} 未核实到该校同名作者，未列为该导师成果")
                continue
            # Abstracts routinely mention generic LLM code generation as context
            # even when the work itself is about another task (e.g. watermarking).
            # Require the requested focus in the title or author keywords.
            focus_evidence = " ".join([title] + [" ".join(
                str(k) for k in d.get("keywords") or []) for d in details])
            on_topic = focus_topic_relevant(focus_evidence, focus_terms)
        else:
            on_topic = paper_topic_relevant(title, interests)
        if not on_topic:
            low_overlap += 1
        year = r.get("year")
        papers.append({"year": year, "title": title,
                       "recent": isinstance(year, int) and year >= CURRENT_YEAR - 5,
                       "topic_match": on_topic,
                       "aminer_url": f"https://www.aminer.cn/pub/{r.get('id')}" if r.get("id") else None})
    # on-topic papers first, so the profile leads with clearly-relevant evidence
    papers.sort(key=lambda p: p["topic_match"], reverse=True)
    kept = [p for p in papers if p["topic_match"]]
    # paper_search_pro is a by-NAME author search, not person_id-scoped, so a common
    # English name pulls in same-name others' papers even after the person is picked.
    # If most retrieved papers are off-topic, the by-name list is unreliable: keep only
    # the on-topic ones and flag it, rather than presenting a polluted list as theirs.
    if papers and len(kept) < 0.5 * len(papers):
        profile["recent_papers_reliable"] = False
        if focus_terms:
            warnings.append(
                f"{name}: 检索结果中 {len(papers)-len(kept)}/{len(papers)} 篇缺少指定方向的关键术语，"
                "已仅保留方向匹配论文；其余可能是跨方向工作或同名混入，须逐篇核实")
        else:
            warnings.append(
                f"{name}: 按姓名检索到的近期论文疑似被同名严重污染（{len(papers)-len(kept)}/{len(papers)} 不相关），"
                "已仅保留与研究兴趣相符者；完整可靠列表请查 AMiner 主页")
        profile["recent_papers"] = select_representative_papers(kept, paper_limit)
    else:
        if low_overlap:
            subject = "指定方向" if focus_terms else "研究兴趣"
            warnings.append(f"{name}: {low_overlap} 篇近期论文与{subject}词重合低，可能是同名混入或跨子方向，已标记待核（topic_match=false）")
        profile["recent_papers"] = select_representative_papers(papers, paper_limit)
    recent_matches = sum(bool(p.get("topic_match") and p.get("recent"))
                         for p in profile["recent_papers"])
    if pid and recent_matches < min(5, paper_limit):
        linked = linked_recent_papers(client, pid, focus_terms, paper_limit, warnings)
        if linked:
            merged = {p.get("aminer_url"): p for p in profile["recent_papers"]}
            for paper in linked:
                merged.setdefault(paper.get("aminer_url"), paper)
            profile["recent_papers"] = select_representative_papers(
                list(merged.values()), paper_limit)
            warnings.append(
                f"{name}: 当前机构的按名论文检索只有 {recent_matches} 篇可用的近期方向成果；"
                "已合并人物 ID 论文关系补召回，关联关系和年份已核对，正式发表与具体作者单位仍需原文确认"
            )
    if broad_context and focus_terms:
        # Broad portraits need a second, unfiltered view. A focus query capped at
        # ten papers can omit the scholar's model families and landmark work.
        # Keep this separate from direct focus evidence and verify each author.
        try:
            broad_rows = unwrap(client.call("paper_search_pro", {
                "author": chosen.get("name"), "org": search_org,
                "order": "n_citation", "page": 0, "size": 100,
                "year_from": CURRENT_YEAR - 5, "year_to": CURRENT_YEAR,
            }))
        except AMinerAPIError as exc:
            warnings.append(f"{name}: 广义代表作补检失败：{exc.message}")
            broad_rows = []
        seen_titles = {re.sub(r"[\W_]+", "", normalized_text(p.get("title")))
                       for p in profile["recent_papers"]}
        context_candidates = []
        for row in broad_rows[:20]:
            paper_id = str(row.get("id") or "")
            title = str(row.get("title") or "").strip()
            year = row.get("year")
            title_key = re.sub(r"[\W_]+", "", normalized_text(title))
            if not paper_id or not title or title_key in seen_titles or not isinstance(year, int):
                continue
            if not CURRENT_YEAR - 5 <= year <= CURRENT_YEAR:
                continue
            try:
                details = unwrap(client.call("paper_detail", {"id": paper_id}))
            except AMinerAPIError as exc:
                warnings.append(f"{name}: 代表作作者核验失败（{paper_id}）：{exc.message}")
                continue
            matching_author = same_scholar_at_university(details, chosen, school, search_org)
            if not matching_author:
                continue
            seen_titles.add(title_key)
            keywords = " ".join(str(k) for detail in details for k in detail.get("keywords") or [])
            context_candidates.append({"year": year, "title": title,
                            "topic_match": broad_context_topic_relevant(title + " " + keywords, focus_terms),
                            "source": "citation_ranked_author_verified",
                            "n_citation_bucket": row.get("n_citation_bucket"),
                            "aminer_url": f"https://www.aminer.cn/pub/{paper_id}"})
        topical = [paper for paper in context_candidates if paper["topic_match"]]
        adjacent = [paper for paper in context_candidates if not paper["topic_match"]]
        context = topical[:4] + adjacent[:2]
        for paper in context_candidates:
            if len(context) >= 6:
                break
            if paper not in context:
                context.append(paper)
        profile["broad_context_papers"] = context
        # Citation rank favors established work. A separate newest-year pass
        # exposes emerging branches that have not accrued citations yet.
        try:
            recent_rows = unwrap(client.call("paper_search_pro", {
                "author": chosen.get("name"), "org": search_org,
                "order": "year", "page": 0, "size": 100,
                "year_from": CURRENT_YEAR - 5, "year_to": CURRENT_YEAR,
            }))
        except AMinerAPIError as exc:
            warnings.append(f"{name}: 最新工作补检失败：{exc.message}")
            recent_rows = []
        excluded = {re.sub(r"[\W_]+", "", normalized_text(p.get("title")))
                    for p in profile["recent_papers"] + context}
        recent_context = []
        for row in recent_rows[:20]:
            paper_id = str(row.get("id") or "")
            title = str(row.get("title") or "").strip()
            year = row.get("year")
            title_key = re.sub(r"[\W_]+", "", normalized_text(title))
            if not paper_id or not title or title_key in excluded or not isinstance(year, int):
                continue
            if not CURRENT_YEAR - 5 <= year <= CURRENT_YEAR:
                continue
            try:
                details = unwrap(client.call("paper_detail", {"id": paper_id}))
            except AMinerAPIError as exc:
                warnings.append(f"{name}: 最新工作作者核验失败（{paper_id}）：{exc.message}")
                continue
            if not same_scholar_at_university(details, chosen, school, search_org):
                continue
            excluded.add(title_key)
            keywords = " ".join(str(k) for detail in details for k in detail.get("keywords") or [])
            recent_context.append({
                "year": year, "title": title,
                "topic_match": broad_context_topic_relevant(title + " " + keywords, focus_terms),
                "source": "recent_author_verified",
                "aminer_url": f"https://www.aminer.cn/pub/{paper_id}",
            })
            if len(recent_context) >= 10:
                break
        profile["recent_context_papers"] = recent_context
        # Name-filtered title search misses papers whose titles use a model or
        # method name instead of the broad field phrase. The person-ID search
        # is an independent recent recall channel; its cards remain leads.
        try:
            id_recent_rows = unwrap(client.call("paper_qa_search", {
                "query": focus_terms[0], "author_id": [pid], "use_topic": True,
                "year": [CURRENT_YEAR - 1, CURRENT_YEAR], "size": 50,
                "force_year_sort": True,
            }))
        except AMinerAPIError as exc:
            warnings.append(f"{name}: 人物 ID 最新方向补检失败：{exc.message}")
            id_recent_rows = []
        id_recent_cards = []
        seen_id_papers = set()
        for row in id_recent_rows:
            paper_id = str(row.get("id") or "")
            title = str(row.get("title") or "").strip()
            year = row.get("year")
            if (not paper_id or not title or paper_id in seen_id_papers or
                    not isinstance(year, int) or year not in (CURRENT_YEAR - 1, CURRENT_YEAR)):
                continue
            seen_id_papers.add(paper_id)
            id_recent_cards.append({
                "year": year, "title": title,
                "venue_name": row.get("venue_name") or None,
                "aminer_url": f"https://www.aminer.cn/pub/{paper_id}",
            })
        profile["author_id_recent_search"] = {
            "window": [CURRENT_YEAR - 1, CURRENT_YEAR],
            "source": "paper_qa_search with exact person_id",
            "cards": id_recent_cards[:50],
            "note": "Recent person-ID search leads. Check selected paper_detail records before claiming author affiliation, author order, venue or task fit.",
        }
        # School-constrained name search can miss short university affiliations.
        # An author-only pass recovers a few branch papers, then verifies their
        # exact person ID and university before showing them as research leads.
        seeds = profile["recent_papers"] + profile.get("broad_context_papers", []) + profile.get("recent_context_papers", [])
        recall_terms = broad_person_field_recall_terms(
            focus_terms, [str(p.get("title") or "") for p in seeds])
        field_rows: list[tuple[str, dict[str, Any]]] = []
        for recall_term in recall_terms:
            try:
                rows = unwrap(client.call("paper_search_pro", {
                    "author": chosen.get("name"), "query": recall_term,
                    "order": "year", "page": 0, "size": 12,
                    "year_from": CURRENT_YEAR - 1, "year_to": CURRENT_YEAR,
                }))
            except AMinerAPIError as exc:
                warnings.append(f"{name}: {recall_term} 人物宽词补检失败：{exc.message}")
                continue
            field_rows.extend((recall_term, row) for row in rows[:12])
        field_ids = list(dict.fromkeys(str(row.get("id") or "") for _, row in field_rows if row.get("id")))
        field_details = fetch_paper_details(client, field_ids, 24, warnings)
        already_shown = {re.sub(r"[\W_]+", "", normalized_text(p.get("title")))
                         for p in seeds}
        recalled: list[dict[str, Any]] = []
        per_term: Counter[str] = Counter()
        for recall_term, row in field_rows:
            paper_id = str(row.get("id") or "")
            title = str(row.get("title") or "").strip()
            year = row.get("year")
            title_key = re.sub(r"[\W_]+", "", normalized_text(title))
            if (not paper_id or not title or title_key in already_shown or
                    not isinstance(year, int) or year not in (CURRENT_YEAR - 1, CURRENT_YEAR) or
                    per_term[recall_term] >= 8):
                continue
            details = [field_details[paper_id]] if paper_id in field_details else []
            if not same_scholar_at_university(details, chosen, school, search_org):
                continue
            already_shown.add(title_key)
            per_term[recall_term] += 1
            recalled.append({"year": year, "title": title,
                             "retrieval_term": recall_term,
                             "source": "author_only_field_recall; person_and_university_verified",
                             "aminer_url": f"https://www.aminer.cn/pub/{paper_id}"})
        profile["field_recall_papers"] = recalled
    profile["recent_papers_sampling"] = {
        "selected": len(profile["recent_papers"]), "limit": paper_limit,
        "exhaustive": False,
        "note": "所列篇数是有限检索样本，不是该学者的方向论文总数或可比较的产出率",
    }
    profile["needs_verification"] = ["当前招生名额与是否在招", "是否仍在该机构任职"]
    return profile


def application_timing(as_of_date: date, profile: dict[str, Any] | None) -> dict[str, Any] | None:
    """Give cycle-specific cautions without inventing program deadlines."""
    if not profile:
        return None
    degree = str(profile.get("target_degree") or "").lower()
    if degree not in {"master", "硕士", "phd", "doctor", "博士"}:
        return None
    try:
        cycle = int(profile.get("target_cycle"))
    except (TypeError, ValueError):
        return None
    if not as_of_date.year <= cycle <= as_of_date.year + 3:
        return None
    if degree in {"phd", "doctor", "博士"}:
        return {"target_cycle": cycle, "as_of_date": as_of_date.isoformat(),
                "degree": "doctor", "official_deadline_required": True,
                "national_exam_route_only": False,
                "notes": [
                    "博士申请考核的报名和联系导师时间因学校、院系与批次而异；必须查目标院系当年的官方招生通知。",
                    f"只建议在 {as_of_date.isoformat()} 当天仍可执行的下一步；过去的套磁、实习或报名月份不得写成未来计划。",
                ]}
    exam_year = cycle - 1
    camp_passed = as_of_date > date(exam_year, 8, 31)
    registration_passed = as_of_date > date(exam_year, 10, 31)
    exam_near = as_of_date.year == exam_year and as_of_date.month in {11, 12}
    notes = [
        f"{cycle} 入学的夏令营/预推免一般在 {exam_year} 年夏季；"
        + ("截至当前日期通常已过，不可当作未来步骤。" if camp_passed else "具体安排以学校公告为准。"),
        f"若走全国统考，报名通常在 {exam_year} 年秋季、初试通常在 {exam_year} 年 12 月；实际日期以研招网和学校公告为准。",
    ]
    if registration_passed:
        notes.append("通常报名窗口已过，立即核实是否已完成报名及网上确认；不要建议未来再报名本轮考试。")
    if exam_near:
        notes.append("本轮初试已临近，备考余量不足两个月；不得写成还有约一年准备期。")
    return {"target_cycle": cycle, "as_of_date": as_of_date.isoformat(),
            "national_exam_route_only": True,
            "summer_camp_window_passed": camp_passed,
            "registration_window_likely_passed": registration_passed,
            "entrance_exam_near": exam_near,
            "notes": notes}


def main() -> None:
    args = build_parser().parse_args()
    if args.mode == "person":
        names = parse_csv(args.names)
        if not names:
            raise SystemExit("--names is required for person mode (comma-separated for comparison)")
        if args.paper_limit < 1:
            raise SystemExit("--paper-limit must be positive")
        focus_terms = direction_aliases(args.direction, aliases=parse_csv(args.aliases))
        estimate = estimate_person_cost(len(names), args.paper_limit, args.deep, len(focus_terms),
                                        args.broad_context)
        if estimate["worst_case_cny"] >= args.max_cost and not args.yes:
            raise SystemExit(
                f"Worst-case API cost estimate ¥{estimate['worst_case_cny']:.2f} reaches --max-cost ¥{args.max_cost:.2f}; "
                "reduce limits or rerun with --yes after explicit confirmation"
            )
        try:
            client = AMinerClient(os.getenv("AMINER_API_KEY", ""))
        except ValueError as exc:
            raise SystemExit(f"{exc}; configure it with tools/setup-aminer-token before calling AMiner") from exc
        warnings: list[str] = []
        profiles = [deepdive_person(
            client, n, args.school, args.deep, args.paper_limit, warnings,
            focus_terms=focus_terms, broad_context=args.broad_context,
        ) for n in names]
        result = {
            "workflow": "person", "count": len(profiles), "is_comparison": len(profiles) > 1,
            "query": {"names": names, "school": args.school or None, "deep": args.deep,
                      "focus": args.direction or None, "aliases": parse_csv(args.aliases),
                      "broad_context": args.broad_context},
            "profiles": profiles, "warnings": list(dict.fromkeys(warnings)), "errors": [],
            "cost_estimate": estimate, "cost": client.cost.summary(),
            "method_notes": [
                "人名深挖：person_search 消歧（免费）+ 可选 person_detail（¥1）+ paper_search_pro 近期论文（¥0.01）。",
                "指定方向时用 query 检索标题及关键词，并核对论文作者机构；近期方向论文少于五篇时按人物 ID 的论文关系补召回。",
                "招生名额与在职状态 AMiner 无数据，须用户到官方页面核实。",
            ],
        }
        write_result(result, args.output)
        return
    if not args.direction:
        raise SystemExit("--direction is required for this mode")
    if args.mode == "named" and not args.school:
        raise SystemExit("--school is required for named mode")
    if args.mode == "profile" and not args.profile:
        raise SystemExit("--profile is required for profile mode")
    if min(args.paper_limit, args.candidate_limit, args.max_schools, args.max_author_lookups) < 1 or args.verify_roles < 0:
        raise SystemExit("limits must be positive and --verify-roles must be non-negative")

    profile = json.loads(args.profile.read_text(encoding="utf-8")) if args.profile else None
    tiers = read_tiers(args.tiers_file)
    aliases = parse_csv(args.aliases)
    schools = parse_csv(args.schools)
    tier_name = args.tier
    if args.school:
        schools = [args.school]
    elif not schools and tier_name:
        if tier_name not in tiers["tiers"]:
            raise SystemExit(f"Unknown tier: {tier_name}")
        schools = list(tiers["tiers"][tier_name]["schools"])
    elif not schools and profile:
        schools = list(profile.get("target_schools") or [])
        tier_name = tier_name or next(iter(profile.get("school_tier_preferences") or []), "")
        if not schools and tier_name in tiers["tiers"]:
            schools = list(tiers["tiers"][tier_name]["schools"])
    explicit_schools = bool(args.school or args.schools or args.tier or
                            (profile and profile.get("target_schools")))
    auto_expand_profile = should_auto_expand_profile(
        args.mode, explicit_schools, args.no_auto_expand_profile, args.auto_expand_profile)
    if args.mode != "discover" and not schools and not auto_expand_profile:
        raise SystemExit("provide --school, --schools, --tier, or target schools in the profile")

    requested_school_count = len(schools)
    schools = schools[: args.max_schools]
    estimated_school_count = args.max_schools if auto_expand_profile else len(schools)
    # --hired-since needs person_detail evidence for the shortlist, so it implies
    # role verification even when --verify-roles was not raised explicitly.
    effective_verify_roles = max(args.verify_roles, args.candidate_limit if (args.hired_since or args.with_portrait) else 0)
    estimate = estimate_cost(
        args.mode, estimated_school_count, min(6 if args.mode == "discover" else 3, 1 + len(aliases)),
        args.paper_limit, effective_verify_roles,
        profile_discovery=auto_expand_profile,
        discovery_alias_count=min(6, 1 + len(aliases)),
    )
    if estimate["worst_case_cny"] >= args.max_cost and not args.yes:
        raise SystemExit(
            f"Worst-case API cost estimate ¥{estimate['worst_case_cny']:.2f} reaches --max-cost ¥{args.max_cost:.2f}; "
            "reduce limits or rerun with --yes after explicit confirmation"
        )

    warnings: list[str] = []
    errors: list[dict[str, Any]] = []
    if requested_school_count > len(schools):
        warnings.append(f"school cap omitted {requested_school_count - len(schools)} schools; provide a region or explicit list")
    try:
        client = AMinerClient(os.getenv("AMINER_API_KEY", ""))
    except ValueError as exc:
        raise SystemExit(f"{exc}; configure it with tools/setup-aminer-token before calling AMiner") from exc
    try:
        if args.mode == "discover":
            institutions = discover_institutions(client, args.direction, aliases, args.paper_limit, warnings, schools)
            result = {
                "workflow": "discover", "query": {"direction": args.direction, "aliases": aliases,
                                                  "schools": schools, "window": [CURRENT_YEAR - 5, CURRENT_YEAR]},
                # The caller paid to compare every explicit school. A mentor
                # candidate limit must not silently hide the last schools.
                "institutions": institutions if schools else institutions[: args.candidate_limit],
                "mentor_followup_plan": mentor_followup_plan(schools, institutions) if schools else None,
                "official_seed_leads": official_seed_leads(schools, args.direction, aliases) if schools else {},
                "warnings": warnings, "errors": errors,
                "cost_estimate": estimate, "cost": client.cost.summary(),
                "method_notes": ["Explicit schools use the same terms, year window and per-school detail cap. matched_paper_count uses matched_paper_year_range; recent_direction_paper_count uses the narrower recent_direction_paper_year_range. Both count task-filtered, affiliation-verified distinct titles in the inspected sample. Do not reverse their date labels or describe either as a user-requested window unless its dates match. When compound_focus is set, one-sided papers are in adjacent_topic_paper_count instead. representative_papers and evidence_papers are compact citation-ranked and recent excerpts (at most 2+1 when comparing 9 or more schools, otherwise 3+2). Counts are not national rankings."]
                if schools else ["Global search is a seed sample, not a national ranking; compare selected schools with --schools."],
            }
            write_result(result, args.output)
            return

        auto_added_schools: list[str] = []
        if auto_expand_profile and profile is not None:
            discovered = discover_institutions(client, args.direction, aliases, args.paper_limit, warnings)
            inspected = discovered[: max(20, args.max_schools * 5)]
            if len(discovered) > len(inspected):
                warnings.append(
                    f"profile expansion inspected {len(inspected)} of {len(discovered)} discovered organizations"
                )
            for row in inspected:
                main_body = extract_institution_name(str(row.get("organization") or ""))
                row["organization_main"] = main_body
                resolved = resolve_organization(client, main_body) if main_body else None
                if resolved:
                    row["organization_aliases"] = [resolved.canonical_name, *resolved.aliases]
            auto_added_schools = choose_profile_expansion_schools(
                profile, schools, inspected, tiers, args.max_schools
            )
            schools.extend(auto_added_schools)
            if auto_added_schools:
                warnings.append(
                    "profile mode automatically added lower-tier, direction-evidenced schools: "
                    + ", ".join(auto_added_schools)
                )
            elif len(schools) < args.max_schools:
                warnings.append(
                    "profile auto-expansion found no additional lower-tier university in the inspected AMiner sample"
                )
            if not schools:
                raise SystemExit(
                    "profile auto-expansion found no university; provide target_schools, --schools, or a broader direction alias"
                )

        all_candidates: dict[str, Candidate] = {}
        per_school: dict[str, int] = {}
        organization_cache: dict[str, ResolvedOrganization | None] = {}
        for school in schools:
            # One school failing (network, gateway, auth on a single call chain)
            # must not discard results already collected for other schools.
            try:
                found = recommend_for_school(
                    client, school, args.department, args.direction, aliases, args.paper_limit, profile,
                    args.max_author_lookups, args.allow_name_fallback, effective_verify_roles, warnings, organization_cache,
                    args.mode == "collaboration", args.allow_cross_discipline, args.hired_since, args.roster_pages,
                    args.with_portrait, args.candidate_limit, args.mode in {"profile", "collaboration"},
                )
            except AMinerAPIError as exc:
                per_school[school] = 0
                errors.append({"school": school, **exc.to_dict()})
                warnings.append(f"school {school} failed ({exc.message}); results below exclude this school")
                continue
            per_school[school] = len(found)
            for person_id, candidate in found.items():
                if person_id not in all_candidates:
                    all_candidates[person_id] = candidate
                else:
                    existing = all_candidates[person_id]
                    existing.source_schools.update(candidate.source_schools)
                    existing.papers.update(candidate.papers)
                    existing.collaboration_orgs.update(candidate.collaboration_orgs)
                    existing.collaboration_types.update(candidate.collaboration_types)
                    for org_id, paper_ids in candidate.collaboration_papers.items():
                        existing.collaboration_papers.setdefault(org_id, set()).update(paper_ids)

        all_candidates = mark_duplicate_names(all_candidates)
        if profile:
            assign_bands(all_candidates, profile, tiers, school_level_map(schools, organization_cache, tiers))

        rank_key = build_rank_key(args.mode, args.collaboration_type, args.rank_by)
        all_ranked = sorted(all_candidates.values(), key=rank_key, reverse=True)
        hire_screening = None
        pool = all_ranked
        if args.hired_since:
            pool, hire_screening = screen_recent_hires(all_ranked, args.hired_since)
        ranked = (
            select_profile_portfolio(
                pool, args.candidate_limit,
                schools if (args.schools or (profile and profile.get("target_schools"))) else None)
            if args.mode == "profile" else pool[: args.candidate_limit]
        )
        backfill_chinese_names(client, ranked, warnings)
        portfolio_band_counts = dict(Counter(
            candidate.recommendation_band for candidate in ranked if candidate.recommendation_band
        ))
        method_notes = [
            "Only authors whose paper affiliation matches the resolved organization ID are considered by default.",
            "Every candidate has at least one non-empty, dated, direction-matched paper evidence item.",
            "Advisor role and current recruitment require official verification unless a role is explicitly returned.",
            "Reach/match/safer labels are heuristic bands, not admission probabilities.",
        ]
        as_of_date = datetime.now(CHINA_TZ).date()
        if args.mode == "profile":
            method_notes.append(
                "The profile result is a paper-based lead pool, not a verified advisor list. "
                "school_research_leads retains up to five unverified search leads per named school even when "
                "the overall candidate limit hides them; do not call these people current faculty. "
                "Use their affiliation units to locate official individual pages and verify task fit. "
                "A person with official identity and specific work but no degree-role proof can be shown as a "
                "pending research match; run shortlist_gate before calling anyone degree-role verified. "
                "Check each named school before filling extra slots from a covered school."
            )
            method_notes.append(
                f"Application advice must be relative to {as_of_date.isoformat()}: "
                "do not present a deadline or summer camp before this date as a future step. "
                "Check each program's official admissions schedule before giving dates."
            )
            if as_of_date.month >= 9 and profile and str(profile.get("target_cycle")) == str(as_of_date.year + 1):
                method_notes.append(
                    f"The typical June-August {as_of_date.year} summer-camp window has passed; "
                    "describe it only as past, not as an application action still available."
                )
        if args.rank_by == "rising":
            method_notes.append(
                "Rising ranking is a heuristic (recent direction evidence plus a smaller citation base), "
                "not an official rising-star index; verify seniority on the official profile page."
            )
        result = {
            "workflow": args.mode,
            "as_of_date": as_of_date.isoformat(),
            "application_timing": application_timing(as_of_date, profile) if args.mode == "profile" else None,
            "query": {"direction": args.direction, "aliases": aliases, "schools": schools,
                      "department": args.department or None, "tier": tier_name or None,
                      "rank_by": args.rank_by,
                      "collaboration_type": args.collaboration_type if args.mode == "collaboration" else None},
            "resolved_organizations": {
                school: ({"id": org.org_id, "canonical_name": org.canonical_name} if org else None)
                for school, org in organization_cache.items()
            },
            "school_tier_facts": school_tier_facts(schools, tiers) if args.mode == "profile" else {},
            "candidate_counts_by_school": per_school,
            "school_research_leads": school_research_leads(all_ranked, schools)
            if args.mode in {"profile", "tier"} else {},
            "task_specific_alternate_leads": task_specific_alternate_leads(
                all_ranked, ranked, schools, direction_aliases(args.direction, aliases))
            if args.mode == "profile" else {},
            "official_directory_hints": official_directory_hints(schools, args.direction),
            "official_profile_hints": official_profile_hints(all_ranked),
            "official_seed_leads": official_seed_leads(schools, args.direction, aliases),
            "verification_next_action": (
                "When official_directory_hints are present, scan those checked entrypoints first in one bounded "
                "directory pass instead of guessing hostnames. They are navigation seeds, not current-role proof. "
                "For other schools, inspect the returned institution and department for an official faculty directory; "
                "send the first person with an official requested-degree role and an attributed dated paper "
                "to shortlist_gate immediately. Do not substitute repeated broad paper searches or a person "
                "deep-dive for that gate. Rotate to each remaining named school before adding more from one school."
                if args.mode == "profile" else None
            ),
            "school_selection": {"requested_count": requested_school_count, "searched_count": len(schools),
                                 "truncated": requested_school_count > args.max_schools,
                                 "auto_expanded": bool(auto_added_schools),
                                 "auto_added_schools": auto_added_schools},
            "candidates": [candidate.to_dict() for candidate in ranked],
            "alternate_collaboration_leads": collaboration_alternate_leads(all_ranked, ranked)
            if args.mode == "collaboration" else [],
            "hire_screening": hire_screening,
            "portfolio_band_counts": portfolio_band_counts if args.mode == "profile" else None,
            "warnings": list(dict.fromkeys(warnings)), "errors": errors, "cost_estimate": estimate,
            "cost": client.cost.summary(),
            "fallback": {
                "suggestions": ["try English/official organization names", "add direction aliases", "use --mode discover"]
                if not ranked else []
            },
            "method_notes": method_notes,
        }
        write_result(result, args.output)
    except AMinerAPIError as exc:
        result = {
            "workflow": args.mode, "query": {"direction": args.direction, "schools": schools},
            "candidates": [], "warnings": warnings, "errors": errors + [exc.to_dict()],
            "cost_estimate": estimate, "cost": client.cost.summary(),
        }
        write_result(result, args.output)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
