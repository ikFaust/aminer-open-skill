import io
import json
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from aminer_client import AMinerAPIError, AMinerClient, CostLedger  # noqa: E402
from recommend import (  # noqa: E402
    CURRENT_YEAR,
    application_timing,
    deepdive_person,
    paper_topic_relevant,
    focus_topic_relevant,
    Candidate,
    ResolvedOrganization,
    applicant_readiness,
    assess_recent_hire,
    assign_bands,
    augment_from_roster,
    backfill_chinese_names,
    build_rank_key,
    classify_org,
    clean_paper,
    choose_profile_expansion_schools,
    should_auto_expand_profile,
    discover_institutions,
    enrich_collaboration,
    enrich_from_portrait,
    estimate_cost,
    estimate_person_cost,
    bounded_paper_ids,
    broad_person_field_recall_terms,
    linked_recent_papers,
    learn_english_school_alias,
    paper_search_org_name,
    remote_sensing_object_detection_title,
    main,
    mentor_followup_plan,
    extract_institution_name,
    filter_by_interest_mismatch,
    filter_discipline_conflicts,
    institution_level,
    industrial_few_shot_defect_paper,
    interest_matches_direction,
    is_mainland_china_institution,
    is_university_affiliation,
    mark_duplicate_names,
    recommend_for_school,
    resolve_organization,
    paper_search,
    resolve_people,
    score_candidates,
    select_profile_portfolio,
    supplement_collaboration_papers,
    school_research_leads,
    school_level_map,
    school_search_terms,
    school_tier_facts,
    title_search_seeds,
    complementary_title_search,
    complementary_abstract_search,
    ranked_abstract_ids,
    task_specific_alternate_leads,
    official_directory_hints,
    official_profile_hints,
    official_seed_leads,
    screen_recent_hires,
    select_representative_papers,
    verify_candidate_roles,
    write_result,
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
    def test_official_seed_leads_are_topic_scoped_navigation(self):
        schools = ["东华大学", "合肥工业大学", "复旦大学"]
        leads = official_seed_leads(schools, "robot learning", ["imitation learning"])
        self.assertEqual({row["name"] for row in leads["东华大学"]}, {"刘华山"})
        self.assertEqual({row["name"] for row in leads["合肥工业大学"]}, {"黄云志"})
        self.assertNotIn("复旦大学", leads)
        self.assertIn("independently verify", leads["东华大学"][0]["scope"])
        self.assertEqual(leads["东华大学"][0]["source_page_published_on"], "2024-05-10")
        self.assertIn("do not assert current faculty", leads["东华大学"][0]["stale_page_warning"])
        self.assertNotIn("stale_page_warning", leads["合肥工业大学"][0])

    def test_embodied_official_seed_exposes_wang_only_as_navigation(self):
        leads = official_seed_leads(["浙江大学"], "embodied intelligence", [])
        wang = next(row for row in leads["浙江大学"] if row["name"] == "王高昂")
        self.assertEqual(wang["url"], "https://person.zju.edu.cn/en/gaoangwang")
        self.assertIn("independently verify", wang["scope"])

    def test_official_profile_hints_require_exact_discovered_identity_and_school(self):
        right = Candidate("p1", "Yunyu Huang", name_zh="黄云志",
                          source_schools={"合肥工业大学"})
        wrong_school = Candidate("p2", "Yunyu Huang", name_zh="黄云志",
                                 source_schools={"上海交通大学"})
        ambiguous = Candidate("p3", "Yunyu Huang", name_zh="黄云志",
                              source_schools={"合肥工业大学"}, identity_ambiguous=True)
        hints = official_profile_hints([right, wrong_school, ambiguous])
        self.assertEqual(len(hints), 1)
        self.assertEqual(hints[0]["name"], "黄云志")
        self.assertIn("re-fetch", hints[0]["scope"])

    def test_thin_school_sample_stays_in_mentor_followup_plan(self):
        schools = ["甲大学", "乙大学", "丙大学", "丁大学", "戊大学", "己大学", "庚大学"]
        counts = [7, 5, 4, 3, 2, 1, 0]
        hits = [90, 80, 70, 60, 50, 35, 75]
        institutions = [
            {"coverage": "bounded_org_id_sample", "matched_paper_count": count,
             "search_hit_count": hit, "inspected_paper_count": 16}
            for count, hit in zip(counts, hits)
        ]
        plan = mentor_followup_plan(schools, institutions)
        self.assertEqual(plan["minimum_thin_sample_probes"], 2)
        self.assertEqual([row["school"] for row in plan["thin_sample_schools"]],
                         ["庚大学", "己大学"])
        self.assertEqual(plan["thin_sample_schools"][0]["matched_paper_count"], 0)

    def test_robot_applicant_prefers_specific_seed_over_bare_lidar(self):
        terms = school_search_terms(
            "robot perception", ["point cloud", "LiDAR", "SLAM", "3D perception"],
            {"target_degree": "phd"})
        self.assertEqual(terms, ["robot perception", "point cloud", "SLAM"])
        self.assertEqual(
            school_search_terms("机器人感知", ["point cloud", "LiDAR", "SLAM"], {"target_degree": "phd"}),
            ["机器人感知", "point cloud", "SLAM"])
        self.assertEqual(
            school_search_terms("atmospheric LiDAR", ["LiDAR", "aerosol"], {"target_degree": "phd"}),
            ["atmospheric LiDAR", "LiDAR", "aerosol"])
        self.assertEqual(
            school_search_terms("robot perception", ["LiDAR"], {"target_degree": "phd"}),
            ["robot perception", "LiDAR"])

    def test_official_directory_seed_is_scoped_and_requires_live_verification(self):
        hints = official_directory_hints(["浙江大学", "清华大学"], "机器人感知")
        self.assertEqual(hints["浙江大学"][0]["url"], "https://www.cse.zju.edu.cn")
        self.assertIn("re-fetch", hints["浙江大学"][0]["scope"])
        self.assertEqual(hints["清华大学"][0]["url"], "https://www.au.tsinghua.edu.cn")
        p3_hints = official_directory_hints(["上海交通大学"], "robot perception")
        self.assertEqual({row["url"] for row in p3_hints["上海交通大学"]},
                         {"https://sais.sjtu.edu.cn/faculty.html",
                          "https://www.cs.sjtu.edu.cn/jiaoshiml.html"})
        self.assertEqual(official_directory_hints(["浙江大学"], "光纤通信"), {})
        p2_hints = official_directory_hints(["复旦大学", "上海交通大学", "苏州大学"], "multimodal")
        self.assertEqual(p2_hints["复旦大学"][0]["url"], "https://cs.fudan.edu.cn/szdw/list.htm")
        self.assertEqual(p2_hints["苏州大学"][0]["url"], "https://scst.suda.edu.cn/11250/list.htm")
        self.assertEqual(p2_hints["上海交通大学"][0]["url"],
                         "https://www.cs.sjtu.edu.cn/jiaoshiml.html")
        medical = official_directory_hints(["东南大学", "哈尔滨工业大学", "南京医科大学"],
                                           "medical image analysis")
        self.assertEqual(set(medical), {"东南大学", "哈尔滨工业大学", "南京医科大学"})
        self.assertTrue(all(row[0]["scope"].startswith("official navigation seed")
                            for row in medical.values()))

    def test_named_applicant_schools_get_exact_national_tier_facts(self):
        tiers = json.loads((Path(__file__).resolve().parents[1] /
                            'references/school-tiers.json').read_text())
        facts = school_tier_facts(['苏州大学', '上海大学', '杭州电子科技大学'], tiers)
        self.assertTrue(facts['苏州大学']['双一流'])
        self.assertTrue(facts['上海大学']['211'])
        self.assertFalse(facts['杭州电子科技大学']['双一流'])
        self.assertFalse(facts['杭州电子科技大学']['211'])

    def test_string_target_cycle_gives_correct_november_exam_timing(self):
        timing = application_timing(date(2026, 11, 4),
                                    {"target_degree": "master", "target_cycle": "2027"})
        self.assertEqual(timing["target_cycle"], 2027)
        self.assertTrue(timing["summer_camp_window_passed"])
        self.assertTrue(timing["registration_window_likely_passed"])
        self.assertTrue(timing["entrance_exam_near"])
        self.assertIn("不足两个月", " ".join(timing["notes"]))

    def test_doctoral_profile_requires_current_official_deadline_without_master_exam_timing(self):
        timing = application_timing(date(2026, 11, 4),
                                    {"target_degree": "phd", "target_cycle": 2027})
        self.assertEqual(timing["degree"], "doctor")
        self.assertTrue(timing["official_deadline_required"])
        self.assertFalse(timing["national_exam_route_only"])
        self.assertIn("2026-11-04", " ".join(timing["notes"]))
        self.assertNotIn("summer_camp_window_passed", timing)

    def test_compound_org_resolves_parent_and_uses_school_id(self):
        client = ScriptedClient({
            "org_disambiguate_pro": [{"一级": "浙江大学", "一级ID": "school-zju",
                                      "二级": "计算机科学与技术学院", "二级ID": "department-cs"}],
            "org_search": [{"org_name": "Zhejiang University", "org_id": "school-zju", "aliases": []}],
            "paper_qa_search": [{"id": "z1", "title": "Robot Learning", "year": 2025}],
        })
        org = resolve_organization(client, "浙江大学计算机科学与技术学院")
        self.assertEqual(org.org_id, "school-zju")
        self.assertEqual(client.calls[1][1]["orgs"], ["浙江大学"])
        self.assertEqual(paper_search(client, "robot learning", org, 10)[0]["id"], "z1")
        params = client.calls[2][1]
        self.assertEqual(params["org_id"], ["school-zju"])
        self.assertEqual(params["query"], "robot learning")
        self.assertNotIn("topic_high", params)
        self.assertNotIn("org_terms", params)

    def test_compound_org_without_parent_never_uses_department_id(self):
        client = ScriptedClient({
            "org_disambiguate_pro": [],
            "org_search": [{"org_name": "浙江大学计算机学院", "org_id": "department-cs", "aliases": []}],
        })
        self.assertIsNone(resolve_organization(client, "浙江大学计算机学院"))
        self.assertEqual(client.calls[-1][1]["orgs"], ["浙江大学"])

    def test_qa_empty_uses_title_query_name_fallback(self):
        client = ScriptedClient({
            "paper_qa_search": [],
            "paper_search_pro": [{"id": "f1", "title": "Computer Vision", "year": 2025}],
        })
        org = ResolvedOrganization("清华大学", "school-thu", "Tsinghua University")
        self.assertEqual(paper_search(client, "computer vision", org, 10)[0]["id"], "f1")
        self.assertEqual([api for api, _ in client.calls], ["paper_qa_search", "paper_search_pro"])
        self.assertEqual(client.calls[-1][1]["query"], "computer vision")
        self.assertEqual(client.calls[-1][1]["org"], "Tsinghua University")

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
        self.assertEqual(classify_org("Tsinghua Univ, Dept Comp Sci"), "academic")
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

    def test_abstract_only_paper_senior_author_survives_lookup_cap(self):
        papers = [{"id": f"p{i}", "title": f"Multimodal LLM System {i}",
                   "year": CURRENT_YEAR - 1} for i in range(4)]
        papers.append({"id": "target", "title": "VideoChat-Flash",
                       "year": CURRENT_YEAR - 1})
        details = {p["id"]: dict(p, authors=[
            {"name": f"Student {p['id']} {i}", "org": "Target University", "orgid": "org-1"}
            for i in range(8)
        ]) for p in papers[:-1]}
        details["target"] = dict(papers[-1],
                                 abstract="A multimodal large language model for videos",
                                 authors=[{"name": "Target Student", "org": "Target University", "orgid": "org-1"},
                                          {"name": "Limin Wang", "org": "Target University", "orgid": "org-1"}])
        client = FakeClient([{"id": "limin", "name": "Limin Wang", "org": "Target University",
                              "org_id": "org-1"}])
        found = resolve_people(client, papers, details,
                               ResolvedOrganization("目标大学", "org-1", "Target University"),
                               ["multimodal large language model", "multimodal llm"], 10, False, [])
        self.assertIn("limin", found)
        self.assertEqual(set(found["limin"].papers), {"target"})

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

    def test_paper_author_id_resolves_same_name_only_after_school_check(self):
        paper = {"id": "p1", "title": "Embodied Navigation", "year": CURRENT_YEAR - 1}
        detail = {"p1": {**paper, "authors": [
            {"name": "Jun Xiao", "id": "right", "org": "Target University", "orgid": "org-1"}]}}
        people = [
            {"id": "wrong", "name": "Jun Xiao", "org": "Target University", "org_id": "org-1"},
            {"id": "right", "name": "Jun Xiao", "org": "Target University", "org_id": "org-1"},
        ]
        found = resolve_people(FakeClient(people), [paper], detail,
                               ResolvedOrganization("目标大学", "org-1", "Target University"),
                               ["navigation"], 10, False, [])
        self.assertEqual(set(found), {"right"})
        self.assertFalse(found["right"].identity_ambiguous)

    def test_department_profile_id_is_retried_when_school_id_finds_namesakes(self):
        paper = {"id": "p1", "title": "Oriented Object Detection", "year": CURRENT_YEAR - 1}
        detail = {"p1": {**paper, "authors": [
            {"name": "Hang Xu", "id": "right", "org": "Target University", "orgid": "department"}]}}
        client = ScriptedClient({"person_search": lambda p: [
            {"id": "wrong", "name": "Hang Xu", "org": "Target University", "org_id": "school"}]
            if "org_id" in p else [
            {"id": "right", "name": "Hang Xu", "org": "School of CS, Target University",
             "org_id": "department"}]})
        found = resolve_people(client, [paper], detail,
                               ResolvedOrganization("目标大学", "school", "Target University"),
                               ["object detection"], 10, False, [])
        self.assertEqual(set(found), {"right"})
        self.assertEqual(len([api for api, _ in client.calls if api == "person_search"]), 2)

    def test_every_candidate_requires_direction_evidence(self):
        candidate = Candidate(person_id="p1", name="A", org="X", org_id="o")
        candidate.papers["x"] = {
            "id": "x", "title": "Computer Vision", "year": 2025, "direction_term_match": True,
            "matched_terms": ["vision"],
        }
        score_candidates({"p1": candidate}, ["vision"], "X", "", None)
        self.assertGreater(candidate.scores["direction_fit"], 0)
        self.assertIsNone(candidate.scores["academic_collaboration"])

    def test_direct_collaboration_work_outranks_large_abstract_only_consortium(self):
        broad = Candidate(person_id="broad", name="Broad", org_id="o")
        broad.papers["p1"] = {"id": "p1", "year": CURRENT_YEAR, "title": "Generic World Model",
                              "direction_term_match": True, "matched_terms": ["embodied"],
                              "topic_evidence": "abstract_only"}
        broad.collaboration_orgs = {f"x{i}": f"University {i}" for i in range(8)}
        direct = Candidate(person_id="direct", name="Direct", org_id="o")
        direct.papers["p2"] = {"id": "p2", "year": CURRENT_YEAR, "title": "Embodied Navigation",
                               "direction_term_match": True, "matched_terms": ["navigation"],
                               "topic_evidence": "title"}
        direct.collaboration_orgs = {"x": "Other University"}
        score_candidates({"broad": broad, "direct": direct}, ["embodied", "navigation"], "X", "", None)
        key = build_rank_key("collaboration", "academic", "overall")
        broad.collaboration_types = {key: "academic" for key in broad.collaboration_orgs}
        direct.collaboration_types = {key: "academic" for key in direct.collaboration_orgs}
        self.assertGreater(key(direct), key(broad))
        broad.n_citation = 5000
        direct.n_citation = 50
        self.assertGreater(build_rank_key("collaboration", "academic", "citation")(broad),
                           build_rank_key("collaboration", "academic", "citation")(direct))

    def test_independent_collaboration_papers_outweigh_one_large_consortium(self):
        consortium = Candidate(person_id="one", name="One")
        repeated = Candidate(person_id="two", name="Two")
        for candidate in (consortium, repeated):
            candidate.papers["p1"] = {"id": "p1", "year": CURRENT_YEAR,
                "title": "Embodied Navigation", "topic_evidence": "title"}
        consortium.collaboration_orgs = {f"u{i}": f"University {i}" for i in range(5)}
        consortium.collaboration_types = {key: "academic" for key in consortium.collaboration_orgs}
        consortium.collaboration_papers = {key: {"p1"} for key in consortium.collaboration_orgs}
        repeated.collaboration_orgs = {"u6": "University 6", "u7": "University 7"}
        repeated.collaboration_types = {key: "academic" for key in repeated.collaboration_orgs}
        repeated.collaboration_papers = {"u6": {"p1"}, "u7": {"p2"}}
        key = build_rank_key("collaboration", "academic", "overall")
        self.assertGreater(key(repeated), key(consortium))
        self.assertEqual(consortium.to_dict()["collaboration_evidence_paper_count"], 1)
        self.assertEqual(repeated.to_dict()["collaboration_evidence_paper_count"], 2)

    def test_profile_shows_hidden_exact_task_scholar_as_unverified_lead(self):
        broad = Candidate(person_id="broad", name="Broad", source_schools={"School A"},
                          interests=["Computer Vision"])
        direct = Candidate(person_id="direct", name="Direct", source_schools={"School A"},
                           interests=["Object Detection", "Oriented Object Detection", "Vision"])
        direct.papers["p"] = {"id": "p", "title": "Oriented Object Detection", "year": CURRENT_YEAR,
                              "aminer_url": "https://www.aminer.cn/pub/p", "topic_evidence": "title"}
        leads = task_specific_alternate_leads([broad, direct], [broad], ["School A"],
                                              ["computer vision", "object detection"])
        self.assertEqual(leads["School A"][0]["person_id"], "direct")
        self.assertIn("official proof", leads["School A"][0]["status"])

    def test_profile_alternates_do_not_promote_generic_deep_learning(self):
        generic = Candidate(person_id="generic", name="Generic", source_schools={"School A"},
                            interests=["Deep Learning", "Vision"])
        direct = Candidate(person_id="direct", name="Direct", source_schools={"School A"},
                           interests=["Object Detection", "Oriented Object Detection"])
        for candidate in (generic, direct):
            candidate.papers["p"] = {"id": "p", "title": "Object Detection", "year": CURRENT_YEAR,
                                     "topic_evidence": "title"}
        leads = task_specific_alternate_leads([generic, direct], [], ["School A"],
                                              ["computer vision", "object detection",
                                               "deep learning", "vision"])
        self.assertEqual([lead["person_id"] for lead in leads["School A"]], ["direct"])

    def test_future_dated_paper_is_not_current_direction_evidence(self):
        row = {"id": "future", "title": "Object Detection", "year": CURRENT_YEAR + 1}
        self.assertIsNone(clean_paper(row, None, ["object detection"]))

    def test_profile_citation_seed_recovers_faculty_missed_by_newest_slice(self):
        papers = {
            "student": {"id": "student", "title": "Object Detection in 2026", "year": CURRENT_YEAR,
                        "authors": [{"name": "Student One", "org": "Alpha University", "orgid": "o"}]},
            "senior": {"id": "senior", "title": "Object Detection in 2023", "year": CURRENT_YEAR - 3,
                       "authors": [{"name": "Professor Two", "org": "Alpha University", "orgid": "o"}]},
        }
        client = ScriptedClient({
            "org_search": [{"org_id": "o", "org_name": "Alpha University", "aliases": []}],
            "paper_qa_search": lambda p: [papers["senior"] if p.get("force_citation_sort") else papers["student"]],
            "paper_search_pro": [],
            "paper_info": lambda p: [papers[i] for i in p["ids"]],
            "paper_detail": lambda p: [papers[p["id"]]],
            "person_search": lambda p: [{"id": "prof-2" if p["name"] == "Professor Two" else "student-1",
                                         "name": p["name"], "org": "Alpha University", "org_id": "o",
                                         "interests": ["Object Detection"]}],
        })
        result = recommend_for_school(
            client, "Alpha University", "", "computer vision", ["object detection", "visual object detection"],
            5, {"research_interests": ["object detection"]}, 10, False, 0, [], {}, False, False,
            citation_seed=True,
        )
        self.assertIn("prof-2", result)
        self.assertIn("senior", result["prof-2"].papers)

    def test_title_recall_adds_school_author_when_qa_already_has_candidates(self):
        self.assertEqual(title_search_seeds(["具身智能", "embodied intelligence", "robot learning"]),
                         ["embodied", "robot"])
        self.assertEqual(title_search_seeds(["computer vision", "object detection",
                                             "oriented object detection", "deep learning"]),
                         ["object detection", "oriented object detection"])
        self.assertEqual(title_search_seeds(["robot learning", "embodied AI",
                                             "embodied intelligence", "manipulation",
                                             "imitation learning", "navigation"]),
                         ["manipulation", "imitation"])
        self.assertEqual(title_search_seeds(["embodied intelligence", "robot learning",
                                             "manipulation", "navigation"], keep_primary_broad=True),
                         ["embodied", "manipulation"])
        self.assertEqual(title_search_seeds(["多模态大模型", "multimodal large language model", "MLLM"])[0],
                         "multimodal llm")
        papers = {
            "qa": {"id": "qa", "title": "Embodied Intelligence Overview", "year": CURRENT_YEAR - 1,
                   "authors": [{"name": "Existing Scholar", "org": "Alpha University", "orgid": "o"}]},
            "title": {"id": "title", "title": "Embodied Intelligence for Robot Navigation",
                      "year": CURRENT_YEAR - 1,
                      "authors": [{"name": "Missed Scholar", "org": "Alpha University", "orgid": "o"}]},
        }
        client = ScriptedClient({
            "org_search": [{"org_id": "o", "org_name": "Alpha University", "aliases": []}],
            "paper_qa_search": [papers["qa"]],
            "paper_search_pro": lambda p: [papers["title"]] if p.get("title") == "embodied" else [],
            "paper_info": lambda p: [papers[i] for i in p["ids"]],
            "paper_detail": lambda p: [papers[p["id"]]],
            "person_search": lambda p: [{"id": "missed" if p["name"] == "Missed Scholar" else "existing",
                                         "name": p["name"], "org": "Alpha University", "org_id": "o"}],
        })
        found = recommend_for_school(client, "Alpha University", "", "embodied intelligence",
                                     ["robot learning"], 5, None, 10, False, 0, [], {}, False, False)
        self.assertEqual(set(found), {"existing", "missed"})
        self.assertEqual(set(found["missed"].papers), {"title"})
        self.assertTrue(any(api == "paper_search_pro" and p.get("title") == "embodied"
                            for api, p in client.calls))

    def test_title_search_does_not_spend_detail_slots_on_off_title_hits(self):
        client = ScriptedClient({"paper_search_pro": [
            {"id": "noise", "title": "Electrochemical sensor", "year": CURRENT_YEAR},
            {"id": "match", "title": "Object Detection in Aerial Images", "year": CURRENT_YEAR},
        ]})
        samples = complementary_title_search(
            client, ResolvedOrganization("杭电", "o", "Hangzhou Dianzi University"),
            ["object detection"], [])
        self.assertEqual([[row["id"] for row in sample] for sample in samples],
                         [["match"], ["match"]])

    def test_multimodal_llm_title_alias_recalls_anygpt_shape(self):
        paper = {"id": "any", "title": "AnyGPT: Unified Multimodal LLM with Discrete Sequence Modeling",
                 "year": CURRENT_YEAR - 2}
        client = ScriptedClient({"paper_search_pro": lambda params: [paper]
                                 if params.get("title") == "multimodal llm" else []})
        samples = complementary_title_search(
            client, ResolvedOrganization("复旦大学", "school", "Fudan University"),
            ["多模态大模型", "multimodal large language model", "MLLM"], [])
        self.assertIn("any", [row["id"] for sample in samples for row in sample])
        self.assertTrue(any(params.get("title") == "multimodal llm"
                            for api, params in client.calls if api == "paper_search_pro"))

    def test_abstract_search_recalls_named_model_paper_without_field_in_title(self):
        paper = {"id": "model", "title": "VideoChat-Flash: Hierarchical Video Token Compression",
                 "year": CURRENT_YEAR - 1,
                 "authors": [{"name": "Limin Wang", "org": "Nanjing University", "orgid": "o"}]}
        detail = dict(paper, abstract="A multimodal large language model for long video understanding")
        client = ScriptedClient({
            "org_search": [{"org_id": "o", "org_name": "Nanjing University", "aliases": []}],
            "paper_qa_search": [],
            "paper_search_pro": lambda p: [paper] if p.get("abstract") == "multimodal large language model" else [],
            "paper_info": lambda p: [paper],
            "paper_detail": lambda p: [detail],
            "person_search": lambda p: [{"id": "limin", "name": "Limin Wang",
                                         "org": "Nanjing University", "org_id": "o",
                                         "interests": ["multimodal large language model"]}],
        })
        found = recommend_for_school(client, "Nanjing University", "", "multimodal large language model",
                                     ["MLLM"], 5, None, 10, False, 0, [], {}, False, False)
        self.assertIn("limin", found)
        self.assertIn("model", found["limin"].papers)
        self.assertTrue(any(api == "paper_search_pro" and p.get("abstract")
                            for api, p in client.calls))

    def test_abstract_sample_keeps_influential_and_recent_named_models(self):
        cited = [{"id": f"c{i}"} for i in range(20)]
        recent = [{"id": f"r{i}"} for i in range(10)]
        selected = ranked_abstract_ids([cited, recent], 24)
        self.assertIn("c13", selected)
        self.assertIn("r0", selected)

    def test_collaboration_broad_keyword_recalls_paper_without_field_in_title(self):
        client = ScriptedClient({"paper_search_pro": lambda p: [
            {"id": "agent-vln", "title": "AgentVLN: Vision-and-Language Navigation",
             "year": CURRENT_YEAR}]
            if p.get("keyword") == "embodied" else []})
        samples = complementary_title_search(
            client, ResolvedOrganization("浙大", "o", "Zhejiang University"),
            ["embodied intelligence", "manipulation"], [], keep_primary_broad=True)
        self.assertIn("agent-vln", [row["id"] for sample in samples for row in sample])
        self.assertTrue(any(p.get("keyword") == "embodied" for api, p in client.calls
                            if api == "paper_search_pro"))
        steve = {"id": "steve", "title": "See and Think: Embodied Agent in Virtual Environment",
                 "year": CURRENT_YEAR - 2}
        detail = dict(steve, keywords=["Open-world embodied agent"])
        self.assertIsNotNone(clean_paper(steve, detail,
                                         ["embodied intelligence", "embodied agent"]))

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

    def test_recent_hire_needs_positive_evidence(self):
        # 张静式 bad case：资深教授、无任何入职信息 → 不得断言为新引进
        senior = {"position_zh": "教授", "bio_zh": "长期从事知识图谱研究。", "edu_zh": "2005年 清华大学 博士"}
        self.assertEqual(assess_recent_hire(senior, 2020)["status"], "no_hire_evidence")
        # bio 明确写了窗口内入职年份
        joined = {"position_zh": "教授", "bio_zh": "2023年9月加入中国人民大学信息学院。"}
        self.assertEqual(assess_recent_hire(joined, 2020)["status"], "likely_recent")
        # bio 写的入职年份在窗口之前 → 明确排除
        earlier = {"position_zh": "副教授", "bio_zh": "2018年入职本校。"}
        result = assess_recent_hire(earlier, 2020)
        self.assertEqual(result["status"], "hired_earlier")
        self.assertIn("2018", result["signals"][0])
        # 博士新毕业 → 正信号
        fresh = {"position_zh": "", "edu_zh": "2024年 浙江大学获得计算机博士学位"}
        self.assertEqual(assess_recent_hire(fresh, 2020)["status"], "likely_recent")
        # 初级职称 → 正信号
        junior = {"position_zh": "助理教授", "edu_zh": ""}
        self.assertEqual(assess_recent_hire(junior, 2020)["status"], "likely_recent")

    def test_recent_hire_screening_excludes_students_and_unevidenced(self):
        junior = Candidate(person_id="a", name="Junior Prof", role="助理教授")
        junior.hire_assessment = {"status": "likely_recent", "signals": ["junior position: 助理教授"]}
        phd = Candidate(person_id="b", name="Phd Student", role="博士生")
        phd.hire_assessment = {"status": "likely_recent", "signals": ["latest degree year 2027"]}
        senior = Candidate(person_id="c", name="Senior Prof", role="教授")
        senior.hire_assessment = {"status": "no_hire_evidence", "signals": []}
        earlier = Candidate(person_id="d", name="Old Hire", role="副教授")
        earlier.hire_assessment = {"status": "hired_earlier", "signals": ["bio states joining in 2018, before 2020"]}
        pool, screening = screen_recent_hires([junior, phd, senior, earlier], 2020)
        self.assertEqual([c.person_id for c in pool], ["a"])
        self.assertEqual(screening["excluded_non_faculty"][0]["name"], "Phd Student")
        self.assertEqual(screening["no_hire_evidence"][0]["name"], "Senior Prof")
        self.assertEqual(screening["excluded_hired_earlier"][0]["name"], "Old Hire")

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

    def test_collaboration_partners_exclude_home_and_link_their_papers(self):
        candidate = Candidate(person_id="p1", name="A", org_id="department")
        candidate.papers["p"] = {"id": "p", "title": "Embodied Navigation", "year": CURRENT_YEAR,
                                 "direction_term_match": True, "matched_terms": ["navigation"]}
        details = {"p": {"id": "p", "authors": [
            {"name": "A", "org": "Zhejiang University", "orgid": "school"},
            {"name": "B", "org": "Zhejiang University", "orgid": "school"},
            {"name": "C", "org": "Peking University", "orgid": "school"},
        ]}}

        class Client:
            def call(self, api, params):
                return {"success": True, "code": 200, "data": []}

        enrich_collaboration(Client(), {"p1": candidate}, details, [],
                             ResolvedOrganization("浙江大学", "school", "Zhejiang University"))
        self.assertEqual(candidate.collaboration_orgs, {"school": "Peking University"})
        self.assertEqual(candidate.to_dict()["collaboration_organizations"][0]["paper_urls"],
                         ["https://www.aminer.cn/pub/p"])

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

    def test_profile_expansion_respects_explicit_school_boundary(self):
        self.assertFalse(should_auto_expand_profile("profile", True, False, False))
        self.assertTrue(should_auto_expand_profile("profile", False, False, False))
        self.assertTrue(should_auto_expand_profile("profile", True, False, True))
        self.assertFalse(should_auto_expand_profile("profile", False, True, False))
        self.assertFalse(should_auto_expand_profile("named", False, False, True))

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

    def test_named_school_profile_keeps_one_lead_from_each_available_school(self):
        rows = []
        for index, school in enumerate(["清华大学", "清华大学", "清华大学", "浙江大学", "上海交通大学"]):
            row = Candidate(person_id=f"p{index}", name=f"Person {index}", source_schools={school})
            row.recommendation_band = "冲刺（启发式）"
            rows.append(row)
        selected = select_profile_portfolio(rows, 3, ["清华大学", "浙江大学", "上海交通大学"])
        self.assertEqual({s for row in selected for s in row.source_schools},
                         {"清华大学", "浙江大学", "上海交通大学"})
        self.assertEqual(len(selected), 3)

    def test_school_research_leads_exposes_alternatives_hidden_by_portfolio_limit(self):
        rows = [Candidate(person_id=f"a{i}", name=f"A {i}", source_schools={"A校"})
                for i in range(4)]
        rows.append(Candidate(person_id="b1", name="B 1", source_schools={"B校"}))
        rows[1].papers["paper"] = {"title": "Task paper", "year": CURRENT_YEAR,
                                  "direction_term_match": True}
        visible = select_profile_portfolio(rows, 2, ["A校", "B校"])
        self.assertEqual(len(visible), 2)
        leads = school_research_leads(rows, ["A校", "B校", "C校"])
        self.assertEqual([row["person_id"] for row in leads["A校"]], ["a0", "a1", "a2", "a3"])
        self.assertEqual(leads["A校"][1]["evidence_paper"]["title"], "Task paper")
        self.assertEqual([row["person_id"] for row in leads["B校"]], ["b1"])
        self.assertEqual(leads["C校"], [])
        self.assertTrue(all("verify current faculty" in row["status"]
                            for school_rows in leads.values() for row in school_rows))

    def test_school_research_leads_prefers_different_papers(self):
        rows = [Candidate(person_id=f"p{i}", name=f"Person {i}", source_schools={"A校"})
                for i in range(4)]
        for index, paper_id in enumerate(("same", "same", "other", "third")):
            rows[index].papers[paper_id] = {"id": paper_id, "title": paper_id,
                                            "year": CURRENT_YEAR, "direction_term_match": True}
        leads = school_research_leads(rows, ["A校"])["A校"]
        self.assertEqual([row["person_id"] for row in leads], ["p0", "p2", "p3", "p1"])
        self.assertEqual(set(leads[0]["evidence_paper"]), {"id", "title", "year", "aminer_url"})

    def test_school_leads_surface_high_citation_field_author_below_top_three(self):
        rows = [Candidate(person_id=f"p{i}", name=f"Person {i}", source_schools={"A校"},
                          n_citation=100 + i) for i in range(8)]
        for i, row in enumerate(rows):
            row.papers[f"paper{i}"] = {"id": f"paper{i}", "title": f"Multimodal LLM {i}",
                                       "year": CURRENT_YEAR, "direction_term_match": True,
                                       "topic_evidence": "title"}
        rows[7].n_citation = 39181
        leads = school_research_leads(rows, ["A校"])["A校"]
        self.assertIn("p7", [row["person_id"] for row in leads])
        self.assertEqual(len(leads), 5)
        self.assertIn("research lead only", next(row for row in leads if row["person_id"] == "p7")["status"])

    def test_obvious_cross_discipline_affiliation_is_filtered(self):
        medical = Candidate(person_id="m", name="A", org="University Hospital")
        computing = Candidate(person_id="c", name="B", org="School of Computer Science")
        warnings = []
        result = filter_discipline_conflicts(
            {"m": medical, "c": computing}, ["computer vision"], False, warnings
        )
        self.assertEqual(set(result), {"c"})
        self.assertIn("filtered 1", warnings[0])

    def test_joint_off_field_paper_affiliation_does_not_delete_computing_faculty(self):
        faculty = Candidate(person_id="f", name="Researcher", org_zh="上海交通大学人工智能学院")
        faculty.author_affiliations = {"Shanghai Jiao Tong University School of Medicine"}
        result = filter_discipline_conflicts({"f": faculty}, ["multimodal large language model"], False, [])
        self.assertEqual(set(result), {"f"})

    def test_school_only_profile_with_exclusively_hospital_papers_remains_excluded(self):
        medical = Candidate(person_id="m", name="Researcher", org="Shanghai Jiao Tong University")
        medical.author_affiliations = {"Shanghai Jiao Tong University Hospital"}
        result = filter_discipline_conflicts({"m": medical}, ["multimodal large language model"], False, [])
        self.assertEqual(result, {})

    def test_cross_discipline_filter_can_be_explicitly_disabled(self):
        medical = Candidate(person_id="m", name="A", org="University Hospital")
        result = filter_discipline_conflicts({"m": medical}, ["computer vision"], True, [])
        self.assertEqual(set(result), {"m"})

    def test_university_collaboration_is_distinct_from_research_institute(self):
        self.assertTrue(is_university_affiliation("Hong Kong Baptist University, Department of Computer Science"))
        self.assertTrue(is_university_affiliation("Tsinghua Univ, Dept Comp Sci"))
        self.assertTrue(is_university_affiliation("北京大学计算机学院"))
        self.assertTrue(is_university_affiliation("Massachusetts Institute of Technology"))
        self.assertFalse(is_university_affiliation("Shenzhen Research Institute of Big Data"))
        candidate = Candidate(person_id="p1", name="A")
        candidate.collaboration_orgs = {"u": "Hong Kong Baptist University", "r": "Shenzhen Research Institute of Big Data"}
        candidate.collaboration_types = {"u": "academic", "r": "academic"}
        rows = candidate.to_dict()["collaboration_organizations"]
        self.assertEqual([row["is_university"] for row in rows], [True, False])

    def test_cost_guard_estimate(self):
        estimate = estimate_cost("tier", school_count=100, alias_count=3, paper_limit=10, verify_roles=0)
        self.assertGreater(estimate["worst_case_cny"], 5.0)

    def test_saved_discovery_still_returns_evidence_to_tool_caller(self):
        result = {"workflow": "discover", "institutions": [{"organization": "School A"}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            visible = io.StringIO()
            with redirect_stdout(visible):
                write_result(result, path)
            self.assertEqual(json.loads(visible.getvalue())["institutions"][0]["organization"],
                             "School A")
            self.assertEqual(json.loads(path.read_text())["institutions"], result["institutions"])

    def test_collaboration_stdout_keeps_leads_and_compacts_partner_portfolio(self):
        organizations = [{"id": f"o{i}", "name": f"University {i}", "type": "academic",
                          "is_university": True, "paper_urls": [f"https://www.aminer.cn/pub/p{i}"]}
                         for i in range(12)]
        result = {"workflow": "collaboration", "official_seed_leads": {"浙江大学": [{"name": "王高昂"}]},
                  "alternate_collaboration_leads": [{"name": "Other"}],
                  "candidates": [{"name": "A", "collaboration_organizations": organizations,
                                  "evidence_papers": [{"id": f"p{i}", "title": f"Paper {i}",
                                                       "year": 2025} for i in range(5)]}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            visible = io.StringIO()
            with redirect_stdout(visible):
                write_result(result, path)
            compact = json.loads(visible.getvalue())
            candidate = compact["candidates"][0]
            self.assertEqual(compact["official_seed_leads"]["浙江大学"][0]["name"], "王高昂")
            self.assertLess(visible.getvalue().find('"alternate_collaboration_leads"'),
                            visible.getvalue().find('"candidates"'))
            self.assertEqual(candidate["collaboration_organization_count"], 12)
            self.assertEqual(candidate["collaboration_organizations_omitted"], 8)
            self.assertEqual(candidate["collaboration_organizations"][0]["paper_urls"],
                             ["https://www.aminer.cn/pub/p0"])
            self.assertEqual(len(candidate["evidence_papers"]), 2)
            self.assertEqual(len(json.loads(path.read_text())["candidates"][0]["collaboration_organizations"]), 12)

    def test_professor_title_does_not_verify_supervisor_eligibility(self):
        candidate = Candidate(person_id="p1", name="Prof X", role="Professor", role_unverified=False)
        self.assertIn("graduate-supervisor eligibility for the requested degree",
                      candidate.to_dict()["needs_verification"])

    def test_cost_estimate_counts_per_school_role_and_collaboration_calls(self):
        estimate = estimate_cost("collaboration", school_count=3, alias_count=1, paper_limit=2, verify_roles=2)
        self.assertEqual(estimate["person_detail_calls"], 6)
        self.assertEqual(estimate["org_detail_calls"], 3)
        self.assertEqual(estimate["paper_qa_search_calls"], 6)  # recent plus citation seed
        self.assertEqual(estimate["paper_detail_calls"], 729)  # initial, fallback, aliases, author branch
        self.assertGreaterEqual(estimate["paper_search_pro_calls"], 15)  # title and abstract passes
        discover = estimate_cost("discover", school_count=0, alias_count=2, paper_limit=3, verify_roles=9)
        self.assertEqual(discover["person_detail_calls"], 0)
        self.assertEqual(discover["org_detail_calls"], 0)
        compared = estimate_cost("discover", school_count=2, alias_count=3, paper_limit=8, verify_roles=0)
        self.assertEqual(compared["paper_qa_search_calls"], 14)

    def test_explicit_school_comparison_does_not_hide_paid_schools_at_candidate_cap(self):
        class EmptyLedger:
            def summary(self):
                return {"total_cost_cny": 0}

        class EmptyClient:
            cost = EmptyLedger()

        schools = [f"School{index}" for index in range(12)]
        rows = [{"organization": school, "matched_paper_count": 0} for school in schools]
        argv = ["recommend.py", "--mode", "discover", "--direction", "medical image analysis",
                "--schools", ",".join(schools), "--max-schools", "12",
                "--candidate-limit", "10", "--yes"]
        with patch.object(sys, "argv", argv), \
             patch("recommend.AMinerClient", return_value=EmptyClient()), \
             patch("recommend.discover_institutions", return_value=rows), \
             patch("recommend.write_result") as write:
            main()
        self.assertEqual([row["organization"] for row in write.call_args.args[0]["institutions"]], schools)

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

    def test_discover_explicit_schools_uses_same_org_scoped_query_and_affiliation_check(self):
        client = ScriptedClient({
            "org_search": lambda p: [{"org_id": "a" if p["orgs"][0] == "Alpha University" else "b",
                                      "org_name": p["orgs"][0], "aliases": []}],
            "paper_qa_search": lambda p: [{"id": p["org_id"][0] + "1", "title": "Robot Learning", "year": 2025}],
            "paper_search_pro": [],
            "paper_info": lambda p: [{"id": p["ids"][0], "title": "Robot Learning", "year": 2025}],
            "paper_detail": lambda p: [{"id": p["id"], "title": "Robot Learning", "year": 2025,
                                         "authors": [{"name": "X", "org": "Alpha University", "orgid": "a"}]}],
        })
        rows = discover_institutions(client, "robot learning", [], 5, [],
                                     ["Alpha University", "Beta University"])
        self.assertEqual([(r["organization_id"], r["matched_paper_count"]) for r in rows],
                         [("a", 1), ("b", 0)])
        qa = [params for api, params in client.calls if api == "paper_qa_search"]
        self.assertEqual([p["org_id"] for p in qa], [["a"], ["a"], ["b"], ["b"]])
        self.assertEqual([bool(p.get("force_citation_sort")) for p in qa], [False, True, False, True])
        self.assertTrue(all(p["query"] == "robot learning" for p in qa))
        self.assertTrue(all(r["coverage"] == "bounded_org_id_sample" for r in rows))

    def test_discover_keeps_requested_school_label_when_aminer_uses_historical_name(self):
        client = ScriptedClient({
            "org_search": [{"org_id": "navy", "org_name": "Historical Military Medical College",
                            "aliases": ["Second Military Medical University"]}],
            "org_disambiguate_pro": [{"一级": "海军军医大学", "一级ID": "navy"}],
            "paper_qa_search": [], "paper_search_pro": [],
        })
        row = discover_institutions(client, "robot learning", [], 5, [], ["海军军医大学"])[0]
        self.assertEqual(row["organization"], "海军军医大学")
        self.assertEqual(row["aminer_organization_name"], "Historical Military Medical College")
        self.assertEqual(row["organization_id"], "navy")

    def test_discover_rejects_org_id_that_conflicts_with_author_affiliation(self):
        client = ScriptedClient({
            "org_search": [{"org_id": "a", "org_name": "Alpha University", "aliases": []}],
            "paper_qa_search": [{"id": "p1", "title": "Robot Learning", "year": 2025}],
            "paper_search_pro": [],
            "paper_info": [{"id": "p1", "title": "Robot Learning", "year": 2025}],
            "paper_detail": [{"id": "p1", "title": "Robot Learning", "year": 2025,
                              "authors": [{"name": "X", "org": "Beta University", "orgid": "a"}]}],
        })
        rows = discover_institutions(client, "robot learning", [], 5, [], ["Alpha University"])
        self.assertEqual(rows[0]["matched_paper_count"], 0)
        self.assertEqual(rows[0]["affiliation_conflicts"], 1)

    def test_discover_medical_image_abstract_recalls_school_paper_and_checks_affiliation(self):
        papers = {
            "fit": {"id": "fit", "title": "MTANet: Multi-task Attention Network",
                    "year": CURRENT_YEAR - 2, "keywords": ["Medical Image Segmentation"],
                    "authors": [{"name": "A", "org": "Alpha University", "orgid": "a"}]},
            "wrong_school": {"id": "wrong_school", "title": "SAM Evaluation for Clinical Scans",
                             "year": CURRENT_YEAR - 1, "keywords": ["Medical Image Analysis"],
                             "authors": [{"name": "B", "org": "Beta University", "orgid": "a"}]},
        }
        client = ScriptedClient({
            "org_search": [{"org_id": "a", "org_name": "Alpha University", "aliases": []}],
            "paper_qa_search": [],
            "paper_search_pro": lambda p: [
                {key: papers[paper_id][key] for key in ("id", "title", "year")}
                for paper_id in ("fit", "wrong_school")
            ] if p.get("abstract") == "medical image" and p.get("org") == "Alpha University" else [],
            "paper_info": lambda p: [papers[pid] for pid in p["ids"]],
            "paper_detail": lambda p: [papers[p["id"]]],
        })
        rows = discover_institutions(client, "medical image analysis",
                                     ["medical image segmentation", "medical imaging"],
                                     5, [], ["Alpha University"])
        self.assertEqual(rows[0]["matched_paper_count"], 1)
        self.assertIn("fit", {paper["id"] for paper in
                              rows[0]["evidence_papers"] + rows[0]["representative_papers"]})
        self.assertEqual(rows[0]["affiliation_conflicts"], 1)
        self.assertTrue(any(api == "paper_search_pro" and p.get("abstract") == "medical image"
                            for api, p in client.calls))

    def test_medical_segmentation_school_count_excludes_other_image_tasks(self):
        papers = {
            "segment": {"id": "segment", "title": "Brain Tumor Segmentation in MRI",
                        "year": CURRENT_YEAR - 1, "keywords": ["medical image segmentation"]},
            "classify": {"id": "classify", "title": "Medical Image Classification for Brain Tumors",
                         "year": CURRENT_YEAR - 1, "keywords": ["medical image analysis"]},
            "aerial": {"id": "aerial", "title": "Aerial Image Segmentation",
                       "year": CURRENT_YEAR - 1, "keywords": ["segmentation"]},
            "crop": {"id": "crop", "title": "Point-Cloud Crop Organ Segmentation",
                     "year": CURRENT_YEAR - 1, "keywords": ["plant phenotyping", "crop organ segmentation"]},
            "quantify": {"id": "quantify", "title": "Clinical MRI Fat Quantification",
                         "year": CURRENT_YEAR - 1, "keywords": ["medical image segmentation"]},
        }
        for paper in papers.values():
            paper["authors"] = [{"name": "A", "org": "Alpha University", "orgid": "a"}]
        client = ScriptedClient({
            "org_search": [{"org_id": "a", "org_name": "Alpha University", "aliases": []}],
            "paper_qa_search": [{key: paper[key] for key in ("id", "title", "year")}
                                for paper in papers.values()],
            "paper_search_pro": [],
            "paper_info": lambda p: [papers[pid] for pid in p["ids"]],
            "paper_detail": lambda p: [papers[p["id"]]],
        })
        row = discover_institutions(client, "medical image segmentation",
                                    ["segmentation", "medical image analysis"],
                                    5, [], ["Alpha University"])[0]
        self.assertEqual(row["matched_paper_count"], 1)
        self.assertEqual(row["adjacent_topic_paper_count"], 4)
        self.assertEqual(row["representative_papers"][0]["id"], "segment")

    def test_compound_few_shot_defect_count_excludes_one_sided_papers(self):
        papers = {
            "direct": {"id": "direct", "title": "Few-shot Surface Defect Detection for Manufacturing",
                       "year": CURRENT_YEAR - 1},
            "few_data": {"id": "few_data", "title": "Surface Defect Detection with Limited Data in Manufacturing",
                         "year": CURRENT_YEAR - 1},
            "zero_shot": {"id": "zero_shot", "title": "Zero-Shot Industrial Anomaly Detection",
                          "year": CURRENT_YEAR - 1},
            "fault": {"id": "fault", "title": "Few-shot Fault Diagnosis of Rotating Machinery",
                      "year": CURRENT_YEAR - 1, "keywords": ["few-shot learning"]},
        }
        for paper in papers.values():
            paper["authors"] = [{"name": "A", "org": "Alpha University", "orgid": "a"}]
        self.assertTrue(industrial_few_shot_defect_paper(papers["direct"], papers["direct"]))
        self.assertTrue(industrial_few_shot_defect_paper(papers["few_data"], papers["few_data"]))
        self.assertFalse(industrial_few_shot_defect_paper(papers["zero_shot"], papers["zero_shot"]))
        self.assertFalse(industrial_few_shot_defect_paper(papers["fault"], papers["fault"]))
        client = ScriptedClient({
            "org_search": [{"org_id": "a", "org_name": "Alpha University", "aliases": []}],
            "paper_qa_search": [{k: p[k] for k in ("id", "title", "year")} for p in papers.values()],
            "paper_search_pro": [],
            "paper_info": lambda p: [papers[pid] for pid in p["ids"]],
            "paper_detail": lambda p: [papers[p["id"]]],
        })
        row = discover_institutions(
            client, "industrial defect detection", ["surface defect detection", "few-shot learning",
                                                    "industrial anomaly detection"],
            5, [], ["Alpha University"])[0]
        self.assertEqual(row["matched_paper_count"], 2)
        self.assertEqual(row["adjacent_topic_paper_count"], 2)
        self.assertEqual({p["id"] for p in row["representative_papers"] + row["evidence_papers"]},
                         {"direct", "few_data"})
        self.assertIn("AND", row["compound_focus"])

    def test_discover_title_view_recalls_direct_code_paper_qa_missed(self):
        paper = {"id": "class-eval", "title": "Evaluating Large Language Models in Class-Level Code Generation",
                 "year": CURRENT_YEAR - 2,
                 "authors": [{"name": "Researcher", "org": "Alpha University", "orgid": "a"}]}
        client = ScriptedClient({
            "org_search": [{"org_id": "a", "org_name": "Alpha University", "aliases": []}],
            "paper_qa_search": [],
            "paper_search_pro": lambda p: [{k: paper[k] for k in ("id", "title", "year")}]
                                if p.get("title") == "code generation" else [],
            "paper_info": lambda p: [paper],
            "paper_detail": lambda p: [paper],
        })
        row = discover_institutions(client, "code generation", ["program synthesis"],
                                    5, [], ["Alpha University"])[0]
        self.assertEqual(row["matched_paper_count"], 1)
        self.assertEqual(row["representative_papers"][0]["id"], "class-eval")
        self.assertTrue(any(api == "paper_search_pro" and params.get("title") == "code generation"
                            for api, params in client.calls))

    def test_sparse_title_view_does_not_shrink_discover_inspection_cap(self):
        papers = {f"qa-{i}": {"id": f"qa-{i}", "title": f"Code Generation Method {i}",
                              "year": CURRENT_YEAR - 1,
                              "authors": [{"name": "Researcher", "org": "Alpha University", "orgid": "a"}]}
                  for i in range(35)}
        papers["title"] = {"id": "title", "title": "Code Generation by Planning",
                           "year": CURRENT_YEAR - 1,
                           "authors": [{"name": "Researcher", "org": "Alpha University", "orgid": "a"}]}
        client = ScriptedClient({
            "org_search": [{"org_id": "a", "org_name": "Alpha University", "aliases": []}],
            "paper_qa_search": [papers[f"qa-{i}"] for i in range(35)],
            "paper_search_pro": lambda p: [papers["title"]] if p.get("title") == "code generation" else [],
            "paper_info": lambda p: [papers[pid] for pid in p["ids"]],
            "paper_detail": lambda p: [papers[p["id"]]],
        })
        row = discover_institutions(client, "code generation", [], 40, [], ["Alpha University"])[0]
        self.assertEqual(row["inspected_paper_count"], 36)
        self.assertEqual(row["matched_paper_count"], 36)

    def test_discover_medical_keyword_recalls_flagship_and_keeps_totals_separate(self):
        paper = {"id": "samed", "title": "SAMed: Segment Anything in Medical Images",
                 "year": CURRENT_YEAR - 2, "keywords": ["Medical Image Segmentation"],
                 "authors": [{"name": "A", "org": "Alpha University", "orgid": "a"}]}
        generic = {"id": "llava", "title": "LLaVA-UHD: an LMM Perceiving Any Aspect Ratio",
                   "year": CURRENT_YEAR - 2, "keywords": ["Medical Image Analysis"],
                   "authors": [{"name": "B", "org": "Alpha University", "orgid": "a"}]}
        papers = {"samed": paper, "llava": generic}

        class TotalsClient(ScriptedClient):
            def call(self, api, params):
                payload = super().call(api, params)
                if api == "paper_search_pro" and params.get("keyword") == "medical image":
                    payload["total"] = 331
                if api == "paper_qa_search" and params.get("topic_high"):
                    payload["total"] = 74
                return payload

        client = TotalsClient({
            "org_search": [{"org_id": "a", "org_name": "Alpha University", "aliases": []}],
            "paper_qa_search": [],
            "paper_search_pro": lambda p: [{k: row[k] for k in ("id", "title", "year")}
                                           for row in papers.values()]
                                if p.get("keyword") == "medical image" else [],
            "paper_info": lambda p: [papers[pid] for pid in p["ids"]],
            "paper_detail": lambda p: [papers[p["id"]]],
        })
        row = discover_institutions(client, "medical image analysis", [], 5, [],
                                    ["Alpha University"])[0]
        self.assertEqual(row["matched_paper_count"], 1)
        self.assertEqual(row["keyword_total_hits"], 331)
        self.assertEqual(row["recent_topic_total_hits"], 74)
        self.assertIn("samed", {p["id"] for p in row["representative_papers"]})
        self.assertEqual(row["adjacent_topic_paper_count"], 1)

    def test_discover_recovers_cited_flagship_and_does_not_count_abstract_only_hit(self):
        papers = {
            "recent": {"id": "recent", "title": "Multimodal large language model benchmark", "year": CURRENT_YEAR},
            "flagship": {"id": "flagship", "title": "CogVLM: Visual Expert for Pretrained Language Models",
                         "year": CURRENT_YEAR - 2, "n_citation_bucket": "1001-5000"},
            "incidental": {"id": "incidental", "title": "Sentiment analysis in social media",
                           "year": CURRENT_YEAR, "abstract_slice": "A multimodal large language model is cited in related work."},
            "hardware": {"id": "hardware", "title": "Multimodal optoelectronic memory arrays for image processing",
                         "year": CURRENT_YEAR - 1},
            "language_only": {"id": "language_only", "title": "Expert specialization in language models",
                              "year": CURRENT_YEAR - 2, "n_citation_bucket": "1001-5000"},
        }
        client = ScriptedClient({
            "org_search": [{"org_id": "a", "org_name": "Alpha University", "aliases": []}],
            "paper_qa_search": lambda p: [papers["flagship"], papers["language_only"]] if p.get("force_citation_sort")
                               else [papers["recent"], papers["incidental"], papers["hardware"]],
            "paper_search_pro": [],
            "paper_info": lambda p: [papers[pid] for pid in p["ids"]],
            "paper_detail": lambda p: [{**papers[p["id"]],
                "keywords": ["Multimodal Learning", "Large Language Model"] if p["id"] == "flagship"
                            else ["Multimodal Learning"] if p["id"] == "language_only" else [],
                "authors": [{"name": "A", "org": "Alpha University", "orgid": "a"}]}],
        })
        rows = discover_institutions(client, "multimodal large language model",
                                     ["vision-language model"], 5, [], ["Alpha University"])
        row = rows[0]
        self.assertEqual({p["id"] for p in row["evidence_papers"] + row["representative_papers"]},
                         {"recent", "flagship"})
        self.assertEqual(row["flagship_paper_count"], 1)
        self.assertEqual(row["abstract_only_paper_count"], 1)
        self.assertEqual(row["adjacent_topic_paper_count"], 2)
        self.assertEqual(row["representative_papers"][0]["id"], "flagship")

    def test_large_school_comparison_keeps_all_counts_but_bounds_display_evidence(self):
        schools = [f"Example {i} University" for i in range(9)]
        client = ScriptedClient({
            "org_search": lambda p: [{"org_id": p["orgs"][0], "org_name": p["orgs"][0], "aliases": []}],
            "paper_qa_search": lambda p: [
                {"id": f'{p["org_id"][0]}-{i}', "title": f"Robot Learning Study {i}",
                 "year": CURRENT_YEAR - i, "n_citation_bucket": "201-1000"}
                for i in range(4)
            ],
            "paper_search_pro": [],
            "paper_info": lambda p: [{"id": pid, "title": "Robot Learning Study", "year": CURRENT_YEAR}
                                      for pid in p["ids"]],
            "paper_detail": lambda p: [{"id": p["id"], "title": "Robot Learning Study " + p["id"].rsplit("-", 1)[1],
                                         "year": CURRENT_YEAR,
                                         "authors": [{"name": "A", "org": p["id"].rsplit("-", 1)[0]}]}],
        })
        rows = discover_institutions(client, "robot learning", [], 5, [], schools)
        self.assertEqual(len(rows), 9)
        self.assertTrue(all(row["matched_paper_count"] == 4 for row in rows))
        self.assertTrue(all(len(row["representative_papers"]) == 2 and len(row["evidence_papers"]) == 1
                            for row in rows))

    def test_citation_sample_prioritizes_direct_multimodal_work_before_generic_llm_hits(self):
        def qa(params):
            query = params["query"]
            if params.get("force_citation_sort"):
                return [
                    {"id": query + "-generic", "title": "Expert Specialization in Language Models",
                     "year": CURRENT_YEAR - 2, "n_citation_bucket": "1001-5000"},
                    {"id": query + "-flagship", "title": "CogVLM: Visual Expert for Pretrained Language Models",
                     "year": CURRENT_YEAR - 2, "n_citation_bucket": "1001-5000"},
                ]
            return [{"id": query + "-recent", "title": "Multimodal Large Language Model Benchmark",
                     "year": CURRENT_YEAR}]
        client = ScriptedClient({
            "org_search": [{"org_id": "a", "org_name": "Alpha University", "aliases": []}],
            "paper_qa_search": qa,
            "paper_search_pro": [],
            "paper_info": lambda p: [{"id": pid, "title": "Multimodal Large Language Model Benchmark",
                                      "year": CURRENT_YEAR} for pid in p["ids"]],
            "paper_detail": lambda p: [{"id": p["id"],
                                         "title": "CogVLM: Visual Expert for Pretrained Language Models"
                                         if p["id"].endswith("-flagship") else "Multimodal Large Language Model Benchmark",
                                         "year": CURRENT_YEAR,
                                         "keywords": ["Multimodal Learning", "Large Language Model"],
                                         "authors": [{"org": "Alpha University"}]}],
        })
        discover_institutions(client, "multimodal large language model",
                              ["MLLM", "vision-language model", "multimodal"], 2, [], ["Alpha University"])
        detailed_ids = [p["id"] for api, p in client.calls if api == "paper_detail"]
        self.assertTrue(any(pid.endswith("-flagship") for pid in detailed_ids))
        self.assertFalse(any(pid.endswith("-generic") for pid in detailed_ids))

    def test_round_robin_paper_cap_preserves_alias_coverage(self):
        rows = [[{"id": "a1"}, {"id": "a2"}, {"id": "a3"}],
                [{"id": "a1"}, {"id": "b2"}]]
        self.assertEqual(bounded_paper_ids(rows, 3), ["a1", "a2", "b2"])

    def test_person_cost_includes_id_relation_and_main_preflights_multi_person(self):
        self.assertEqual(estimate_person_cost(3, 6, True, 1)["person_paper_relation_calls"], 3)
        with patch.object(sys, "argv", ["recommend.py", "--mode", "person", "--names", "A,B,C",
                                        "--direction", "robotics", "--deep"]):
            with self.assertRaisesRegex(SystemExit, "Worst-case API cost estimate"):
                main()

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

    def test_global_fallback_recovers_when_org_scoped_search_is_empty(self):
        # paper_search_pro with an `org` filter returns nothing (the real API drops
        # papers for many niche directions), but a global search surfaces a paper
        # whose author is affiliated to the target school. The fallback should
        # recover that author instead of returning an empty result.
        detail = {
            "id": "p1", "title": "Optical Fiber Communication Advances", "year": 2025,
            "authors": [{"name": "Fiber Author", "org": "Nankai University", "orgid": "dept9"}],
        }

        def paper_search_pro(params):
            # org-scoped call -> empty; global call (no org) -> one paper
            return [] if params.get("org") else [{"id": "p1", "title": detail["title"], "year": 2025}]

        client = ScriptedClient({
            "org_search": [{"org_id": "school1", "org_name": "Nankai University", "aliases": []}],
            "paper_qa_search": [],
            "paper_search_pro": paper_search_pro,
            "paper_info": [{"id": "p1", "title": detail["title"], "year": 2025}],
            "paper_detail": [detail],
            "person_search": [{"id": "person1", "name": "Fiber Author", "org": "Nankai University", "org_id": "school1"}],
        })
        warnings = []
        result = recommend_for_school(
            client, "Nankai University", "", "optical fiber communication", [], 5, None, 10, False, 0,
            warnings, {}, False, False, 0, 0, False, 10,
        )
        self.assertEqual(len(result), 1)
        self.assertTrue(any("global direction fallback added" in w for w in warnings))
        # a global (org-unfiltered) paper_search_pro call must have happened
        self.assertTrue(any(api == "paper_search_pro" and not p.get("org") for api, p in client.calls))
        self.assertEqual(len([api for api, _ in client.calls if api == "paper_detail"]), 1)



class ScriptedClient:
    """Fake client returning canned payloads per API, keyed by call order or a handler."""

    def __init__(self, handlers):
        # handlers: dict api -> callable(params)->rows  OR  api -> list-of-rows (rotated)
        self.handlers = handlers
        self.calls = []

    def call(self, api, params):
        self.calls.append((api, dict(params)))
        h = self.handlers.get(api)
        if h is None:
            raise AssertionError(f"unexpected API: {api}")
        rows = h(params) if callable(h) else h
        return {"success": True, "code": 200, "data": rows}


class EnglishSchoolAliasTests(unittest.TestCase):
    def test_english_canonical_name_beats_noisy_search_alias(self):
        org = ResolvedOrganization(
            "南京邮电大学", "njupt", "Nanjing University of Posts and Telecommunications",
            ["nanjing posts telecommunications university", "南京邮电大学"])
        self.assertEqual(paper_search_org_name(org),
                         "Nanjing University of Posts and Telecommunications")
        client = ScriptedClient({"paper_search_pro": []})
        complementary_title_search(client, org, ["code generation"], [])
        self.assertTrue(all(params["org"] == org.canonical_name
                            for api, params in client.calls if api == "paper_search_pro"))

    def test_exact_paper_author_profile_recovers_english_search_name(self):
        org = ResolvedOrganization("南京理工大学", "cn-id", "南京理工大学", ["南京理工大学"])
        client = ScriptedClient({
            "paper_detail": [{"authors": [{"id": "person-1", "name": "Zebin Wu",
                                           "orgid": "cn-id", "org": "Nanjing Univ Sci & Technol"}]}],
            "person_search": [{"id": "person-1", "name": "Zebin Wu", "org_id": "cn-id",
                               "org_zh": "南京理工大学计算机科学与工程学院",
                               "org": "School of Computer Science and Engineering, Nanjing University of Science and Technology"}],
            "paper_search_pro": [],
        })
        learn_english_school_alias(client, org, [[{"id": "paper-1"}]], [])
        self.assertEqual(paper_search_org_name(org), "Nanjing University of Science and Technology")
        complementary_title_search(client, org, ["remote sensing object detection"], [])
        self.assertTrue(all(params["org"] == "Nanjing University of Science and Technology"
                            for api, params in client.calls if api == "paper_search_pro"))

    def test_wrong_profile_id_cannot_supply_english_alias(self):
        org = ResolvedOrganization("南京理工大学", "cn-id", "南京理工大学", ["南京理工大学"])
        client = ScriptedClient({
            "paper_detail": [{"authors": [{"id": "person-1", "name": "Zebin Wu", "orgid": "cn-id"}]}],
            "person_search": [{"id": "namesake", "name": "Zebin Wu", "org_id": "cn-id",
                               "org_zh": "南京理工大学", "org": "Nanjing University of Science and Technology"}],
        })
        learn_english_school_alias(client, org, [[{"id": "paper-1"}]], [])
        self.assertEqual(paper_search_org_name(org), "南京理工大学")

    def test_remote_sensing_detection_task_boundary(self):
        self.assertEqual(title_search_seeds(["remote sensing object detection"]),
                         ["remote sensing object detection"])
        self.assertTrue(remote_sensing_object_detection_title(
            "PKINet: Poly Kernel Inception Network for Remote Sensing Detection"))
        self.assertTrue(remote_sensing_object_detection_title(
            "Fine-grained Ship Detection in Complex Remote Sensing Images"))
        self.assertTrue(remote_sensing_object_detection_title(
            "Sparse Object Detection in Aerial Images"))
        self.assertFalse(remote_sensing_object_detection_title(
            "Foundation Models for Remote Sensing Change Detection"))
        self.assertFalse(remote_sensing_object_detection_title(
            "Cloud Detection in Remote Sensing Imagery"))

    def test_author_branch_pass_recovers_verified_company_coauthor(self):
        org = ResolvedOrganization("浙江大学", "zju", "浙江大学", ["Zhejiang University"])
        pan = Candidate("pan", "Gang Pan", name_zh="潘纲", org_id="zju",
                        papers={"seed": {"id": "seed", "title": "Neural Decoding",
                                         "year": CURRENT_YEAR - 1, "topic_evidence": "title",
                                         "direction_term_match": True}})
        paper = {"id": "cbramod", "title": "CBraMod: A Brain Foundation Model for EEG Decoding",
                 "year": CURRENT_YEAR - 1}
        detail = {**paper, "authors": [
            {"id": "pan", "name": "Gang Pan", "orgid": "zju", "org": "Zhejiang University"},
            {"id": "other", "name": "Other Author", "orgid": "alibaba", "org": "Alibaba Group"}]}
        client = ScriptedClient({
            "paper_search_pro": lambda p: [paper] if p.get("query") == "EEG decoding" else [],
            "paper_detail": [detail],
            "org_detail": [{"id": "alibaba", "type": "TYPE_ENTERPRISE"}],
        })
        details = {}
        supplement_collaboration_papers(client, {"pan": pan}, details, org,
                                        ["brain-computer interface", "EEG decoding"], [])
        enrich_collaboration(client, {"pan": pan}, details, [], org)
        self.assertIn("cbramod", pan.papers)
        self.assertEqual(pan.collaboration_types["alibaba"], "industry")


class InterestMismatchFilterTests(unittest.TestCase):
    def test_drops_offfield_keeps_cv_subfields_and_empty(self):
        terms = ["computer vision", "计算机视觉", "object detection"]
        # CV subfields must be KEPT even though they don't literally say "vision"
        slam = Candidate(person_id="p1", name="SLAM Prof", interests=["Simultaneous Localization And Mapping"])
        reid = Candidate(person_id="p2", name="ReID Prof", interests=["Vehicle Re-Identification"])
        # clearly off-field co-authors must be DROPPED
        urban = Candidate(person_id="p3", name="Huang", interests=["Urban Planning", "Architecture"])
        nuclear = Candidate(person_id="p4", name="Huang2", interests=["Monte Carlo", "Reactor Physics"])
        clinical = Candidate(person_id="p5", name="Chen", interests=["Prenatal Diagnosis", "Cancer Risk"])
        no_interest = Candidate(person_id="p6", name="Unknown", interests=[])
        nuclear_norm = Candidate(person_id="p7", name="Luo", interests=["Face Recognition", "Nuclear Norm"])
        out = filter_by_interest_mismatch(
            {c.person_id: c for c in [slam, reid, urban, nuclear, clinical, no_interest, nuclear_norm]}, terms, []
        )
        self.assertEqual(set(out), {"p1", "p2", "p6", "p7"})  # mathematical nuclear norm is on-field

    def test_roster_candidates_never_dropped(self):
        r = Candidate(person_id="r1", name="X", interests=["Nuclear"], identity_resolution="roster")
        out = filter_by_interest_mismatch({"r1": r}, ["computer vision"], [])
        self.assertEqual(set(out), {"r1"})


class InterestMatchTests(unittest.TestCase):
    def test_loose_match_hits_reworded_direction(self):
        terms = ["大模型", "large language model", "大语言模型"]
        self.assertTrue(interest_matches_direction(["Language Modeling", "Topic Modeling"], terms))
        self.assertTrue(interest_matches_direction(["Neural Machine Translation"], ["机器翻译", "machine translation"]))

    def test_rejects_offdirection_and_empty(self):
        terms = ["大模型", "large language model"]
        self.assertFalse(interest_matches_direction(["Power System", "Electricity Market"], terms))
        self.assertFalse(interest_matches_direction(["Monte Carlo", "Heat Transfer"], terms))
        self.assertFalse(interest_matches_direction([], terms))

    def test_direct_substring_still_matches(self):
        self.assertTrue(interest_matches_direction(["Computer Vision", "Detection"], ["computer vision"]))


class BackfillChineseNamesTests(unittest.TestCase):
    def test_same_person_id_chinese_name_filled(self):
        cand = Candidate(person_id="p1", name="Wei Ye", name_zh="", org_id="org-1")
        client = ScriptedClient({"person_search": [
            {"id": "p1", "name": "Wei Ye", "name_zh": "叶蔚", "org_id": "org-1"},
            {"id": "p2", "name": "Wei Ye", "name_zh": "韦烨", "org_id": "org-9"},
        ]})
        backfill_chinese_names(client, [cand], [])
        self.assertEqual(cand.name_zh, "叶蔚")

    def test_same_school_homonym_does_not_supply_chinese_name(self):
        cand = Candidate(person_id="p1", name="Bowen Cao", name_zh="", org_id="org-1")
        client = ScriptedClient({"person_search": [
            {"id": "p2", "name": "Bowen Cao", "name_zh": "曹博文", "org_id": "org-1"},
        ]})
        backfill_chinese_names(client, [cand], [])
        self.assertEqual(cand.name_zh, "")

    def test_unresolved_and_already_named_are_skipped(self):
        unresolved = Candidate(person_id="unresolved:x", name="Di He", name_zh="", org_id="org-1")
        named = Candidate(person_id="p2", name="Yang Yu", name_zh="俞扬", org_id="org-1")

        class Boom:
            def call(self, api, params):
                raise AssertionError("must not query for skipped candidates")

        backfill_chinese_names(Boom(), [unresolved, named], [])
        self.assertEqual(unresolved.name_zh, "")
        self.assertEqual(named.name_zh, "俞扬")


class RosterAugmentTests(unittest.TestCase):
    def _org(self):
        return ResolvedOrganization("清华大学", "org-1", "Tsinghua University", ["清华大学"])

    def test_adds_direction_matched_scholar_missed_by_papers(self):
        pages = {0: [{"id": "s1", "name": "Zhiyuan Liu", "name_zh": "刘知远", "org_id": "org-1",
                      "interests": ["Language Modeling", "Topic Modeling"], "n_citation": 85000}],
                 10: []}
        client = ScriptedClient({"person_search": lambda p: pages.get(p.get("offset", 0), [])})
        existing = {}
        augment_from_roster(client, self._org(), ["大模型", "large language model"], existing, [],
                            max_pages=3, direction_evidence=False)
        self.assertIn("s1", existing)
        self.assertEqual(existing["s1"].identity_resolution, "roster")

    def test_skips_offdirection_and_duplicates(self):
        existing = {"a1": Candidate(person_id="a1", name="Fuchun Sun", name_zh="孙富春", org_id="org-1")}
        pages = {0: [
            {"id": "a1", "name": "Fuchun Sun", "name_zh": "孙富春", "org_id": "org-1", "interests": ["Robotics"]},  # dup
            {"id": "n1", "name": "Kang X", "name_zh": "康重庆", "org_id": "org-1", "interests": ["Power System"]},  # off-dir
        ], 10: []}
        client = ScriptedClient({"person_search": lambda p: pages.get(p.get("offset", 0), [])})
        augment_from_roster(client, self._org(), ["大模型", "large language model"], existing, [],
                            max_pages=2, direction_evidence=False)
        self.assertEqual(set(existing), {"a1"})  # nothing added


class PortraitEnrichTests(unittest.TestCase):
    def test_precise_join_year_from_works(self):
        cand = Candidate(person_id="p1", name="Yuncheng Wu", org="Renmin University of China", org_id="org-1")
        client = ScriptedClient({"person_figure": [{
            "works": [{"org": "Renmin University of China", "start_year": 2024, "position_extra": "副教授"},
                      {"org": "National University of Singapore", "start_year": 2021, "end_year": 2024, "position_extra": "助理教授"}],
            "edus": [{"org": "NUS", "start_year": 2015, "end_year": 2019}],
        }]})
        enrich_from_portrait(client, cand, hired_since=2020)
        self.assertEqual(len(cand.career), 2)
        self.assertEqual(cand.hire_assessment["status"], "likely_recent")
        self.assertIn("2024", cand.hire_assessment["signals"][0])

    def test_join_before_window_is_hired_earlier(self):
        cand = Candidate(person_id="p1", name="X", org="Tsinghua University", org_id="org-1")
        client = ScriptedClient({"person_figure": [{
            "works": [{"org": "Tsinghua University", "start_year": 2010, "position_extra": "教授"}]}]})
        enrich_from_portrait(client, cand, hired_since=2020)
        self.assertEqual(cand.hire_assessment["status"], "hired_earlier")

    def test_sparse_works_leaves_hire_assessment_untouched(self):
        cand = Candidate(person_id="p1", name="X", org="Shanghai Jiao Tong University", org_id="org-1")
        cand.hire_assessment = {"status": "no_hire_evidence", "signals": []}
        client = ScriptedClient({"person_figure": [{
            "works": [{"org": "National University of Singapore", "position_extra": "研究员"}]}]})  # no year, no home org
        enrich_from_portrait(client, cand, hired_since=2020)
        self.assertEqual(cand.hire_assessment["status"], "no_hire_evidence")  # safe fallback
        self.assertEqual(len(cand.career), 1)  # career still recorded


class PersonDeepDiveTests(unittest.TestCase):
    def test_broad_robotics_recall_uses_verified_author_and_school(self):
        papers = {
            "uniact": {"id": "uniact", "title": "UniAct: Action Streaming for Humanoid Robots",
                       "year": CURRENT_YEAR - 1,
                       "authors": [{"id": "wang", "name": "Yizhou Wang",
                                    "org": "Peking University", "orgid": "pku"}]},
            "wrong": {"id": "wrong", "title": "Robot Learning at Another University",
                      "year": CURRENT_YEAR - 1,
                      "authors": [{"id": "wang", "name": "Yizhou Wang",
                                   "org": "Other University", "orgid": "other"}]},
            "namesake": {"id": "namesake", "title": "Robot Learning by a Namesake",
                         "year": CURRENT_YEAR - 1,
                         "authors": [{"id": "different-wang", "name": "Yizhou Wang",
                                      "org": "Peking University", "orgid": "pku"}]},
            "unlinked": {"id": "unlinked", "title": "Robot Learning without Author ID",
                         "year": CURRENT_YEAR - 1,
                         "authors": [{"name": "Yizhou Wang",
                                      "org": "Peking University", "orgid": "pku"}]},
        }
        client = ScriptedClient({
            "person_search": [{"id": "wang", "name": "Yizhou Wang", "name_zh": "王亦洲",
                               "org": "Peking University", "org_zh": "北京大学", "org_id": "pku"}],
            "paper_search_pro": lambda p: [{k: paper[k] for k in ("id", "title", "year")}
                                          for paper in papers.values()] if p.get("query") == "robot" else [],
            "paper_detail": lambda p: [papers[p["id"]]],
            "paper_qa_search": [], "person_paper_relation": [],
        })
        profile = deepdive_person(client, "王亦洲", "北京大学", False, 5, [],
                                  ["embodied intelligence"], broad_context=True)
        self.assertEqual([p["title"] for p in profile["field_recall_papers"]],
                         [papers["uniact"]["title"]])
        self.assertEqual(broad_person_field_recall_terms(
            ["embodied intelligence"], ["Humanoid Agents", "ForceVLA"]),
            ["robot", "force", "humanoid"])

    def test_recent_profile_sample_covers_more_than_newest_year(self):
        papers = ([{"title": f"New {i}", "year": 2026, "recent": True, "topic_match": True}
                   for i in range(8)] +
                  [{"title": f"Established {i}", "year": 2025, "recent": True,
                    "topic_match": True} for i in range(3)])
        selected = select_representative_papers(papers, 6)
        self.assertEqual(len(selected), 6)
        self.assertEqual(sum(p["year"] == 2025 for p in selected), 3)

    def test_duplicate_titles_from_different_aminer_ids_are_one_work(self):
        papers = [
            {"title": "Learning to Move Before Learning to Do", "year": 2026,
             "recent": True, "topic_match": True, "aminer_url": "https://www.aminer.cn/pub/a"},
            {"title": "Learning to Move Before Learning to Do", "year": 2026,
             "recent": True, "topic_match": True, "aminer_url": "https://www.aminer.cn/pub/b"},
        ]
        self.assertEqual(len(select_representative_papers(papers, 5)), 1)

    def test_person_id_relation_recovers_recent_old_affiliation_work(self):
        client = ScriptedClient({
            "person_search": [{"id": "sjtu-liu", "name": "Fucheng Liu", "name_zh": "刘付成",
                               "org": "Shanghai Jiao Tong University", "org_zh": "上海交通大学", "org_id": "sjtu"}],
            "paper_search_pro": [],
            "person_paper_relation": [
                {"id": "p-new", "author_id": "sjtu-liu", "title": "Satellite Attitude Control"},
                {"id": "p-old", "author_id": "sjtu-liu", "title": "Satellite Attitude Control"},
                {"id": "p-future", "author_id": "sjtu-liu", "title": "Satellite Attitude Control"},
                {"id": "p-other", "author_id": "other-person", "title": "Satellite Attitude Control"},
            ],
            "paper_info": lambda p: [
                {"id": "p-new", "title": "Satellite Attitude Control with Reaction Wheels", "year": 2025,
                 "authors": [{"name": "Fucheng Liu", "org": "Old University"}]},
                {"id": "p-old", "title": "Satellite Attitude Control", "year": 2015},
                {"id": "p-future", "title": "Satellite Attitude Control", "year": 2027},
            ],
        })
        warnings = []
        profile = deepdive_person(client, "刘付成", "上海交通大学", False, 5, warnings,
                                  ["satellite attitude control"])
        self.assertEqual([p["aminer_url"].split("/")[-1] for p in profile["recent_papers"]], ["p-new"])
        self.assertEqual(profile["recent_papers"][0]["source"], "person_paper_relation")
        self.assertTrue(any("具体作者单位仍需原文确认" in w for w in warnings))
        relation = [p for api, p in client.calls if api == "person_paper_relation"]
        self.assertEqual(relation, [{"id": "sjtu-liu"}])

    def test_id_relation_labels_off_topic_paper_instead_of_claiming_focus(self):
        client = ScriptedClient({
            "person_paper_relation": [{"id": "p1", "author_id": "person1"}],
            "paper_info": [{"id": "p1", "title": "Photonic Crystal", "year": 2025}],
        })
        papers = linked_recent_papers(client, "person1", ["satellite attitude control"], 5, [])
        self.assertEqual(len(papers), 1)
        self.assertFalse(papers[0]["topic_match"])

    def test_focused_code_reasoning_does_not_count_generic_llm_papers(self):
        terms = ["LLM code reasoning", "large language model", "LLM", "code reasoning"]
        self.assertTrue(focus_topic_relevant(
            "MARCO: Meta-Reflection with Cross-Referencing for Code Reasoning", terms))
        self.assertFalse(focus_topic_relevant(
            "Sample Lottery: Unsupervised Discovery of Critical Instances for LLM Reasoning", terms))
        self.assertTrue(focus_topic_relevant(
            "GEO Satellite Attitude Control", ["spacecraft attitude control", "attitude control"]))
        self.assertTrue(focus_topic_relevant(
            "Long-Horizon Coding Agents", ["code generation", "coding agents"]))
        self.assertTrue(focus_topic_relevant(
            "When Requirements Interact: Scaling Long-Horizon Coding Via Interdependent Specifications",
            ["code generation", "coding agent"]))
        self.assertTrue(focus_topic_relevant(
            "Quality, Not Just Outcome: Selecting Coding-Agent Trajectories",
            ["code generation", "coding agent"]))
        self.assertTrue(focus_topic_relevant(
            "Program Synthesis with Tests", ["code generation", "program synthesis"]))
        self.assertFalse(focus_topic_relevant(
            "Towards Codable Watermarking for LLMs", ["code generation", "coding agents"]))
        self.assertFalse(focus_topic_relevant(
            "CODE: Towards Partial Label Graph Learning", ["code generation", "coding agents",
                                                         "large language model for code"]))
        self.assertTrue(focus_topic_relevant(
            "ToolLLM: Facilitating Large Language Models to Master Real-world APIs",
            ["large language model", "pretraining", "instruction tuning"]))

    def test_id_linked_work_is_merged_even_when_name_search_found_one_paper(self):
        client = ScriptedClient({
            "person_search": [{"id": "sun", "name": "Xu Sun", "name_zh": "孙栩",
                               "org": "Peking University", "org_zh": "北京大学", "org_id": "pku"}],
            "paper_search_pro": [{"id": "p-old", "title": "Code Generation with LLMs", "year": 2024}],
            "paper_detail": [{"id": "p-old", "authors": [
                {"name": "Xu Sun", "org": "Peking University", "orgid": "pku"}]}],
            "person_paper_relation": [
                {"id": "p-old", "author_id": "sun", "title": "Code Generation with LLMs"},
                {"id": "p-new", "author_id": "sun", "title": "Long-Horizon Coding Agents"}],
            "paper_info": [{"id": "p-old", "title": "Code Generation with LLMs", "year": 2024},
                           {"id": "p-new", "title": "Long-Horizon Coding Agents", "year": 2026}],
        })
        profile = deepdive_person(client, "孙栩", "北京大学", False, 5, [],
                                  ["code generation", "coding agents"])
        titles = [p["title"] for p in profile["recent_papers"]]
        self.assertEqual(titles, ["Long-Horizon Coding Agents", "Code Generation with LLMs"])
        self.assertEqual(len([api for api, _ in client.calls if api == "person_paper_relation"]), 1)

    def test_full_first_query_does_not_starve_second_focus_alias(self):
        client = ScriptedClient({
            "person_search": [{"id": "li", "name": "Ge Li", "name_zh": "李戈",
                               "org": "Peking University", "org_zh": "北京大学", "org_id": "pku"}],
            "paper_search_pro": lambda p: ([
                {"id": f"primary-{i}", "title": "Code Generation with LLMs", "year": 2025}
                for i in range(5)] if p.get("query") == "code generation" else [
                {"id": "agent", "title": "Coding-Agent Trajectories for Software Engineering", "year": 2026}]),
            "paper_detail": lambda p: [{"id": p["id"], "authors": [
                {"id": "li", "name": "Ge Li", "org": "Peking University", "orgid": "pku"}]}],
            "person_paper_relation": [],
        })
        profile = deepdive_person(client, "李戈", "北京大学", False, 5, [],
                                  ["code generation", "coding agent"])
        titles = [p["title"] for p in profile["recent_papers"]]
        self.assertIn("Coding-Agent Trajectories for Software Engineering", titles)
        searches = [p["query"] for api, p in client.calls if api == "paper_search_pro"]
        self.assertEqual(searches, ["code generation", "code generation", "coding agent"])

    def test_citation_order_adds_influential_recent_work(self):
        client = ScriptedClient({
            "person_search": [{"id": "liu", "name": "Zhiyuan Liu", "name_zh": "刘知远",
                               "org": "Tsinghua University", "org_zh": "清华大学", "org_id": "thu"}],
            "paper_search_pro": lambda p: ([
                {"id": "influential", "title": "Tool Learning with Large Language Models", "year": 2024}]
                if p.get("order") == "n_citation" else [
                {"id": f"new-{i}", "title": "Large Language Model Study", "year": 2026}
                for i in range(5)]),
            "paper_detail": lambda p: [{"id": p["id"], "authors": [
                {"id": "liu", "name": "Zhiyuan Liu", "org": "Tsinghua University", "orgid": "thu"}]}],
            "person_paper_relation": [],
        })
        profile = deepdive_person(client, "刘知远", "清华大学", False, 5, [],
                                  ["large language model"])
        self.assertIn("Tool Learning with Large Language Models",
                      [p["title"] for p in profile["recent_papers"]])
        self.assertFalse(profile["recent_papers_sampling"]["exhaustive"])
        self.assertEqual(profile["recent_papers_sampling"]["limit"], 5)
        orders = [p["order"] for api, p in client.calls if api == "paper_search_pro"]
        self.assertEqual(orders, ["year", "n_citation"])

    def test_broad_context_recovers_landmarks_without_accepting_name_collision(self):
        client = ScriptedClient({
            "person_search": [{"id": "liu", "name": "Zhiyuan Liu", "name_zh": "刘知远",
                               "org": "Tsinghua University", "org_zh": "清华大学", "org_id": "thu"}],
            "paper_search_pro": lambda p: ([
                {"id": "sensor", "title": "Technology Roadmap for Flexible Sensors", "year": 2023},
                {"id": "minicpm", "title": "MiniCPM: Scalable Small Language Models", "year": 2024,
                 "n_citation_bucket": "201-1000"},
                {"id": "feedback", "title": "UltraFeedback: Boosting Language Models", "year": 2024},
            ] if not p.get("query") and p.get("order") == "n_citation" else [
                {"id": "new", "title": "New Vision-Language-Action Model", "year": CURRENT_YEAR},
            ] if not p.get("query") and p.get("order") == "year" else []),
            "paper_detail": lambda p: [{"id": p["id"], "authors": [{
                "id": "liu", "name": "Zhiyuan Liu", "org": "Other University" if p["id"] == "sensor"
                else "Tsinghua University", "orgid": "other" if p["id"] == "sensor" else "thu",
            }]}],
            "paper_qa_search": [
                {"id": "recent-rl", "title": "JustRL: Scaling a 1.5B LLM with a Simple RL Recipe",
                 "year": CURRENT_YEAR - 1, "venue_name": "ICLR"},
                {"id": "old", "title": "Older Language Model Study", "year": CURRENT_YEAR - 2},
            ],
            "person_paper_relation": [],
        })
        profile = deepdive_person(client, "刘知远", "清华大学", False, 5, [],
                                  ["large language model"], broad_context=True)
        titles = [p["title"] for p in profile["broad_context_papers"]]
        self.assertEqual(titles, ["MiniCPM: Scalable Small Language Models",
                                  "UltraFeedback: Boosting Language Models"])
        self.assertTrue(all(p["source"] == "citation_ranked_author_verified"
                            for p in profile["broad_context_papers"]))
        self.assertEqual([p["title"] for p in profile["recent_context_papers"]],
                         ["New Vision-Language-Action Model"])
        self.assertFalse(profile["recent_context_papers"][0]["topic_match"])
        self.assertEqual([p["title"] for p in profile["author_id_recent_search"]["cards"]],
                         ["JustRL: Scaling a 1.5B LLM with a Simple RL Recipe"])
        qa_calls = [p for api, p in client.calls if api == "paper_qa_search"]
        self.assertEqual(qa_calls[0]["author_id"], ["liu"])
        self.assertEqual(qa_calls[0]["year"], [CURRENT_YEAR - 1, CURRENT_YEAR])
        broad_calls = [p for api, p in client.calls if api == "paper_search_pro" and not p.get("query")]
        self.assertEqual([p["order"] for p in broad_calls], ["n_citation", "year"])
        self.assertTrue(all((p["year_from"], p["year_to"]) == (CURRENT_YEAR - 5, CURRENT_YEAR)
                            for p in broad_calls))
        self.assertGreater(estimate_person_cost(2, 10, True, 6, True)["worst_case_cny"],
                           estimate_person_cost(2, 10, True, 6)["worst_case_cny"])
        self.assertEqual(estimate_person_cost(2, 10, True, 6, True)["paper_qa_search_calls"], 2)

    def test_relation_prioritizes_focused_title_outside_newest_200_ids(self):
        rows = [{"id": f"z{i:03}", "author_id": "person1", "title": "Unrelated Graph Study"}
                for i in range(230)]
        rows.append({"id": "a-focused", "author_id": "person1", "title": "Program Synthesis with Tests"})
        client = ScriptedClient({
            "person_paper_relation": rows,
            "paper_info": lambda p: [{"id": "a-focused", "title": "Program Synthesis with Tests", "year": 2026}]
            if "a-focused" in p["ids"] else [],
        })
        papers = linked_recent_papers(client, "person1", ["code generation", "program synthesis"], 5, [])
        self.assertEqual([p["title"] for p in papers], ["Program Synthesis with Tests"])

    def test_relation_abstract_incidental_focus_does_not_prove_topic(self):
        client = ScriptedClient({
            "person_paper_relation": [{"id": "p1", "author_id": "person1",
                                       "title": "Alpha Mining in Finance"}],
            "paper_info": [{"id": "p1", "title": "Alpha Mining in Finance", "year": 2026,
                            "abstract_slice": "Large language models for code generation are unrelated background."}],
        })
        papers = linked_recent_papers(client, "person1", ["code generation"], 5, [])
        self.assertFalse(papers[0]["topic_match"])

    def test_paper_topic_relevance_flags_low_overlap(self):
        interests = ["Attitude Control", "Geo Satellite", "Reaction Wheel"]
        self.assertTrue(paper_topic_relevant("Active Vibration Control of On-Orbit Assembly", interests))
        # plasma photonic crystal paper shares no interest token -> low overlap
        self.assertFalse(paper_topic_relevant("Tunable Valley Topological States Via Plasma Photonic Crystals", interests))
        # no interests -> cannot filter, treat as match
        self.assertTrue(paper_topic_relevant("anything at all", []))

    def test_deepdive_disambiguates_and_flags_mixin(self):
        class DDClient:
            def call(self, api, params):
                if api == "person_search":
                    return {"success": True, "data": [
                        {"id": "a1", "name": "Fucheng Liu", "name_zh": "刘付成",
                         "org": "Peking University", "org_zh": "北京大学", "n_citation": 5000, "interests": ["X"]},
                        {"id": "a2", "name": "Fucheng Liu", "name_zh": "刘付成",
                         "org": "Shanghai Jiao Tong University", "org_zh": "上海交通大学航空航天学院",
                         "n_citation": 212, "interests": ["Attitude Control"]},
                    ]}
                if api == "paper_search_pro":
                    return {"success": True, "data": [
                        {"id": "p1", "title": "Attitude Control of Satellite", "year": 2026},
                        {"id": "p2", "title": "Plasma Photonic Crystal", "year": 2026},
                        {"id": "p3", "title": "Valley Topological States", "year": 2026},
                    ]}
                if api == "person_paper_relation":
                    return {"success": True, "data": []}
                raise AssertionError(api)

        # school given, exactly one org matches -> confident deep-dive of a2 (not the higher-cited a1)
        warnings = []
        prof = deepdive_person(DDClient(), "刘付成", "上海交通大学", deep=False, paper_limit=6, warnings=warnings)
        self.assertTrue(prof["found"])
        self.assertFalse(prof.get("needs_disambiguation"))
        self.assertEqual(prof["person_id"], "a2")            # school match wins over higher citation
        # 2/3 papers off-topic (majority) -> by-name list deemed unreliable, keep only on-topic
        self.assertFalse(prof.get("recent_papers_reliable"))
        titles = [p["title"] for p in prof["recent_papers"]]
        self.assertEqual(titles, ["Attitude Control of Satellite"])
        self.assertTrue(any("污染" in w for w in warnings))

    def test_deepdive_common_name_asks_for_disambiguation(self):
        """A bare common name resolving to many distinct people must NOT be guessed."""
        class CommonNameClient:
            def call(self, api, params):
                assert api == "person_search"          # must not reach paper_search_pro
                return {"success": True, "data": [
                    {"id": "z1", "name": "Tao Zhang", "name_zh": "张涛", "org": "Catalysis Inst", "n_citation": 9000, "interests": ["Catalysis"]},
                    {"id": "z2", "name": "Tao Zhang", "name_zh": "张涛", "org": "Materials Sci", "n_citation": 8000, "interests": ["Metallic Glass"]},
                    {"id": "z3", "name": "Tao Zhang", "name_zh": "张涛", "org": "Automation", "n_citation": 7000, "interests": ["Reinforcement Learning"]},
                ]}
        warnings = []
        prof = deepdive_person(CommonNameClient(), "张涛", "", deep=False, paper_limit=6, warnings=warnings)
        self.assertTrue(prof["needs_disambiguation"])
        self.assertGreaterEqual(len(prof["candidates"]), 3)
        self.assertNotIn("recent_papers", prof)             # did not deep-dive a guess
        self.assertTrue(any("无法确定" in w for w in warnings))

    def test_deepdive_dominant_citation_is_confident(self):
        """No school, but top citation dominates -> confident pick, no disambiguation."""
        class DomClient:
            def call(self, api, params):
                if api == "person_search":
                    return {"success": True, "data": [
                        {"id": "d1", "name": "Linjiang Zheng", "name_zh": "郑林江", "org": "Chongqing Univ", "n_citation": 971, "interests": ["Data Models"]},
                        {"id": "d2", "name": "Linjiang Zheng", "name_zh": "郑林江", "org": "Somewhere", "n_citation": 30, "interests": ["Other"]},
                    ]}
                return {"success": True, "data": [{"id": "q1", "title": "Data Models for Flight Safety", "year": 2026}]}
        prof = deepdive_person(DomClient(), "郑林江", "", deep=False, paper_limit=6, warnings=[])
        self.assertFalse(prof.get("needs_disambiguation"))
        self.assertEqual(prof["person_id"], "d1")

    def test_deepdive_not_found(self):
        class EmptyClient:
            def call(self, api, params):
                return {"success": True, "data": []}
        prof = deepdive_person(EmptyClient(), "查无此人", "", deep=False, paper_limit=6, warnings=[])
        self.assertFalse(prof["found"])

    def test_named_focus_finds_marco_and_excludes_other_school_namesake(self):
        marco = "6833cebb163c01c85061f69d"
        client = ScriptedClient({
            "person_search": [{"id": "pku-ming", "name": "Ming Zhang", "name_zh": "张铭",
                               "org": "Institute of Data Science, School of CS, Peking University",
                               "org_zh": "北京大学计算机学院",
                               "org_id": "pku", "interests": ["Code Reasoning"]}],
            "paper_search_pro": [
                {"id": marco, "title": "MARCO: Meta-Reflection with Cross-Referencing for Code Reasoning", "year": 2025},
                {"id": "fudan-paper", "title": "Code Reasoning at Fudan", "year": 2025},
            ],
            "paper_detail": lambda p: [{"id": p["id"], "authors": [
                {"id": "pku-ming", "name": "Zhang, Ming", "org": "Peking University", "orgid": "pku"}
                if p["id"] == marco else
                {"name": "Ming Zhang", "org": "Fudan University", "orgid": "fudan"}
            ]}],
            "person_paper_relation": [],
        })
        warnings = []
        profile = deepdive_person(client, "张铭", "北京大学", False, 10, warnings, ["code"])
        self.assertEqual([p["aminer_url"].split("/")[-1] for p in profile["recent_papers"]], [marco])
        self.assertTrue(profile["recent_papers"][0]["topic_match"])
        searches = [p for api, p in client.calls if api == "paper_search_pro"]
        self.assertEqual(len(searches), 2)
        self.assertEqual([p["order"] for p in searches], ["year", "n_citation"])
        self.assertTrue(all(p["query"] == "code" and p["org"] == "Peking University"
                            and "keyword" not in p for p in searches))
        self.assertEqual(len([api for api, _ in client.calls if api == "paper_detail"]), 2)
        self.assertTrue(any("未核实到该校同名作者" in w for w in warnings))

    def test_exact_chinese_name_excludes_longer_fuzzy_match_at_same_school(self):
        client = ScriptedClient({
            "person_search": [
                {"id": "li-ge", "name": "Ge Li", "name_zh": "李戈", "org": "Peking University",
                 "org_zh": "北京大学", "n_citation": 100},
                {"id": "li-gehui", "name": "Gehui Li", "name_zh": "李戈辉", "org": "Peking University",
                 "org_zh": "北京大学", "n_citation": 2000},
            ],
            "paper_search_pro": [], "person_paper_relation": [],
        })
        profile = deepdive_person(client, "李戈", "北京大学", False, 5, [])
        self.assertFalse(profile["needs_disambiguation"])
        self.assertEqual(profile["person_id"], "li-ge")

    def test_incidental_abstract_terms_do_not_make_watermarking_code_work(self):
        client = ScriptedClient({
            "person_search": [{"id": "sun", "name": "Xu Sun", "name_zh": "孙栩",
                               "org": "Peking University", "org_zh": "北京大学", "org_id": "pku"}],
            "paper_search_pro": [{"id": "watermark", "title": "Codable Text Watermarking for LLMs",
                                  "year": 2024}],
            "paper_detail": [{"id": "watermark", "authors": [
                {"name": "Xu Sun", "org": "Peking University", "orgid": "pku"}],
                "keywords": ["text watermarking", "large language model"],
                "abstract": "Code generation is one of many contexts for language models."}],
            "person_paper_relation": [],
        })
        profile = deepdive_person(client, "孙栩", "北京大学", False, 5, [],
                                  ["code generation", "coding agents"])
        self.assertEqual(profile["recent_papers"], [])

    def test_school_constraint_does_not_pick_other_school_by_citations(self):
        client = ScriptedClient({"person_search": [
            {"id": "fudan", "name": "Ming Zhang", "org": "Fudan University", "n_citation": 9000},
            {"id": "nju", "name": "Ming Zhang", "org": "Nanjing University", "n_citation": 10},
        ]})
        profile = deepdive_person(client, "张铭", "北京大学", False, 10, [])
        self.assertTrue(profile["needs_disambiguation"])
        self.assertFalse(any(api == "paper_search_pro" for api, _ in client.calls))


if __name__ == "__main__":
    unittest.main()
