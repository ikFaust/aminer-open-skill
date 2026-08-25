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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from aminer_client import API_SPEC, AMinerAPIError, AMinerClient, unwrap


SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TIERS = SKILL_ROOT / "references" / "school-tiers.json"
CURRENT_YEAR = datetime.now(timezone.utc).year
FACULTY_TERMS = (
    "professor", "associate professor", "assistant professor", "researcher", "principal investigator",
    "教授", "副教授", "研究员", "副研究员", "博导", "博士生导师", "硕导", "硕士生导师",
)
NON_FACULTY_TERMS = ("student", "phd candidate", "postdoc", "postdoctoral", "学生", "博士生", "硕士生", "博士后")
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


def normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


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
    scores: dict[str, float | None] = field(default_factory=dict)
    confidence: str = "low"
    recommendation_band: str | None = None
    source_schools: set[str] = field(default_factory=set)
    alternate_profile_ids: list[str] = field(default_factory=list)
    identity_ambiguous: bool = False
    identity_resolution: str = "org_constrained_name"
    role: str | None = None
    role_unverified: bool = True

    @property
    def display_name(self) -> str:
        return self.name_zh or self.name

    def to_dict(self) -> dict[str, Any]:
        verification = ["current department affiliation", "current recruitment status", "admissions requirements and deadline"]
        if self.role_unverified:
            verification.insert(0, "whether this scholar is eligible to supervise the requested degree")
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
            "interests": self.interests,
            "n_citation": self.n_citation,
            "evidence_papers": list(self.papers.values()),
            "collaboration_organizations": [
                {"id": key, "name": value, "type": self.collaboration_types.get(key, "unknown")}
                for key, value in self.collaboration_orgs.items()
            ],
            "scores": self.scores,
            "confidence": self.confidence,
            "recommendation_band": self.recommendation_band,
            "needs_verification": verification,
        }


def resolve_organization(client: AMinerClient, school: str) -> ResolvedOrganization | None:
    rows = unwrap(client.call("org_search", {"orgs": [school]}))
    if not rows:
        return None
    query = normalized_text(school)
    rows.sort(
        key=lambda row: (
            normalized_text(row.get("org_name")) == query,
            any(normalized_text(alias) == query for alias in row.get("aliases") or []),
        ),
        reverse=True,
    )
    row = rows[0]
    org_id = str(row.get("org_id") or "")
    name = str(row.get("org_name") or school)
    if not org_id:
        return None
    return ResolvedOrganization(school, org_id, name, [str(x) for x in row.get("aliases") or []])


def paper_search(client: AMinerClient, direction: str, org_name: str | None, size: int) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"keyword": direction, "order": "year", "page": 0, "size": size}
    if org_name:
        params["org"] = org_name
    return unwrap(client.call("paper_search_pro", params))


def batch_paper_info(client: AMinerClient, paper_ids: list[str], warnings: list[str]) -> list[dict[str, Any]]:
    if not paper_ids:
        return []
    if len(paper_ids) > 100:
        warnings.append(f"paper_info supports 100 IDs; {len(paper_ids) - 100} papers were omitted")
    return unwrap(client.call("paper_info", {"ids": paper_ids[:100]}))


def clean_paper(row: dict[str, Any], detail: dict[str, Any] | None, terms: list[str]) -> dict[str, Any] | None:
    title = str((detail or {}).get("title") or row.get("title") or "").strip()
    year = (detail or {}).get("year") or row.get("year")
    if not title or not isinstance(year, int):
        return None
    text = " ".join(
        [title, str(row.get("abstract_slice") or ""), str((detail or {}).get("abstract") or "")]
        + [str(x) for x in ((detail or {}).get("keywords") or [])]
    )
    matched = [term for term in terms if contains_any(text, [term])]
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
    }


