import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from aminer_client import CostLedger  # noqa: E402
from recommend import (  # noqa: E402
    Candidate,
    applicant_readiness,
    assign_bands,
    classify_org,
    consolidate_duplicate_profiles,
    score_candidates,
)


class RecommendationLogicTests(unittest.TestCase):
    def test_cost_ledger(self):
        ledger = CostLedger()
        ledger.add("paper_search_pro")
        ledger.add("paper_detail")
        summary = ledger.summary()
        self.assertEqual(summary["total_calls"], 2)
        self.assertEqual(summary["total_cost_cny"], 0.02)

    def test_org_classification(self):
        self.assertEqual(classify_org("Tsinghua University"), "academic")
        self.assertEqual(classify_org("Microsoft Research Asia"), "industry")
        self.assertEqual(classify_org("Unknown Group"), "unknown")

    def test_missing_collaboration_is_unknown(self):
        candidate = Candidate(
            person_id="p1",
            name="Test Scholar",
            org="Zhejiang University",
            papers={"x": {"id": "x", "title": "Embodied Intelligence", "year": 2026}},
        )
        score_candidates({"p1": candidate}, ["embodied intelligence"], "Zhejiang University", "", None)
        self.assertIsNone(candidate.scores["academic_collaboration"])
        self.assertIsNone(candidate.scores["industry_collaboration"])

    def test_profile_bands_are_labels_not_probabilities(self):
        profile = {
            "gpa": 88,
            "gpa_scale": 100,
            "rank_percentile": 15,
            "research_projects": [{"title": "VLM"}, {"title": "robot learning"}],
            "publications": [],
            "internships": [],
        }
        candidate = Candidate(person_id="p1", name="A")
        candidate.scores = {"applicant_experience_fit": 75.0}
        assign_bands({"p1": candidate}, profile, 3)
        self.assertIn(candidate.recommendation_band, {"冲刺", "匹配", "相对稳妥"})
        self.assertLessEqual(applicant_readiness(profile), 100)

    def test_duplicate_profiles_are_flagged_and_evidence_is_merged(self):
        first = Candidate(person_id="p1", name="Zhang Di", source_schools={"Fudan University"})
        first.papers["a"] = {"id": "a"}
        second = Candidate(person_id="p2", name="Zhang Di", source_schools={"Fudan University"})
        second.papers["b"] = {"id": "b"}
        merged = consolidate_duplicate_profiles({"p1": first, "p2": second})
        self.assertEqual(len(merged), 1)
        candidate = next(iter(merged.values()))
        self.assertTrue(candidate.identity_ambiguous)
        self.assertEqual(set(candidate.papers), {"a", "b"})


if __name__ == "__main__":
    unittest.main()
