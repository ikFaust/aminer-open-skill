import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from aminer_client import AMinerAPIError, AMinerClient, CostLedger  # noqa: E402
from recommend import (  # noqa: E402
    Candidate,
    ResolvedOrganization,
    applicant_readiness,
    assign_bands,
    build_rank_key,
    classify_org,
    clean_paper,
    choose_profile_expansion_schools,
    discover_institutions,
    enrich_collaboration,
    estimate_cost,
    extract_institution_name,
    filter_discipline_conflicts,
    institution_level,
    is_mainland_china_institution,
    mark_duplicate_names,
    recommend_for_school,
    resolve_people,
    score_candidates,
    select_profile_portfolio,
    school_level_map,
    verify_candidate_roles,
)


class FakeClient:
    def __init__(self, people=None):
        self.people = people or []
        self.calls = []

    def call(self, api, params):
        self.calls.append((api, params))
        if api == "person_search":
            return {"success": True, "code": 200, "data": self.people}
        raise AssertionError(f"unexpected API: {api}")


def tiers():
    return {
        "english_names": {"Tsinghua University": "清华大学"},
        "tiers": {
            "985": {"schools": ["清华大学"], "difficulty": 3},
            "华五": {"schools": [], "difficulty": 3},
            "211": {"schools": ["苏州大学"], "difficulty": 2},
            "双一流": {"schools": ["清华大学", "苏州大学", "山西大学"], "difficulty": 1},
        }
    }