def fetch_paper_details(
    client: AMinerClient, paper_ids: list[str], max_details: int, warnings: list[str]
) -> dict[str, dict[str, Any]]:
    selected = paper_ids[:max_details]
    if len(paper_ids) > len(selected):
        warnings.append(f"paper detail cap omitted {len(paper_ids) - len(selected)} papers")
    details: dict[str, dict[str, Any]] = {}
    for paper_id in selected:
        rows = unwrap(client.call("paper_detail", {"id": paper_id}))
        if rows:
            details[paper_id] = rows[0]
    return details


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
    author_papers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    author_affiliations: dict[str, set[str]] = defaultdict(set)
    author_org_ids: dict[str, set[str]] = defaultdict(set)
    skipped_without_org = 0

    for paper_id, row in paper_by_id.items():
        detail = details.get(paper_id)
        evidence = clean_paper(row, detail, direction_terms)
        if not evidence or not detail:
            continue
        for author in detail.get("authors") or []:
            if not isinstance(author, dict):
                continue
            name = str(author.get("name") or "").strip()
            org_id = str(author.get("orgid") or "")
            org_name = str(author.get("org") or "").strip()
            if not name:
                continue
            exact_org = bool(org_id and org_id == organization.org_id)
            fallback_org = bool(not org_id and contains_any(org_name, [organization.canonical_name, organization.query]))
            if not exact_org and not (allow_name_fallback and fallback_org):
                skipped_without_org += 1
                continue
            key = normalized_text(name)
            author_papers[key].append(evidence)
            if org_name:
                author_affiliations[key].add(org_name)
            if org_id:
                author_org_ids[key].add(org_id)

    ordered_authors = sorted(
        author_papers,
        key=lambda key: (len({p["id"] for p in author_papers[key]}), max(p["year"] for p in author_papers[key])),
        reverse=True,
    )
    if len(ordered_authors) > max_author_lookups:
        warnings.append(f"author lookup cap omitted {len(ordered_authors) - max_author_lookups} organization-matched authors")
    if skipped_without_org:
        warnings.append(f"skipped {skipped_without_org} paper authors whose affiliation did not match the target organization")

    by_id: dict[str, Candidate] = {}
    fallback_count = 0
    for author_key in ordered_authors[:max_author_lookups]:
        display_name = next(
            str(author.get("name"))
            for detail in details.values()
            for author in detail.get("authors") or []
            if isinstance(author, dict) and normalized_text(author.get("name")) == author_key
        )
        people = unwrap(
            client.call("person_search", {"name": display_name, "org_id": [organization.org_id], "size": 10})
        )
        org_people = [row for row in people if str(row.get("org_id") or "") == organization.org_id]
        exact_people = [
            row for row in org_people
            if author_key in (normalized_text(row.get("name")), normalized_text(row.get("name_zh")))
        ]
        resolution = "org_constrained_exact_name"
        if not exact_people and allow_name_fallback:
            exact_people = [
                row for row in people
                if contains_any(f"{row.get('org')} {row.get('org_zh')}", [organization.canonical_name, organization.query])
            ]
            fallback_count += len(exact_people)
            resolution = "name_fallback"
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
    academic_terms = ("university", "college", "institute", "academy", "school", "大学", "学院", "研究院", "科学院", "实验室")
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


def enrich_collaboration(
    client: AMinerClient, candidates: dict[str, Candidate], details: dict[str, dict[str, Any]]
) -> None:
    candidate_names: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates.values():
        for name in (candidate.name, candidate.name_zh):
            if name:
                candidate_names[normalized_text(name)].append(candidate)
    for detail in details.values():
        authors = [row for row in detail.get("authors") or [] if isinstance(row, dict)]
        involved: dict[str, Candidate] = {}
        for author in authors:
            for candidate in candidate_names.get(normalized_text(author.get("name")), []):
                if detail.get("id") in candidate.papers:
                    involved[candidate.person_id] = candidate
        for candidate in involved.values():
            for author in authors:
                org_name = str(author.get("org") or "").strip()
                # Authors without an orgid get a "name:" pseudo-key so they are never
                # sent to org_detail as if the display name were an organization ID.
                org_id = str(author.get("orgid") or "") or f"name:{normalized_text(org_name)}"
                if not org_name or (candidate.org_id and org_id == candidate.org_id):
                    continue
                candidate.collaboration_orgs[org_id] = org_name

    org_ids = list(dict.fromkeys(
        org_id for candidate in candidates.values() for org_id in candidate.collaboration_orgs
        if org_id and not org_id.startswith("name:")
    ))
    verified_types: dict[str, str] = {}
    if org_ids:
        for row in unwrap(client.call("org_detail", {"ids": org_ids[:100]})):
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
        direction_score = min(100.0, 50.0 + 15.0 * len(matched_papers) + 5.0 * len({t for p in matched_papers for t in p.get("matched_terms", [])}))
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
        affiliation = " ".join([candidate.org, candidate.org_zh, *candidate.author_affiliations])
        if contains_any(affiliation, CROSS_DISCIPLINE_AFFILIATION_TERMS):
            removed.append(f"{candidate.display_name} ({candidate.org_zh or candidate.org})")
        else:
            kept[person_id] = candidate
    if removed:
        preview = "; ".join(removed[:5])
        suffix = f"; and {len(removed) - 5} more" if len(removed) > 5 else ""
        warnings.append(f"filtered {len(removed)} obvious cross-discipline affiliations: {preview}{suffix}")
    return kept


