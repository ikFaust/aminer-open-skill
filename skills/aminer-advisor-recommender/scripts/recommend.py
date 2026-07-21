#!/usr/bin/env python3
"""Run one of four AMiner-backed school/advisor recommendation workflows."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from aminer_client import AMinerClient, unwrap


SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TIERS = SKILL_ROOT / "references" / "school-tiers.json"
CURRENT_YEAR = datetime.now(timezone.utc).year


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
    ordered = [direction, *aliases]
    return list(dict.fromkeys(x.strip() for x in ordered if x and x.strip()))


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
    papers: dict[str, dict[str, Any]] = field(default_factory=dict)
    collaboration_orgs: dict[str, str] = field(default_factory=dict)
    collaboration_types: dict[str, str] = field(default_factory=dict)
    scores: dict[str, float | None] = field(default_factory=dict)
    confidence: str = "low"
    recommendation_band: str | None = None
    source_schools: set[str] = field(default_factory=set)
    alternate_profile_ids: list[str] = field(default_factory=list)
    identity_ambiguous: bool = False

    @property
    def display_name(self) -> str:
        return self.name_zh or self.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "person_id": self.person_id,
            "name": self.display_name,
            "name_en": self.name,
            "organization": self.org_zh or self.org,
            "organization_id": self.org_id,
            "source_schools": sorted(self.source_schools),
            "aminer_url": f"https://www.aminer.cn/profile/{self.person_id}",
            "alternate_profile_ids": self.alternate_profile_ids,
            "identity_ambiguous": self.identity_ambiguous,
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
            "needs_verification": [
                "whether this scholar is an eligible prospective advisor",
                "current department affiliation",
                "current recruitment status",
                "admissions requirements and deadline",
            ] + (["multiple AMiner profiles may refer to the same or different people"] if self.identity_ambiguous else []),
        }


def read_tiers(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def paper_search(client: AMinerClient, direction: str, school: str, size: int) -> list[dict[str, Any]]:
    payload = client.call(
        "paper_search_pro",
        {"keyword": direction, "org": school, "order": "year", "page": 0, "size": size},
    )
    return unwrap(payload)


def batch_paper_info(client: AMinerClient, paper_ids: list[str]) -> list[dict[str, Any]]:
    if not paper_ids:
        return []
    return unwrap(client.call("paper_info", {"ids": paper_ids[:100]}))


def resolve_people(
    client: AMinerClient,
    papers: list[dict[str, Any]],
    school: str,
    direction_terms: list[str],
    max_author_lookups: int,
) -> dict[str, Candidate]:
    by_id: dict[str, Candidate] = {}
    names: list[str] = []
    paper_by_author: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for paper in papers:
        for author in paper.get("authors") or []:
            if not isinstance(author, dict):
                continue
            name = str(author.get("name") or author.get("name_zh") or "").strip()
            if name:
                names.append(name)
                paper_by_author[normalized_text(name)].append(paper)

    for name in list(dict.fromkeys(names))[:max_author_lookups]:
        people = unwrap(client.call("person_search", {"name": name, "org": school, "size": 3}))
        for person in people:
            person_id = str(person.get("id") or "")
            if not person_id:
                continue
            candidate = by_id.setdefault(
                person_id,
                Candidate(
                    person_id=person_id,
                    name=str(person.get("name") or name),
                    name_zh=str(person.get("name_zh") or ""),
                    org=str(person.get("org") or ""),
                    org_zh=str(person.get("org_zh") or ""),
                    org_id=str(person.get("org_id") or ""),
                    interests=[str(x) for x in (person.get("interests") or [])],
                    n_citation=person.get("n_citation") if isinstance(person.get("n_citation"), int) else None,
                ),
            )
            for paper in paper_by_author[normalized_text(name)]:
                title = str(paper.get("title") or "")
                evidence = {
                    "id": paper.get("id"),
                    "title": title,
                    "year": paper.get("year"),
                    "aminer_url": f"https://www.aminer.cn/pub/{paper.get('id')}",
                    "direction_term_match": contains_any(
                        " ".join([title, str(paper.get("abstract_slice") or "")]), direction_terms
                    ),
                }
                candidate.papers[str(paper.get("id"))] = evidence
    return by_id


def classify_org(name: str) -> str:
    value = normalized_text(name)
    industry_terms = (
        "company", "corporation", "corp", "inc", "ltd", "laboratories", "microsoft",
        "google", "meta", "alibaba", "tencent", "huawei", "bytedance", "字节", "腾讯",
        "阿里", "华为", "百度", "商汤", "旷视",
    )
    academic_terms = (
        "university", "college", "institute", "academy", "school", "大学", "学院", "研究院",
        "科学院", "实验室",
    )
    if any(term in value for term in industry_terms):
        return "industry"
    if any(term in value for term in academic_terms):
        return "academic"
    return "unknown"


def enrich_collaboration(
    client: AMinerClient,
    candidates: dict[str, Candidate],
    max_papers: int,
) -> None:
    unique_papers: list[str] = []
    for candidate in candidates.values():
        unique_papers.extend(candidate.papers)
    unique_papers = list(dict.fromkeys(unique_papers))[:max_papers]
    candidate_names = {
        normalized_text(name): candidate
        for candidate in candidates.values()
        for name in (candidate.name, candidate.name_zh)
        if name
    }
    for paper_id in unique_papers:
        detail_rows = unwrap(client.call("paper_detail", {"id": paper_id}))
        if not detail_rows:
            continue
        authors = [row for row in (detail_rows[0].get("authors") or []) if isinstance(row, dict)]
        involved = {
            candidate_names[normalized_text(author.get("name"))].person_id:
            candidate_names[normalized_text(author.get("name"))]
            for author in authors
            if normalized_text(author.get("name")) in candidate_names
        }
        for candidate in involved.values():
            for author in authors:
                org_name = str(author.get("org") or "").strip()
                org_id = str(author.get("orgid") or normalized_text(org_name))
                same_org_id = bool(candidate.org_id and org_id == candidate.org_id)
                same_org_name = contains_any(org_name, [candidate.org, candidate.org_zh])
                if org_name and not same_org_id and not same_org_name:
                    candidate.collaboration_orgs[org_id] = org_name

    org_ids = list(dict.fromkeys(
        org_id for candidate in candidates.values() for org_id in candidate.collaboration_orgs
        if org_id and " " not in org_id
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
            org_id: verified_types.get(org_id, classify_org(name))
            for org_id, name in candidate.collaboration_orgs.items()
        }


def score_candidates(
    candidates: dict[str, Candidate],
    direction_terms: list[str],
    school: str,
    department: str,
    profile: dict[str, Any] | None,
) -> None:
    profile_text = " "
    if profile:
        profile_text = json.dumps(
            {
                "research_interests": profile.get("research_interests", []),
                "research_projects": profile.get("research_projects", []),
                "publications": profile.get("publications", []),
                "internships": profile.get("internships", []),
            },
            ensure_ascii=False,
        )
    profile_tokens = tokens(profile_text)

    for candidate in candidates.values():
        evidence_text = " ".join(
            [candidate.name, candidate.org, candidate.org_zh, *candidate.interests]
            + [str(p.get("title") or "") for p in candidate.papers.values()]
        )
        matches = sum(1 for term in direction_terms if contains_any(evidence_text, [term]))
        direction_score = min(100.0, 45.0 + 15.0 * matches + 8.0 * len(candidate.papers))

        recent_years = [
            int(p["year"]) for p in candidate.papers.values()
            if isinstance(p.get("year"), int) and int(p["year"]) >= CURRENT_YEAR - 5
        ]
        activity_score = min(100.0, len(recent_years) * 25.0) if recent_years else 0.0
        affiliation_text = f"{candidate.org} {candidate.org_zh}"
        affiliation_score = 100.0 if contains_any(affiliation_text, [school]) else 50.0
        if department and contains_any(affiliation_text, [department]):
            affiliation_score = 100.0

        categories = list(candidate.collaboration_types.values())
        academic_score = min(100.0, categories.count("academic") * 20.0) if categories else None
        industry_score = min(100.0, categories.count("industry") * 25.0) if categories else None
        evidence_tokens = tokens(evidence_text)
        applicant_score = None
        if profile_tokens:
            overlap = len(profile_tokens & evidence_tokens)
            applicant_score = min(100.0, 25.0 + overlap * 12.5)

        components: list[tuple[float | None, float]] = [
            (direction_score, 0.35),
            (activity_score, 0.20),
            (affiliation_score, 0.15),
            (academic_score, 0.10),
            (industry_score, 0.10),
            (applicant_score, 0.10),
        ]
        known_weight = sum(weight for value, weight in components if value is not None)
        total = sum(float(value) * weight for value, weight in components if value is not None)
        overall = round(total / known_weight, 1) if known_weight else 0.0
        candidate.scores = {
            "overall": overall,
            "direction_fit": round(direction_score, 1),
            "recent_activity": round(activity_score, 1),
            "institution_department_fit": round(affiliation_score, 1),
            "academic_collaboration": academic_score,
            "industry_collaboration": industry_score,
            "applicant_experience_fit": applicant_score,
        }
        known = sum(value is not None for value, _ in components)
        candidate.confidence = "high" if known >= 6 and len(candidate.papers) >= 2 else "medium" if known >= 4 else "low"


def applicant_readiness(profile: dict[str, Any]) -> float:
    score = 35.0
    percentile = profile.get("rank_percentile")
    if isinstance(percentile, (int, float)):
        score += max(0.0, min(30.0, (50.0 - float(percentile)) * 0.75))
    gpa = profile.get("gpa")
    scale = profile.get("gpa_scale")
    if isinstance(gpa, (int, float)) and isinstance(scale, (int, float)) and scale:
        score += max(0.0, min(20.0, float(gpa) / float(scale) * 20.0))
    score += min(10.0, len(profile.get("research_projects") or []) * 3.0)
    score += min(10.0, len(profile.get("publications") or []) * 5.0)
    score += min(5.0, len(profile.get("internships") or []) * 2.0)
    return min(100.0, score)


def consolidate_duplicate_profiles(candidates: dict[str, Candidate]) -> dict[str, Candidate]:
    grouped: dict[tuple[str, tuple[str, ...]], list[Candidate]] = defaultdict(list)
    for candidate in candidates.values():
        grouped[(normalized_text(candidate.display_name), tuple(sorted(candidate.source_schools)))].append(candidate)
    result: dict[str, Candidate] = {}
    for rows in grouped.values():
        rows.sort(
            key=lambda row: (len(row.papers), row.n_citation if row.n_citation is not None else -1),
            reverse=True,
        )
        primary = rows[0]
        if len(rows) > 1:
            primary.identity_ambiguous = True
            primary.alternate_profile_ids = [row.person_id for row in rows[1:]]
            for row in rows[1:]:
                primary.papers.update(row.papers)
                primary.collaboration_orgs.update(row.collaboration_orgs)
                primary.collaboration_types.update(row.collaboration_types)
        result[primary.person_id] = primary
    return result


def assign_bands(candidates: dict[str, Candidate], profile: dict[str, Any], tier_difficulty: int) -> None:
    readiness = applicant_readiness(profile)
    threshold = {3: 82.0, 2: 72.0, 1: 62.0}.get(tier_difficulty, 70.0)
    for candidate in candidates.values():
        adjusted = readiness + (float(candidate.scores.get("applicant_experience_fit") or 50) - 50) * 0.2
        if adjusted >= threshold + 8:
            candidate.recommendation_band = "相对稳妥"
        elif adjusted >= threshold - 5:
            candidate.recommendation_band = "匹配"
        else:
            candidate.recommendation_band = "冲刺"


def recommend_for_school(
    client: AMinerClient,
    school: str,
    department: str,
    direction: str,
    aliases: list[str],
    paper_limit: int,
    collaboration_papers: int,
    profile: dict[str, Any] | None,
    max_author_lookups: int,
) -> dict[str, Candidate]:
    terms = direction_aliases(direction, aliases)
    search_rows: dict[str, dict[str, Any]] = {}
    for term in terms[:3]:
        for row in paper_search(client, term, school, paper_limit):
            if row.get("id"):
                search_rows[str(row["id"])] = row
    info = batch_paper_info(client, list(search_rows))
    candidates = resolve_people(client, info, school, terms, max_author_lookups)
    for candidate in candidates.values():
        candidate.source_schools.add(school)
    if collaboration_papers:
        enrich_collaboration(client, candidates, collaboration_papers)
    score_candidates(candidates, terms, school, department, profile)
    return candidates


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("named", "tier", "collaboration", "profile"))
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
    parser.add_argument("--collaboration-papers", type=int, default=0)
    parser.add_argument(
        "--collaboration-type", choices=("all", "academic", "industry"), default="all"
    )
    parser.add_argument("--output", type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.mode == "named" and not args.school:
        raise SystemExit("--school is required for named mode")
    if args.mode == "profile" and not args.profile:
        raise SystemExit("--profile is required for profile mode")
    if args.paper_limit < 1 or args.candidate_limit < 1 or args.max_schools < 1 or args.max_author_lookups < 1:
        raise SystemExit("limits must be positive")

    profile = json.loads(args.profile.read_text(encoding="utf-8")) if args.profile else None
    tiers = read_tiers(args.tiers_file)
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
    if not schools:
        raise SystemExit("provide --school, --schools, --tier, or target schools in the profile")
    requested_school_count = len(schools)
    schools = schools[: args.max_schools]

    token = os.getenv("AMINER_API_KEY", "")
    client = AMinerClient(token)
    aliases = parse_csv(args.aliases)
    collaboration_papers = args.collaboration_papers
    if args.mode == "collaboration" and collaboration_papers == 0:
        collaboration_papers = min(args.paper_limit, 10)

    all_candidates: dict[str, Candidate] = {}
    per_school: dict[str, int] = {}
    for school in schools:
        found = recommend_for_school(
            client,
            school,
            args.department,
            args.direction,
            aliases,
            args.paper_limit,
            collaboration_papers,
            profile,
            args.max_author_lookups,
        )
        per_school[school] = len(found)
        for person_id, candidate in found.items():
            if person_id not in all_candidates:
                all_candidates[person_id] = candidate
                continue
            existing = all_candidates[person_id]
            existing.source_schools.update(candidate.source_schools)
            existing.papers.update(candidate.papers)
            existing.collaboration_orgs.update(candidate.collaboration_orgs)
            existing.collaboration_types.update(candidate.collaboration_types)
            if float(candidate.scores.get("overall") or 0) > float(existing.scores.get("overall") or 0):
                existing.scores = candidate.scores
                existing.confidence = candidate.confidence

    if profile:
        difficulty = int(tiers["tiers"].get(tier_name, {}).get("difficulty", 2))
        assign_bands(all_candidates, profile, difficulty)

    all_candidates = consolidate_duplicate_profiles(all_candidates)

    def rank_key(candidate: Candidate) -> tuple[float, float]:
        if args.mode == "collaboration":
            if args.collaboration_type == "all":
                collaboration_count = len(candidate.collaboration_orgs)
            else:
                collaboration_count = sum(
                    value == args.collaboration_type for value in candidate.collaboration_types.values()
                )
            return float(collaboration_count), float(candidate.scores.get("overall") or 0)
        return float(candidate.scores.get("overall") or 0), 0.0

    ranked = sorted(all_candidates.values(), key=rank_key, reverse=True)[: args.candidate_limit]
    result = {
        "workflow": args.mode,
        "query": {
            "direction": args.direction,
            "aliases": aliases,
            "schools": schools,
            "department": args.department or None,
            "tier": tier_name or None,
            "collaboration_type": args.collaboration_type if args.mode == "collaboration" else None,
        },
        "candidate_counts_by_school": per_school,
        "school_selection": {
            "requested_count": requested_school_count,
            "searched_count": len(schools),
            "truncated": requested_school_count > len(schools),
            "guidance": "Provide a region or explicit school list to avoid arbitrary truncation."
            if requested_school_count > len(schools)
            else None,
        },
        "candidates": [candidate.to_dict() for candidate in ranked],
        "cost": client.cost.summary(),
        "method_notes": [
            "School tiers are filters, not universal quality rankings.",
            "Reach/match/safer labels are heuristic bands, not admission probabilities.",
            "Department affiliation and current recruitment must be verified on official sites.",
            "Missing collaboration data is reported as unknown, not as zero collaboration.",
        ],
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"Saved result to {args.output}")
    else:
        sys.stdout.write(rendered)


if __name__ == "__main__":
    main()
