import importlib.util
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("shortlist_gate", SCRIPTS / "shortlist_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class FakeClient:
    def call(self, api, params):
        if api == "person_detail":
            return {"success": True, "data": {"id": params["id"], "name": "Zhi Liu", "name_zh": "刘志"}}
        if api == "paper_detail":
            return {"success": True, "data": [{"id": params["id"], "title": "Detection Work", "year": 2024,
                     "authors": [{"id": "p1", "name": "Zhi Liu", "org": "Shanghai University"}]}]}
        raise AssertionError(api)


class FakeEnglishClient:
    def call(self, api, params):
        if api == "person_detail":
            return {"success": True, "data": {"id": params["id"], "name": "Tingting Han", "name_zh": ""}}
        if api == "paper_detail":
            return {"success": True, "data": [{"id": params["id"], "title": "HERO Detection", "year": 2026,
                     "authors": [{"id": "h1", "name": "Tingting Han", "org": "Hangzhou Dianzi University"}]}]}
        raise AssertionError(api)


class WrongAffiliationClient(FakeClient):
    def call(self, api, params):
        row = super().call(api, params)
        if api == "paper_detail":
            row["data"][0]["authors"][0]["org"] = "Other University"
        return row


class ResolvableClient(FakeClient):
    def __init__(self, search_id="p1"):
        self.search_id = search_id

    def call(self, api, params):
        if api == "person_search":
            return {"success": True, "data": [{"id": self.search_id,
                     "name": "Zhi Liu", "name_zh": "刘志"}]}
        return super().call(api, params)


class EnglishUniversityClient(FakeClient):
    def __init__(self, author_org_id="school-suda", author_org="School of Computer Science, Soochow University"):
        self.author_org_id = author_org_id
        self.author_org = author_org

    def call(self, api, params):
        if api == "org_search":
            self_query = params["orgs"]
            assert self_query == ["苏州大学"]
            return {"success": True, "data": [{"org_id": "school-suda", "org_name": "Soochow University"}]}
        row = super().call(api, params)
        if api == "paper_detail":
            row["data"][0]["authors"][0].update(org=self.author_org, orgid=self.author_org_id)
        return row


class ShortlistGateTest(unittest.TestCase):
    def test_missing_person_id_resolves_only_by_exact_school_name_and_paper_author(self):
        row = {"name": "刘志", "school_domain": "shu.edu.cn",
               "school_names": ["上海大学", "Shanghai University"],
               "official_profile_url": "https://faculty.shu.edu.cn/person",
               "paper": {"aminer_id": "paper1", "title": "Detection Work", "year": 2024}}
        page = "刘志 上海大学教授，博士生导师。代表作 Detection Work, 2024"
        with patch.object(gate, "fetch_official", return_value=page):
            approved = gate.verify_candidate(row, [], "doctor", ResolvableClient(), ["detection"])
            wrong_id = gate.verify_candidate(row, [], "doctor", ResolvableClient("other"), ["detection"])
        self.assertTrue(approved["approved"], approved["problems"])
        self.assertEqual(approved["verified"]["resolved_person_id"], "p1")
        self.assertFalse(wrong_id["approved"])
        self.assertIn("cannot be uniquely resolved", " ".join(wrong_id["problems"]))

    def test_admissions_date_uses_china_day_across_utc_midnight(self):
        real_datetime = datetime

        class FixedDatetime:
            @staticmethod
            def now(tz):
                return real_datetime(2026, 11, 5, 16, 30, tzinfo=timezone.utc).astimezone(tz)

        with patch.object(gate, "datetime", FixedDatetime):
            self.assertEqual(gate.today_in_china().isoformat(), "2026-11-06")

    def test_newer_profile_title_remains_visible_when_old_role_page_has_old_title(self):
        row = {"name": "曹敏", "school_domain": "suda.edu.cn",
               "official_profile_url": "https://scst.suda.edu.cn/new/page.htm",
               "official_supervisor_url": "https://scst.suda.edu.cn/old/page.htm",
               "paper": {"title": "Vision-Grounded Text-Based Person Retrieval", "year": 2026}}
        profile = ("苏州大学 学院 曹敏 时间：2023-04-07 曹敏，博士 副教授 "
                   "代表作 Vision-Grounded Text-Based Person Retrieval, 2026")
        old_role = "曹敏，讲师 硕士生导师 时间：2023-02-01"
        with patch.object(gate, "fetch_official", side_effect=[profile, old_role]):
            result = gate.verify_candidate(row, [], "master", None, ["text-based retrieval"])
        self.assertTrue(result["approved"], result["problems"])
        self.assertIn("副教授", result["verified"]["official_profile_title_context"])
        self.assertNotIn("讲师", result["verified"]["official_profile_title_context"])
        self.assertEqual(result["verified"]["official_profile_title"], "副教授")
        self.assertEqual(result["verified"]["supervisor_role_page_title"], "讲师")
        self.assertEqual(result["verified"]["preferred_title_source"], row["official_profile_url"])
        self.assertEqual(result["verified"]["current_faculty_and_cycle_status"], "not_verified_by_gate")
        self.assertTrue(any("title conflict" in warning for warning in result.get("warnings", [])))

    def test_utf8_candidates_file_cli_reaches_fail_closed_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "candidates.json"
            source.write_text(json.dumps([{"name": "张栋", "school_domain": "suda.edu.cn"}],
                                         ensure_ascii=False), encoding="utf-8")
            env = dict(os.environ)
            env.pop("AMINER_API_KEY", None)
            proc = subprocess.run([sys.executable, str(SCRIPTS / "shortlist_gate.py"),
                                   "--candidates-file", str(source), "--degree", "master",
                                   "--allowed-domains", "suda.edu.cn",
                                   "--focus-terms", "vision-language,image-text,图文检索"],
                                  capture_output=True, text=True, env=env, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        output = json.loads(proc.stdout)
        self.assertEqual(output["approved"], [])
        self.assertEqual(output["leads"][0]["name"], "张栋")

    def test_adjacent_credential_paper_does_not_gain_direct_fit_evidence(self):
        row = {"name": "张可", "school_domain": "suda.edu.cn",
               "official_profile_url": "https://scst.suda.edu.cn/person",
               "paper": {"title": "PatchNet: Weakly Supervised Semantic Segmentation", "year": 2022}}
        page = ("张可 苏州大学计算机科学与技术学院 副教授 硕士生导师 "
                "研究方向：多模态大语言模型下的视频文本分析。"
                "代表作 PatchNet: Weakly Supervised Semantic Segmentation, 2022")
        with patch.object(gate, "fetch_official", return_value=page):
            result = gate.verify_candidate(row, [], "master", None,
                                           ["vision-language", "image-text", "图文检索", "MLLM"])
        self.assertTrue(result["approved"], result["problems"])
        self.assertEqual(result["fit_audit"]["matched_focus_terms"], [])
        self.assertIn("not certified", result["fit_audit"]["scope"])

    def test_topic_specific_credential_paper_exposes_title_overlap(self):
        row = {"name": "鲁继文", "school_domain": "tsinghua.edu.cn",
               "official_profile_url": "https://au.tsinghua.edu.cn/person",
               "paper": {"title": "Point Cloud Segmentation by Contrastive Learning", "year": 2026}}
        page = ("鲁继文 清华大学自动化系 教授 博士生导师 "
                "代表作 Point Cloud Segmentation by Contrastive Learning, 2026")
        with patch.object(gate, "fetch_official", return_value=page):
            result = gate.verify_candidate(row, [], "doctor", None,
                                           ["point cloud", "LiDAR", "SLAM"])
        self.assertTrue(result["approved"], result["problems"])
        self.assertEqual(result["fit_audit"]["matched_focus_terms"], ["point cloud"])

    def test_umbrella_term_does_not_turn_adjacent_paper_into_project_match(self):
        self.assertEqual(gate.title_focus_matches("Multimodal Face Anti-Spoofing",
                                                   ["multimodal", "多模态", "image-text"]), [])
        self.assertEqual(gate.title_focus_matches("Text-Image Relation Inference",
                                                   ["multimodal", "image-text"]), [])
        self.assertEqual(gate.title_focus_matches("Vision-aided Parsing with Multi-MLLM Debating",
                                                   ["MLLM", "fine-tuning", "cross-modal retrieval"]), [])
        self.assertEqual(gate.title_focus_matches("Text-Based Person Retrieval",
                                                   ["MLLM", "text-based person retrieval"]),
                         ["text-based person retrieval"])

    def test_transient_official_profile_shell_is_retried_without_relaxing_role(self):
        row = {"name": "崔兆鹏", "school_domain": "zju.edu.cn",
               "official_profile_url": "https://person.zju.edu.cn/zhpcui",
               "paper": {"title": "CG-SLAM: Dense RGB-D SLAM", "year": 2024}}
        complete = ("崔兆鹏 浙江大学计算机科学与技术学院 研究员 博士生导师 "
                    "研究方向：机器人感知。论文 CG-SLAM: Dense RGB-D SLAM, 2024")
        with patch.object(gate, "fetch_official", side_effect=["浙江大学个人主页 首页", complete]) as fetch:
            result = gate.verify_candidate(row, [], "doctor", None, ["SLAM"])
        self.assertEqual(fetch.call_count, 2)
        self.assertTrue(result["approved"], result["problems"])

    def test_two_partial_pages_still_fail_closed(self):
        row = {"name": "崔兆鹏", "school_domain": "zju.edu.cn",
               "official_profile_url": "https://person.zju.edu.cn/zhpcui",
               "paper": {"title": "CG-SLAM: Dense RGB-D SLAM", "year": 2024}}
        with patch.object(gate, "fetch_official", side_effect=["浙江大学个人主页 首页", "崔兆鹏 研究员"]):
            result = gate.verify_candidate(row, [], "doctor", None, ["SLAM"])
        self.assertFalse(result["approved"])
        self.assertTrue(any("supervisor evidence" in p for p in result["problems"]))

    def test_wrong_chinese_name_and_missing_official_program_are_blocked(self):
        row = {"name": "刘智", "school_domain": "shu.edu.cn", "person_id": "p1",
               "official_profile_url": "https://ee.shu.edu.cn/liu", "school_names": ["Shanghai University"],
               "paper": {"title": "Detection Work", "year": 2024, "aminer_id": "paper1"}}
        with patch.object(gate, "fetch_official", return_value="刘志 上海大学通信与信息工程学院 教授 博士生导师"):
            result = gate.verify_candidate(row, ["计算机学院"], "master", FakeClient())
        self.assertFalse(result["approved"])
        self.assertTrue(any("刘志" in problem for problem in result["problems"]))
        self.assertTrue(any("requested program" in problem for problem in result["problems"]))

    def test_official_faculty_paper_and_supervisor_evidence_pass(self):
        row = {"name": "王伟行", "school_domain": "suda.edu.cn",
               "official_profile_url": "https://scst.suda.edu.cn/person",
               "paper": {"title": "Domain Generalization for Face Anti-Spoofing", "year": 2023}}
        page = "王伟行 苏州大学计算机科学与技术学院 讲师 硕士生导师 代表作 Domain generalization for face anti-spoofing via negative data augmentation, 2023"
        with patch.object(gate, "fetch_official", return_value=page):
            result = gate.verify_candidate(row, ["计算机科学与技术学院"], "master", None)
        self.assertTrue(result["approved"], result["problems"])

    def test_individual_official_phd_recruitment_establishes_doctor_role(self):
        row = {"name": "弋力", "school_domain": "tsinghua.edu.cn",
               "official_profile_url": "https://iiis.tsinghua.edu.cn/rydw/qzjs/yili.htm",
               "paper": {"title": "Point Cloud Robotics", "year": 2024}}
        page = ("弋力 助理教授 三维视觉 I am actively looking for motivated visiting students, "
                "interns, PhDs, and postdocs. Publications: Point Cloud Robotics, 2024")
        with patch.object(gate, "fetch_official", return_value=page):
            result = gate.verify_candidate(row, [], "doctor", None, ["point cloud"])
        self.assertTrue(result["approved"], result["problems"])

    def test_doctorate_in_bio_does_not_establish_supervisor_role(self):
        self.assertFalse(gate.has_role_for_name(
            "弋力 助理教授 PhD from Stanford University. Point cloud robotics.",
            "弋力", "doctor", roster=False))

    def test_unspecified_college_does_not_infer_one_from_applicant_major(self):
        row = {"name": "徐杭", "school_domain": "hdu.edu.cn",
               "official_profile_url": "https://comm.hdu.edu.cn/person",
               "paper": {"title": "Object Detection", "year": 2024}}
        page = "徐杭 杭州电子科技大学通信工程学院 副教授 硕士生导师 研究方向 目标检测 论文 Object Detection, 2024"
        with patch.object(gate, "fetch_official", return_value=page):
            unrestricted = gate.verify_candidate(row, [], "master", None)
            restricted = gate.verify_candidate(row, ["计算机学院"], "master", None)
        self.assertTrue(unrestricted["approved"], unrestricted["problems"])
        self.assertFalse(restricted["approved"])

    def test_profile_role_survives_generic_optional_roster(self):
        row = {"name": "王伟行", "school_domain": "suda.edu.cn",
               "official_profile_url": "https://scst.suda.edu.cn/person",
               "official_supervisor_url": "https://scst.suda.edu.cn/11250/list.htm",
               "paper": {"title": "Face Anti-Spoofing", "year": 2023}}
        pages = {
            row["official_profile_url"]: "王伟行 苏州大学计算机科学与技术学院 讲师 硕士生导师 论文 Face Anti-Spoofing, 2023",
            row["official_supervisor_url"]: "苏州大学计算机科学与技术学院 导师简介 王伟行",
        }
        with patch.object(gate, "fetch_official", side_effect=lambda url, domain: pages[url]):
            result = gate.verify_candidate(row, ["计算机科学与技术学院"], "master", None)
        self.assertTrue(result["approved"], result["problems"])

    def test_split_official_bios_pass_with_old_role_source_warning(self):
        row = {"name": "曹敏", "school_domain": "suda.edu.cn",
               "official_profile_url": "https://scst.suda.edu.cn/new/page.htm",
               "official_supervisor_url": "https://scst.suda.edu.cn/old/page.htm",
               "paper": {"title": "Text-Based Person Retrieval", "year": 2026}}
        pages = {
            row["official_profile_url"]: "曹敏 副教授 代表作 Text-Based Person Retrieval, 2026",
            row["official_supervisor_url"]: "曹敏 时间: 2020-01-01 讲师，硕士生导师。",
        }
        with patch.object(gate, "fetch_official", side_effect=lambda url, domain: pages[url]):
            result = gate.verify_candidate(row, [], "master", None, ["retrieval"])
        self.assertTrue(result["approved"], result["problems"])
        self.assertEqual(result["verified"]["supervisor_role_page_date"], "2020-01-01")
        self.assertTrue(any("current-cycle" in warning for warning in result["warnings"]))

    def test_old_dated_profile_warns_without_claiming_current_role(self):
        row = {"name": "刘勇", "school_domain": "zju.edu.cn",
               "official_profile_url": "https://cse.zju.edu.cn/person",
               "paper": {"title": "Robot Localization", "year": 2024}}
        page = "刘勇 时间：2011-03-03 教授、博士生导师 论文 Robot Localization, 2024"
        with patch.object(gate, "fetch_official", return_value=page):
            result = gate.verify_candidate(row, [], "doctor", None, ["SLAM"])
        self.assertTrue(result["approved"], result["problems"])
        self.assertEqual(result["verified"]["official_profile_page_date"], "2011-03-03")
        self.assertEqual(result["verified"]["current_faculty_and_cycle_status"], "not_verified_by_gate")
        self.assertTrue(any("current faculty and degree status" in warning
                            for warning in result["warnings"]))

    def test_generic_roster_without_profile_role_does_not_pass(self):
        row = {"name": "王伟行", "school_domain": "suda.edu.cn",
               "official_profile_url": "https://scst.suda.edu.cn/person",
               "official_supervisor_url": "https://scst.suda.edu.cn/11250/list.htm",
               "paper": {"title": "Face Anti-Spoofing", "year": 2023}}
        pages = {
            row["official_profile_url"]: "王伟行 苏州大学计算机科学与技术学院 讲师 论文 Face Anti-Spoofing, 2023",
            row["official_supervisor_url"]: "苏州大学计算机科学与技术学院 导师简介 王伟行",
        }
        with patch.object(gate, "fetch_official", side_effect=lambda url, domain: pages[url]):
            result = gate.verify_candidate(row, ["计算机科学与技术学院"], "master", None)
        self.assertFalse(result["approved"])
        self.assertTrue(any("supervisor evidence" in p for p in result["problems"]))

    def test_aminer_id_and_paper_attribution_path_passes(self):
        row = {"name": "刘志", "school_domain": "shu.edu.cn", "person_id": "p1",
               "official_profile_url": "https://cs.shu.edu.cn/liu", "school_names": ["Shanghai University"],
               "paper": {"title": "Detection Work", "year": 2024, "aminer_id": "paper1"}}
        with patch.object(gate, "fetch_official", return_value="刘志 上海大学计算机工程与科学学院 硕士生导师"):
            result = gate.verify_candidate(row, ["计算机工程与科学学院"], "master", FakeClient())
        self.assertTrue(result["approved"], result["problems"])
        self.assertEqual(result["verified"]["aminer_paper_id"], "paper1")

    def test_rejected_affiliation_is_not_marked_as_verified_paper(self):
        row = {"name": "刘志", "school_domain": "shu.edu.cn", "person_id": "p1",
               "official_profile_url": "https://cs.shu.edu.cn/liu", "school_names": ["Shanghai University"],
               "paper": {"title": "Detection Work", "year": 2024, "aminer_id": "paper1"}}
        with patch.object(gate, "fetch_official", return_value="刘志 上海大学计算机工程与科学学院 硕士生导师"):
            result = gate.verify_candidate(row, ["计算机工程与科学学院"], "master", WrongAffiliationClient())
        self.assertFalse(result["approved"])
        self.assertIn("paper author affiliation does not establish the requested university", result["problems"])
        self.assertNotIn("aminer_paper_id", result["verified"])

    def test_chinese_school_name_resolves_english_paper_affiliation(self):
        row = {"name": "刘志", "school_domain": "suda.edu.cn", "person_id": "p1",
               "official_profile_url": "https://scst.suda.edu.cn/liu", "school_names": ["苏州大学"],
               "paper": {"title": "Detection Work", "year": 2024, "aminer_id": "paper1"}}
        with patch.object(gate, "fetch_official", return_value="刘志 苏州大学计算机科学与技术学院 硕士生导师"):
            approved = gate.verify_candidate(row, ["计算机科学与技术学院"], "master", EnglishUniversityClient())
            wrong_id = gate.verify_candidate(row, ["计算机科学与技术学院"], "master", EnglishUniversityClient("other-school"))
        self.assertTrue(approved["approved"], approved["problems"])
        self.assertIn("canonical English name", approved["verified"]["paper_affiliation"])
        self.assertFalse(wrong_id["approved"])
        self.assertNotIn("aminer_paper_id", wrong_id["verified"])

    def test_canonical_university_abbreviation_requires_matching_id_and_name(self):
        row = {"name": "刘志", "school_domain": "suda.edu.cn", "person_id": "p1",
               "official_profile_url": "https://scst.suda.edu.cn/liu", "school_names": ["苏州大学"],
               "paper": {"title": "Detection Work", "year": 2024, "aminer_id": "paper1"}}
        page = "刘志 苏州大学计算机科学与技术学院 硕士生导师"
        with patch.object(gate, "fetch_official", return_value=page):
            approved = gate.verify_candidate(
                row, [], "master", EnglishUniversityClient(author_org="School of Computer Science, Soochow Univ"))
            wrong_id = gate.verify_candidate(
                row, [], "master", EnglishUniversityClient("other-school", "Soochow Univ"))
            wrong_name = gate.verify_candidate(
                row, [], "master", EnglishUniversityClient("school-suda", "Shanghai Univ"))
        self.assertTrue(approved["approved"], approved["problems"])
        self.assertIn("abbreviation", approved["verified"]["paper_affiliation"])
        for rejected in (wrong_id, wrong_name):
            self.assertFalse(rejected["approved"])
            self.assertIn("paper author affiliation does not establish the requested university", rejected["problems"])
            self.assertNotIn("aminer_paper_id", rejected["verified"])

    def test_missing_author_org_id_uses_normalized_school_name_text_with_warning(self):
        row = {"name": "刘志", "school_domain": "suda.edu.cn", "person_id": "p1",
               "official_profile_url": "https://scst.suda.edu.cn/liu", "school_names": ["苏州大学"],
               "paper": {"title": "Detection Work", "year": 2024, "aminer_id": "paper1"}}
        with patch.object(gate, "fetch_official", return_value="刘志 苏州大学计算机学院 硕士生导师"):
            approved = gate.verify_candidate(
                row, [], "master", EnglishUniversityClient(None, "Institute of AI, Soochow Univ"))
            wrong_school = gate.verify_candidate(
                row, [], "master", EnglishUniversityClient(None, "Institute of AI, Shanghai Univ"))
        self.assertTrue(approved["approved"], approved["problems"])
        self.assertIn("org ID absent", approved["verified"]["paper_affiliation"])
        self.assertTrue(any("org ID is absent" in warning for warning in approved["warnings"]))
        self.assertFalse(wrong_school["approved"])

    def test_single_school_name_string_is_not_split_into_characters(self):
        row = {"name": "刘志", "school_domain": "suda.edu.cn", "person_id": "p1",
               "official_profile_url": "https://scst.suda.edu.cn/liu", "school_names": "苏州大学",
               "paper": {"title": "Detection Work", "year": 2024, "aminer_id": "paper1"}}
        with patch.object(gate, "fetch_official", return_value="刘志 苏州大学计算机学院 硕士生导师"):
            result = gate.verify_candidate(row, [], "master", EnglishUniversityClient())
        self.assertTrue(result["approved"], result["problems"])
        self.assertEqual(gate.school_name_list(row), ["苏州大学"])

    def test_candidate_band_is_rejected_even_with_other_evidence(self):
        row = {"name": "王伟行", "school_domain": "suda.edu.cn", "band": "匹配",
               "official_profile_url": "https://scst.suda.edu.cn/person",
               "paper": {"title": "Face Anti-Spoofing", "year": 2023}}
        page = "王伟行 苏州大学计算机科学与技术学院 讲师 硕士生导师 代表作 Face Anti-Spoofing, 2023"
        with patch.object(gate, "fetch_official", return_value=page):
            result = gate.verify_candidate(row, ["计算机科学与技术学院"], "master", None)
        self.assertFalse(result["approved"])
        self.assertTrue(any("band" in p for p in result["problems"]))

    def test_nonuniversity_url_is_rejected(self):
        self.assertFalse(gate.official_host("https://evil.example/suda.edu.cn", "suda.edu.cn"))
        self.assertFalse(gate.official_host("https://scst.suda.edu.cn.evil.example", "suda.edu.cn"))
        self.assertTrue(gate.official_host("https://scst.suda.edu.cn/person", "suda.edu.cn"))

    def test_doctoral_title_alone_does_not_prove_master_eligibility(self):
        page = "张三 计算机学院 教授 博士生导师"
        self.assertFalse(gate.has_role_for_name(page, "张三", "master", roster=False))
        self.assertTrue(gate.has_role_for_name(page, "张三", "doctor", roster=False))
        self.assertTrue(gate.has_role_for_name(page + " 欢迎联系报考本人博士研究生和硕士研究生", "张三", "master", roster=False))

    def test_official_chinese_name_binds_english_aminer_id_through_profile(self):
        row = {"name": "韩婷婷", "school_domain": "hdu.edu.cn", "person_id": "h1",
               "official_profile_url": "https://faculty.hdu.edu.cn/jsjxy/htt/main.htm",
               "school_names": ["Hangzhou Dianzi University"],
               "paper": {"title": "HERO Detection", "year": 2026, "aminer_id": "hero"}}
        page = "计算机学院 韩婷婷 副教授 硕士生导师 论文 Tingting Han, HERO Detection, 2026"
        with patch.object(gate, "fetch_official", return_value=page):
            result = gate.verify_candidate(row, ["计算机学院"], "master", FakeEnglishClient())
        self.assertTrue(result["approved"], result["problems"])
        self.assertEqual(result["verified"]["canonical_name"], "韩婷婷")

    def test_official_paper_can_support_row_without_aminer_person_id(self):
        row = {"name": "韩婷婷", "school_domain": "hdu.edu.cn",
               "official_profile_url": "https://faculty.hdu.edu.cn/jsjxy/htt/main.htm",
               "paper": {"title": "HERO Detection", "year": 2026, "aminer_id": "hero"}}
        page = "计算机学院 韩婷婷 副教授 硕士生导师 论文 Tingting Han, HERO Detection, 2026"
        with patch.object(gate, "fetch_official", return_value=page):
            result = gate.verify_candidate(row, ["计算机学院"], "master", None)
        self.assertTrue(result["approved"], result["problems"])
        self.assertIn("paper_on_official_profile", result["verified"])


if __name__ == "__main__":
    unittest.main()