def verify_candidate_roles(client: AMinerClient, candidates: dict[str, Candidate], limit: int, warnings: list[str]) -> None:
    ranked = sorted(candidates.values(), key=lambda row: (len(row.papers), row.n_citation or 0), reverse=True)[:limit]
    for candidate in ranked:
        if candidate.person_id.startswith("unresolved:"):
            continue
        rows = unwrap(client.call("person_detail", {"id": candidate.person_id}))
        if not rows:
            continue
        row = rows[0]
        role = str(row.get("position_zh") or row.get("position") or "").strip()
        candidate.role = role or None
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
    value = normalized_text(name)
    for tier_name, level in (("985", 3), ("华五", 3), ("211", 2), ("双一流", 1)):
        if any(normalized_text(school) == value for school in tiers["tiers"].get(tier_name, {}).get("schools", [])):
            return level
    if any(term in value for term in ("二本", "普通本科", "省属")):
        return 0
    # Do not silently promote an unlisted institution above an ordinary undergraduate
    # institution. Strong non-211 institutions can be supplied in an explicit tier later.
    return 0


def school_difficulty(school: str, tiers: dict[str, Any]) -> int:
    return institution_level(school, tiers)


def assign_bands(candidates: dict[str, Candidate], profile: dict[str, Any], tiers: dict[str, Any]) -> None:
    readiness = applicant_readiness(profile)
    undergraduate_level = institution_level(str(profile.get("undergraduate_institution") or ""), tiers)
    for candidate in candidates.values():
        target_level = max([school_difficulty(school, tiers) for school in candidate.source_schools] or [2])
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
        groups[(normalized_text(candidate.display_name), tuple(sorted(candidate.source_schools)))].append(candidate)
    for rows in groups.values():
        if len(rows) <= 1:
            continue
        ids = [row.person_id for row in rows]
        for row in rows:
            row.identity_ambiguous = True
            row.alternate_profile_ids = [person_id for person_id in ids if person_id != row.person_id]
    return candidates


def select_profile_portfolio(candidates: list[Candidate], limit: int) -> list[Candidate]:
    """Keep cross-tier bands visible instead of letting reach candidates fill every row."""
    labels = ("冲刺（启发式）", "匹配（启发式）", "相对稳妥（启发式）")
    ratios = {labels[0]: 0.4, labels[1]: 0.4, labels[2]: 0.2}
    groups = {label: [row for row in candidates if row.recommendation_band == label] for label in labels}
    selected: list[Candidate] = []
    selected_ids: set[str] = set()
    for label in labels:
        quota = max(1, round(limit * ratios[label]))
        for row in groups[label][:quota]:
            selected.append(row)
            selected_ids.add(row.person_id)
    for row in candidates:
        if len(selected) >= limit:
            break
        if row.person_id not in selected_ids:
            selected.append(row)
            selected_ids.add(row.person_id)
    return selected[:limit]


def estimate_cost(
    mode: str, school_count: int, alias_count: int, paper_limit: int, verify_roles: int,
    profile_discovery: bool = False,
) -> dict[str, Any]:
    search_calls = alias_count * max(1, school_count) if mode != "discover" else alias_count
    detail_calls = search_calls * paper_limit
    org_search_calls = school_count if mode != "discover" else 0
    # Collaboration enrichment and role verification run independently for each school.
    # Discover currently aggregates author affiliations directly and does not call org_detail.
    org_detail_calls = school_count if mode == "collaboration" else 0
    person_detail_calls = verify_roles * school_count if mode != "discover" else 0
    discovery_search_calls = alias_count if profile_discovery else 0
    discovery_detail_calls = discovery_search_calls * paper_limit
    estimate = (
        (search_calls + discovery_search_calls) * API_SPEC["paper_search_pro"][2]
        + (detail_calls + discovery_detail_calls) * API_SPEC["paper_detail"][2]
        + org_search_calls * API_SPEC["org_search"][2]
        + org_detail_calls * API_SPEC["org_detail"][2]
        + person_detail_calls * API_SPEC["person_detail"][2]
    )
    return {
        "worst_case_cny": round(estimate, 2), "paper_search_pro_calls": search_calls + discovery_search_calls,
        "paper_detail_calls": detail_calls + discovery_detail_calls, "org_search_calls": org_search_calls,
        "org_detail_calls": org_detail_calls, "person_detail_calls": person_detail_calls,
        "profile_discovery_calls": discovery_search_calls,
    }


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
        name = str(row.get("organization") or "").strip()
        aliases = [str(value) for value in row.get("organization_aliases") or []]
        identity_names = [name, *aliases]
        if not name or any(normalized_text(value) in existing for value in identity_names):
            continue
        if classify_org(" ".join(identity_names)) != "academic":
            continue
        if not contains_any(" ".join(identity_names), ("university", "college", "大学", "学院")):
            continue
        level = max(school_difficulty(value, tiers) for value in identity_names)
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


