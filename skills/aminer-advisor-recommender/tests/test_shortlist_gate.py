import importlib.util
from pathlib import Path
import sys
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


class EnglishUniversityClient(FakeClient):
    def __init__(self, author_org_id="school-suda"):
        self.author_org_id = author_org_id

    def call(self, api, params):
        if api == "org_search":
            self_query = params["orgs"]
            assert self_query == ["苏州大学"]
            return {"success": True, "data": [{"org_id": "school-suda", "org_name": "Soochow University"}]}
        row = super().call(api, params)
        if api == "paper_detail":
            row["data"][0]["authors"][0].update(
                org="School of Computer Science, Soochow University", orgid=self.author_org_id)
        return row


class ShortlistGateTest(unittest.TestCase):
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