class RecommendationLogicTests(unittest.TestCase):
    def test_cost_ledger(self):
        ledger = CostLedger()
        ledger.add("paper_search_pro")
        ledger.add("paper_detail")
        self.assertEqual(ledger.summary()["total_cost_cny"], 0.02)

    def test_failed_http_call_is_not_charged(self):
        body = io.BytesIO(b'{"code":401,"success":false,"msg":"unauthorized"}')
        error = urllib.error.HTTPError("https://example", 401, "Unauthorized", {}, body)
        client = AMinerClient("bad", max_retries=1)
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(AMinerAPIError):
                client.call("person_search", {"name": "x"})
        self.assertEqual(client.cost.summary()["total_calls"], 0)

    def test_org_classification(self):
        self.assertEqual(classify_org("Tsinghua University"), "academic")
        self.assertEqual(classify_org("Example Provincial University"), "academic")
        self.assertEqual(classify_org("Microsoft Research Asia"), "industry")
        self.assertEqual(classify_org("Unknown Group"), "unknown")

    def test_dirty_or_unmatched_paper_is_rejected(self):
        self.assertIsNone(clean_paper({"id": "x", "title": "", "year": 2025}, None, ["vision"]))
        self.assertIsNone(clean_paper({"id": "x", "title": "Database", "year": 2025}, None, ["vision"]))
        self.assertIsNotNone(clean_paper({"id": "x", "title": "Computer Vision", "year": 2025}, None, ["vision"]))

    def test_author_resolution_requires_matching_org_id(self):
        papers = [{"id": "p1", "title": "Computer Vision", "year": 2025}]
        details = {
            "p1": {
                "id": "p1", "title": "Computer Vision", "year": 2025,
                "authors": [
                    {"name": "Correct Author", "org": "Target University", "orgid": "org-1"},
                    {"name": "Wrong Coauthor", "org": "Other University", "orgid": "org-2"},
                ],
            }
        }
        people = [{"id": "a1", "name": "Correct Author", "org": "Target University", "org_id": "org-1"}]
        client = FakeClient(people)
        result = resolve_people(
            client, papers, details, ResolvedOrganization("目标大学", "org-1", "Target University"),
            ["vision"], 10, False, [],
        )
        self.assertEqual(set(result), {"a1"})
        self.assertEqual(client.calls[0][1]["org_id"], ["org-1"])

    def test_multiple_exact_profiles_remain_one_unresolved_author(self):
        papers = [{"id": "p1", "title": "Computer Vision", "year": 2025}]
        details = {"p1": {"id": "p1", "title": "Computer Vision", "year": 2025,
                          "authors": [{"name": "Wei Zhang", "org": "Target University", "orgid": "org-1"}]}}
        people = [
            {"id": "a1", "name": "Wei Zhang", "org": "Target University", "org_id": "org-1"},
            {"id": "a2", "name": "Wei Zhang", "org": "Target University", "org_id": "org-1"},
        ]
        result = resolve_people(
            FakeClient(people), papers, details,
            ResolvedOrganization("目标大学", "org-1", "Target University"), ["vision"], 10, False, [],
        )
        self.assertEqual(len(result), 1)
        candidate = next(iter(result.values()))
        self.assertTrue(candidate.identity_ambiguous)
        self.assertTrue(candidate.person_id.startswith("unresolved:"))
        self.assertIsNone(candidate.to_dict()["aminer_url"])
        self.assertEqual(candidate.alternate_profile_ids, ["a1", "a2"])

    def test_every_candidate_requires_direction_evidence(self):
        candidate = Candidate(person_id="p1", name="A", org="X", org_id="o")
        candidate.papers["x"] = {
            "id": "x", "title": "Computer Vision", "year": 2025, "direction_term_match": True,
            "matched_terms": ["vision"],
        }
        score_candidates({"p1": candidate}, ["vision"], "X", "", None)
        self.assertGreater(candidate.scores["direction_fit"], 0)
        self.assertIsNone(candidate.scores["academic_collaboration"])

    def test_same_name_profiles_are_not_merged(self):
        first = Candidate(person_id="p1", name="Zhang Wei", source_schools={"X"})
        second = Candidate(person_id="p2", name="Zhang Wei", source_schools={"X"})
        result = mark_duplicate_names({"p1": first, "p2": second})
        self.assertEqual(set(result), {"p1", "p2"})
        self.assertTrue(first.identity_ambiguous)
        self.assertEqual(first.alternate_profile_ids, ["p2"])

    def test_double_first_class_only_school_sits_between_ordinary_and_211(self):
        self.assertEqual(institution_level("清华大学", tiers()), 3)
        self.assertEqual(institution_level("苏州大学", tiers()), 2)
        self.assertEqual(institution_level("山西大学", tiers()), 1)
        self.assertEqual(institution_level("某省属二本院校", tiers()), 0)

    def test_rank_by_rising_prefers_smaller_citation_base(self):
        def with_papers(person_id, n_citation, count, year):
            candidate = Candidate(person_id=person_id, name=person_id, n_citation=n_citation)
            candidate.papers = {f"{person_id}{i}": {"id": f"{person_id}{i}", "title": "T", "year": year,
                                                    "direction_term_match": True} for i in range(count)}
            candidate.scores = {"overall": 60.0}
            return candidate

        mega_pi = with_papers("mega", 80000, 5, 2026)
        early_career = with_papers("young", 800, 3, 2026)
        stale = with_papers("stale", 100, 3, 2015)
        key = build_rank_key("named", "all", "rising")
        ranked = sorted([mega_pi, early_career, stale], key=key, reverse=True)
        self.assertEqual([c.person_id for c in ranked], ["young", "mega", "stale"])

        by_citation = sorted([mega_pi, early_career], key=build_rank_key("named", "all", "citation"), reverse=True)
        self.assertEqual(by_citation[0].person_id, "mega")
        by_recent = sorted([early_career, stale], key=build_rank_key("named", "all", "recent"), reverse=True)
        self.assertEqual(by_recent[0].person_id, "young")

    def test_affiliation_string_reduces_to_university_main_body(self):
        self.assertEqual(
            extract_institution_name("School of CS, Beijing Technology and Business University, Beijing, PR China"),
            "Beijing Technology and Business University",
        )
        self.assertEqual(
            extract_institution_name("Institute of Digital Civilization, University of Shanghai for Science and Technology, Shanghai, China"),
            "University of Shanghai for Science and Technology",
        )
        self.assertEqual(extract_institution_name("清华大学计算机系智能技术与系统国家重点实验室"), "清华大学")

    def test_overseas_and_unresolved_institutions_are_not_auto_added(self):
        profile = {"undergraduate_institution": "某省属二本院校"}
        institutions = [
            {"organization": "Department of Mechanical Engineering, Chalmers University of Technology, Gothenburg, Sweden",
             "organization_main": "Chalmers University of Technology",
             "organization_aliases": ["Chalmers University of Technology"], "matched_paper_count": 4},
            {"organization": "Dept of CSE(AI), KIET (Deemed to Be University), Ghaziabad, U.P., India",
             "organization_main": "KIET (Deemed to Be University)",
             "organization_aliases": ["KIET University"], "matched_paper_count": 3},
            {"organization": "School of CS, Some Unresolvable University, Beijing, PR China",
             "organization_main": "Some Unresolvable University", "matched_paper_count": 3},
            {"organization": "School of CS, Example Provincial University, Nanchang, PR China",
             "organization_main": "Example Provincial University",
             "organization_aliases": ["Example Provincial University", "某省属大学"], "matched_paper_count": 2},
        ]
        result = choose_profile_expansion_schools(profile, ["清华大学"], institutions, tiers(), limit=4)
        self.assertEqual(result, ["Example Provincial University"])

    def test_department_level_orgid_author_is_accepted_by_school_text(self):
        papers = [{"id": "p1", "title": "Computer Vision", "year": 2025}]
        details = {"p1": {"id": "p1", "title": "Computer Vision", "year": 2025,
                          "authors": [{"name": "Dept Author",
                                       "org": "Department of Automation, Tsinghua University",
                                       "orgid": "dept-9"}]}}
        people = [{"id": "a1", "name": "Dept Author", "org": "Tsinghua University", "org_id": "org-1"}]
        result = resolve_people(
            FakeClient(people), papers, details,
            ResolvedOrganization("清华大学", "org-1", "Tsinghua University", ["清华大学"]),
            ["vision"], 10, False, [],
        )
        self.assertEqual(set(result), {"a1"})

    def test_department_profile_found_via_org_text_retry(self):
        papers = [{"id": "p1", "title": "Computer Vision", "year": 2025}]
        details = {"p1": {"id": "p1", "title": "Computer Vision", "year": 2025,
                          "authors": [{"name": "Dept Author",
                                       "org": "Department of Automation, Tsinghua University",
                                       "orgid": "dept-9"}]}}

        class DeptClient:
            def __init__(self):
                self.calls = []

            def call(self, api, params):
                self.calls.append((api, params))
                assert api == "person_search"
                if "org_id" in params:
                    return {"success": True, "data": []}  # server-side school-ID filter excludes dept profiles
                return {"success": True, "data": [
                    {"id": "a1", "name": "Dept Author",
                     "org": "Department of Automation, Tsinghua University", "org_id": "dept-9"},
                ]}

        client = DeptClient()
        result = resolve_people(
            client, papers, details,
            ResolvedOrganization("清华大学", "org-1", "Tsinghua University", ["清华大学"]),
            ["vision"], 10, False, [],
        )
        self.assertEqual(set(result), {"a1"})
        self.assertEqual(client.calls[1][1].get("org"), "Tsinghua University")

    def test_english_school_name_levels_through_resolved_aliases(self):
        # Via the english_names map even when org_search aliases lack the Chinese name
        self.assertEqual(institution_level("Tsinghua University", tiers()), 3)
        cache = {"Tsinghua University": ResolvedOrganization(
            "Tsinghua University", "org-thu", "Tsinghua University", [])}
        levels = school_level_map(["Tsinghua University"], cache, tiers())
        self.assertEqual(levels["Tsinghua University"], 3)
        profile = {"undergraduate_institution": "某211大学", "gpa": 3.6, "gpa_scale": 4.0,
                   "rank_percentile": 15, "research_projects": [{}, {}], "publications": [],
                   "internships": [{}]}
        candidate = Candidate(person_id="p1", name="A", source_schools={"Tsinghua University"})
        candidate.scores = {"applicant_experience_fit": 50.0}
        assign_bands({"p1": candidate}, profile, tiers(), levels)
        self.assertEqual(candidate.recommendation_band, "冲刺（启发式）")

    def test_generic_tier_phrases_are_recognized(self):
        self.assertEqual(institution_level("某211大学", tiers()), 2)
        self.assertEqual(institution_level("华东某985高校", tiers()), 3)
        self.assertEqual(institution_level("某双一流高校", tiers()), 1)
        self.assertEqual(institution_level("双非一本院校", tiers()), 1)
        self.assertEqual(institution_level("双非院校", tiers()), 0)
        self.assertEqual(institution_level("普通本科院校", tiers()), 0)

    def test_missing_orgid_authors_are_never_sent_to_paid_org_detail(self):
        candidate = Candidate(person_id="p1", name="A", org_id="o1")
        candidate.papers["x1"] = {"id": "x1", "title": "Computer Vision", "year": 2025,
                                  "direction_term_match": True, "matched_terms": ["vision"]}
        details = {"x1": {"id": "x1", "authors": [
            {"name": "A", "org": "Target University", "orgid": "o1"},
            {"name": "B", "org": "清华大学"},
        ]}}

        class NoOrgDetailClient:
            def call(self, api, params):
                raise AssertionError(f"unexpected paid call: {api}")

        enrich_collaboration(NoOrgDetailClient(), {"p1": candidate}, details, [])
        self.assertEqual(list(candidate.collaboration_orgs.values()), ["清华大学"])
        self.assertTrue(next(iter(candidate.collaboration_orgs)).startswith("name:"))
        self.assertEqual(candidate.collaboration_types[next(iter(candidate.collaboration_orgs))], "academic")

    def test_org_detail_failure_degrades_to_name_classification(self):
        candidate = Candidate(person_id="p1", name="A", org_id="o1")
        candidate.papers["x1"] = {"id": "x1", "title": "Computer Vision", "year": 2025,
                                  "direction_term_match": True, "matched_terms": ["vision"]}
        details = {"x1": {"id": "x1", "authors": [
            {"name": "A", "org": "Target University", "orgid": "o1"},
            {"name": "B", "org": "华为技术有限公司", "orgid": "o2"},
        ]}}

        class FailingOrgDetailClient:
            def call(self, api, params):
                assert api == "org_detail"
                raise AMinerAPIError(api, 500, "gateway error")

        warnings = []
        enrich_collaboration(FailingOrgDetailClient(), {"p1": candidate}, details, warnings)
        self.assertEqual(candidate.collaboration_types["o2"], "industry")
        self.assertIn("org_detail failed", warnings[0])

    def test_person_detail_failure_keeps_candidate_unverified(self):
        candidate = Candidate(person_id="p1", name="A")

        class FailingPersonDetailClient:
            def call(self, api, params):
                raise AMinerAPIError(api, 503, "unavailable")

        warnings = []
        verify_candidate_roles(FailingPersonDetailClient(), {"p1": candidate}, 1, warnings)
        self.assertTrue(candidate.role_unverified)
        self.assertIn("person_detail failed", warnings[0])

    def test_second_tier_to_985_is_reach(self):
        profile = {
            "undergraduate_institution": "某省属二本院校", "gpa": 82, "gpa_scale": 100,
            "rank_percentile": 30, "research_projects": [{}], "publications": [], "internships": [],
        }
        candidate = Candidate(person_id="p1", name="A", source_schools={"清华大学"})
        candidate.scores = {"applicant_experience_fit": 50.0}
        assign_bands({"p1": candidate}, profile, tiers())
        self.assertEqual(candidate.recommendation_band, "冲刺（启发式）")
        self.assertLess(applicant_readiness(profile), 70)

    def test_profile_expansion_adds_direction_evidenced_lower_tier_school(self):
        profile = {"undergraduate_institution": "某省属二本院校"}
        institutions = [
            {"organization": "Tsinghua University, Beijing, China", "organization_main": "Tsinghua University",
             "organization_aliases": ["清华大学"], "matched_paper_count": 3},
            {"organization": "Example Provincial University, Nanchang, China",
             "organization_main": "Example Provincial University",
             "organization_aliases": ["某省属大学"], "matched_paper_count": 2},
            {"organization": "Example Robotics Company", "matched_paper_count": 4},
        ]
        result = choose_profile_expansion_schools(
            profile, ["清华大学"], institutions, tiers(), limit=3
        )
        self.assertEqual(result, ["Example Provincial University"])

    def test_profile_portfolio_keeps_available_lower_risk_bands_visible(self):
        rows = []
        for index in range(8):
            candidate = Candidate(person_id=f"r{index}", name=f"Reach {index}")
            candidate.recommendation_band = "冲刺（启发式）"
            rows.append(candidate)
        match = Candidate(person_id="m", name="Match")
        match.recommendation_band = "匹配（启发式）"
        safer = Candidate(person_id="s", name="Safer")
        safer.recommendation_band = "相对稳妥（启发式）"
        selected = select_profile_portfolio([*rows, match, safer], 5)
        self.assertIn("m", {row.person_id for row in selected})
        self.assertIn("s", {row.person_id for row in selected})

    def test_obvious_cross_discipline_affiliation_is_filtered(self):
        medical = Candidate(person_id="m", name="A", org="University Hospital")
        computing = Candidate(person_id="c", name="B", org="School of Computer Science")
        warnings = []
        result = filter_discipline_conflicts(
            {"m": medical, "c": computing}, ["computer vision"], False, warnings
        )
        self.assertEqual(set(result), {"c"})
        self.assertIn("filtered 1", warnings[0])

    def test_cross_discipline_filter_can_be_explicitly_disabled(self):
        medical = Candidate(person_id="m", name="A", org="University Hospital")
        result = filter_discipline_conflicts({"m": medical}, ["computer vision"], True, [])
        self.assertEqual(set(result), {"m"})

    def test_cost_guard_estimate(self):
        estimate = estimate_cost("tier", school_count=100, alias_count=3, paper_limit=10, verify_roles=0)
        self.assertGreater(estimate["worst_case_cny"], 5.0)

    def test_cost_estimate_counts_per_school_role_and_collaboration_calls(self):
        estimate = estimate_cost("collaboration", school_count=3, alias_count=1, paper_limit=2, verify_roles=2)
        self.assertEqual(estimate["person_detail_calls"], 6)
        self.assertEqual(estimate["org_detail_calls"], 3)
        discover = estimate_cost("discover", school_count=0, alias_count=2, paper_limit=3, verify_roles=9)
        self.assertEqual(discover["person_detail_calls"], 0)
        self.assertEqual(discover["org_detail_calls"], 0)

    def test_discover_aggregates_distinct_papers_by_organization(self):
        class DiscoverClient:
            def call(self, api, params):
                if api == "paper_search_pro":
                    return {"success": True, "data": [{"id": "p1", "title": "Robot Learning", "year": 2025}]}
                if api == "paper_info":
                    return {"success": True, "data": [{"id": "p1", "title": "Robot Learning", "year": 2025}]}
                if api == "paper_detail":
                    return {"success": True, "data": [{
                        "id": "p1", "title": "Robot Learning", "year": 2025,
                        "authors": [
                            {"name": "A", "org": "Alpha University", "orgid": "o1"},
                            {"name": "B", "org": "Alpha University", "orgid": "o1"},
                            {"name": "C", "org": "Beta University", "orgid": "o2"},
                        ],
                    }]}
                raise AssertionError(api)

        rows = discover_institutions(DiscoverClient(), "robot learning", [], 5, [])
        self.assertEqual([row["organization_id"] for row in rows], ["o1", "o2"])
        self.assertEqual(rows[0]["matched_paper_count"], 1)

    def test_unknown_organization_returns_actionable_warning(self):
        class MissingOrgClient:
            def call(self, api, params):
                self.api = api
                return {"success": True, "data": []}

        warnings = []
        result = recommend_for_school(
            MissingOrgClient(), "不存在大学", "", "vision", [], 5, None, 10, False, 0,
            warnings, {}, False, False,
        )
        self.assertEqual(result, {})
        self.assertIn("official English name or discover mode", warnings[0])


if __name__ == "__main__":
    unittest.main()