def recommend_for_school(
    client: AMinerClient, school: str, department: str, direction: str, aliases: list[str], paper_limit: int,
    profile: dict[str, Any] | None, max_author_lookups: int, allow_name_fallback: bool,
    verify_roles: int, warnings: list[str], organization_cache: dict[str, ResolvedOrganization | None],
    include_collaboration: bool, allow_cross_discipline: bool,
) -> dict[str, Candidate]:
    organization = organization_cache.get(school)
    if school not in organization_cache:
        organization = resolve_organization(client, school)
        organization_cache[school] = organization
    if organization is None:
        warnings.append(f"organization not found in AMiner: {school}; try an official English name or discover mode")
        return {}
    terms = direction_aliases(direction, aliases)[:3]
    search_rows: dict[str, dict[str, Any]] = {}
    for term in terms:
        for row in paper_search(client, term, organization.canonical_name, paper_limit):
            if row.get("id"):
                search_rows[str(row["id"])] = row
    if not search_rows and normalized_text(organization.canonical_name) != normalized_text(school):
        warnings.append(f"no papers found with canonical name {organization.canonical_name}; retried original name {school}")
        for term in terms:
            for row in paper_search(client, term, school, paper_limit):
                if row.get("id"):
                    search_rows[str(row["id"])] = row
    info = batch_paper_info(client, list(search_rows), warnings)
    info_by_id = {str(row.get("id")): row for row in info if row.get("id")}
    details = fetch_paper_details(client, list(search_rows), len(search_rows), warnings)
    papers = [dict(search_rows[paper_id], **info_by_id.get(paper_id, {})) for paper_id in search_rows]
    candidates = resolve_people(
        client, papers, details, organization, terms, max_author_lookups, allow_name_fallback, warnings
    )
    for candidate in candidates.values():
        candidate.source_schools.add(school)
    candidates = filter_discipline_conflicts(candidates, terms, allow_cross_discipline, warnings)
    if include_collaboration:
        enrich_collaboration(client, candidates, details)
    score_candidates(candidates, terms, school, department, profile)
    if verify_roles:
        verify_candidate_roles(client, candidates, verify_roles, warnings)
        score_candidates(candidates, terms, school, department, profile)
    return candidates


def discover_institutions(
    client: AMinerClient, direction: str, aliases: list[str], paper_limit: int, warnings: list[str]
) -> list[dict[str, Any]]:
    terms = direction_aliases(direction, aliases)[:3]
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
         "evidence_papers": list(evidence[org_id].values())}
        for org_id, count in counts.most_common()
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("named", "tier", "collaboration", "profile", "discover"))
    parser.add_argument("--direction", required=True)
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
    parser.add_argument("--max-author-lookups", type=int, default=30)
    parser.add_argument("--collaboration-type", choices=("all", "academic", "industry"), default="all")
    parser.add_argument("--allow-name-fallback", action="store_true")
    parser.add_argument("--allow-cross-discipline", action="store_true")
    parser.add_argument("--verify-roles", type=int, default=0, help="Paid person_detail calls for top N candidates")
    parser.add_argument("--max-cost", type=float, default=5.0)
    parser.add_argument("--yes", action="store_true", help="Allow a worst-case estimate above --max-cost")
    parser.add_argument("--no-auto-expand-profile", action="store_true", help="Search only explicitly selected profile schools")
    parser.add_argument("--output", type=Path)
    return parser


def write_result(result: dict[str, Any], output: Path | None) -> None:
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        print(f"Saved result to {output}")
    else:
        sys.stdout.write(rendered)


def main() -> None:
    args = build_parser().parse_args()
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
    auto_expand_profile = args.mode == "profile" and not args.no_auto_expand_profile
    if args.mode != "discover" and not schools and not auto_expand_profile:
        raise SystemExit("provide --school, --schools, --tier, or target schools in the profile")

    requested_school_count = len(schools)
    schools = schools[: args.max_schools]
    estimated_school_count = args.max_schools if auto_expand_profile else len(schools)
    estimate = estimate_cost(
        args.mode, estimated_school_count, min(3, 1 + len(aliases)), args.paper_limit, args.verify_roles,
        profile_discovery=auto_expand_profile,
    )
    if estimate["worst_case_cny"] >= args.max_cost and not args.yes:
        raise SystemExit(
            f"Worst-case API cost estimate ¥{estimate['worst_case_cny']:.2f} reaches --max-cost ¥{args.max_cost:.2f}; "
            "reduce limits or rerun with --yes after explicit confirmation"
        )

    warnings: list[str] = []
    if requested_school_count > len(schools):
        warnings.append(f"school cap omitted {requested_school_count - len(schools)} schools; provide a region or explicit list")
    try:
        client = AMinerClient(os.getenv("AMINER_API_KEY", ""))
    except ValueError as exc:
        raise SystemExit(f"{exc}; configure it with tools/setup-aminer-token before calling AMiner") from exc
    try:
        if args.mode == "discover":
            institutions = discover_institutions(client, args.direction, aliases, args.paper_limit, warnings)
            result = {
                "workflow": "discover", "query": {"direction": args.direction, "aliases": aliases},
                "institutions": institutions[: args.candidate_limit], "warnings": warnings, "errors": [],
                "cost_estimate": estimate, "cost": client.cost.summary(),
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
                resolved = resolve_organization(client, str(row.get("organization") or ""))
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
            found = recommend_for_school(
                client, school, args.department, args.direction, aliases, args.paper_limit, profile,
                args.max_author_lookups, args.allow_name_fallback, args.verify_roles, warnings, organization_cache,
                args.mode == "collaboration", args.allow_cross_discipline,
            )
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

        all_candidates = mark_duplicate_names(all_candidates)
        if profile:
            assign_bands(all_candidates, profile, tiers)

        def rank_key(candidate: Candidate) -> tuple[float, float]:
            if args.mode == "collaboration":
                count = len(candidate.collaboration_orgs) if args.collaboration_type == "all" else sum(
                    value == args.collaboration_type for value in candidate.collaboration_types.values()
                )
                return float(count), float(candidate.scores.get("overall") or 0)
            return float(candidate.scores.get("overall") or 0), float(len(candidate.papers))

        all_ranked = sorted(all_candidates.values(), key=rank_key, reverse=True)
        ranked = (
            select_profile_portfolio(all_ranked, args.candidate_limit)
            if args.mode == "profile" else all_ranked[: args.candidate_limit]
        )
        portfolio_band_counts = dict(Counter(
            candidate.recommendation_band for candidate in ranked if candidate.recommendation_band
        ))
        result = {
            "workflow": args.mode,
            "query": {"direction": args.direction, "aliases": aliases, "schools": schools,
                      "department": args.department or None, "tier": tier_name or None,
                      "collaboration_type": args.collaboration_type if args.mode == "collaboration" else None},
            "resolved_organizations": {
                school: ({"id": org.org_id, "canonical_name": org.canonical_name} if org else None)
                for school, org in organization_cache.items()
            },
            "candidate_counts_by_school": per_school,
            "school_selection": {"requested_count": requested_school_count, "searched_count": len(schools),
                                 "truncated": requested_school_count > args.max_schools,
                                 "auto_expanded": bool(auto_added_schools),
                                 "auto_added_schools": auto_added_schools},
            "candidates": [candidate.to_dict() for candidate in ranked],
            "portfolio_band_counts": portfolio_band_counts if args.mode == "profile" else None,
            "warnings": list(dict.fromkeys(warnings)), "errors": [], "cost_estimate": estimate,
            "cost": client.cost.summary(),
            "fallback": {
                "suggestions": ["try English/official organization names", "add direction aliases", "use --mode discover"]
                if not ranked else []
            },
            "method_notes": [
                "Only authors whose paper affiliation matches the resolved organization ID are considered by default.",
                "Every candidate has at least one non-empty, dated, direction-matched paper evidence item.",
                "Advisor role and current recruitment require official verification unless a role is explicitly returned.",
                "Reach/match/safer labels are heuristic bands, not admission probabilities.",
            ],
        }
        write_result(result, args.output)
    except AMinerAPIError as exc:
        result = {
            "workflow": args.mode, "query": {"direction": args.direction, "schools": schools},
            "candidates": [], "warnings": warnings, "errors": [exc.to_dict()],
            "cost_estimate": estimate, "cost": client.cost.summary(),
        }
        write_result(result, args.output)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
