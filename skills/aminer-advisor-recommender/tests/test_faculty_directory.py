import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import faculty_directory as directory  # noqa: E402


class FacultyDirectoryTests(unittest.TestCase):
    def test_navigation_keyword_is_not_research_evidence(self):
        profile = directory.Page()
        profile.feed('信息与通信工程系 研究方向：工业控制系统安全 个人简介 光纤实验室友情链接')
        with patch.object(directory, 'fetch', return_value=('https://school.edu.cn/teacher/a.htm', profile)):
            row = directory.inspect_person(('甲乙', 'https://school.edu.cn/teacher/a.htm'), ['通信', '光纤'])
        self.assertEqual(row['matched_terms'], [])
        self.assertEqual(row['match_scope'], 'research_section')

    def test_related_university_subdomain_is_allowed_but_other_school_is_not(self):
        root = 'https://imo.nankai.edu.cn/sz/zzjs.htm'
        self.assertTrue(directory.same_university_domain('https://ceo.nankai.edu.cn/szll/a.htm', root))
        self.assertFalse(directory.same_university_domain('https://ceo.other.edu.cn/szll/a.htm', root))
        page = directory.Page()
        page.feed('<a href="https://ceo.nankai.edu.cn/szll/xdgxyjs/wz.htm">王志</a>'
                  '<a href="javascript:void(0)">在职教师</a>'
                  '<a href="/index.htm">首页</a>')
        self.assertEqual(directory.teacher_links(page, root, root),
                         [('王志', 'https://ceo.nankai.edu.cn/szll/xdgxyjs/wz.htm')])
        self.assertIsNone(directory.public_link(root, ' javascript:void(0)'))

    def test_college_link_to_institute_home_reaches_current_faculty_roster(self):
        college = directory.Page()
        college.feed('<a href="https://imo.nankai.edu.cn/">现代光学研究所主页</a>')
        institute = directory.Page()
        institute.feed('<a href="/sz/zzjs.htm">在职教师</a>')
        roster = directory.Page()
        roster.feed('<a href="https://ceo.nankai.edu.cn/szll/xdgxyjs/lb.htm">刘波</a>')
        profile = directory.Page()
        profile.feed('刘波 教授，博士生导师。研究方向：现代光通信和光电子技术。')
        pages = {
            'https://ceo.nankai.edu.cn/': college,
            'https://imo.nankai.edu.cn/': institute,
            'https://imo.nankai.edu.cn/sz/zzjs.htm': roster,
            'https://ceo.nankai.edu.cn/szll/xdgxyjs/lb.htm': profile,
        }
        with patch.object(directory, 'fetch', side_effect=lambda url: (url, pages[url])):
            result = directory.scan('https://ceo.nankai.edu.cn/', ['光通信'], degree='doctor')
        self.assertEqual(result['roster'], [
            {'name': '刘波', 'url': 'https://ceo.nankai.edu.cn/szll/xdgxyjs/lb.htm'}])
        self.assertIn('https://imo.nankai.edu.cn/sz/zzjs.htm', result['directory_pages'])
        self.assertEqual(result['matches'][0]['name'], '刘波')

    def test_university_professional_colleges_link_reaches_institute_roster(self):
        home = directory.Page()
        home.feed('<a href="/213/list.htm">专业学院</a>')
        units = directory.Page()
        units.feed('<a href="https://ceo.nankai.edu.cn/">电子信息与光学工程学院</a>')
        college = directory.Page()
        college.feed('<a href="https://imo.nankai.edu.cn/">现代光学研究所主页</a>')
        institute = directory.Page()
        institute.feed('<a href="/sz/zzjs.htm">在职教师</a>')
        roster = directory.Page()
        roster.feed('<a href="https://ceo.nankai.edu.cn/szll/xdgxyjs/lb.htm">刘波</a>')
        profile = directory.Page()
        profile.feed('刘波 研究方向：现代光通信和光电子技术。教授，博士生导师。')
        pages = {'https://www.nankai.edu.cn/': home,
                 'https://www.nankai.edu.cn/213/list.htm': units,
                 'https://ceo.nankai.edu.cn/': college,
                 'https://imo.nankai.edu.cn/': institute,
                 'https://imo.nankai.edu.cn/sz/zzjs.htm': roster,
                 'https://ceo.nankai.edu.cn/szll/xdgxyjs/lb.htm': profile}
        with patch.object(directory, 'fetch', side_effect=lambda url: (url, pages[url])):
            result = directory.scan('https://www.nankai.edu.cn/', ['光通信'],
                                    degree='doctor', unit_terms=['光学'])
        self.assertEqual([row['name'] for row in result['roster']], ['刘波'])
        self.assertIn('https://imo.nankai.edu.cn/sz/zzjs.htm', result['directory_pages'])

    def test_teacher_team_institute_and_info_profile_are_followed(self):
        home = directory.Page()
        home.feed('<a href="/szdw/jsdw1/ayjscz/kzyjcyjs.htm">教师队伍</a>')
        team = directory.Page()
        team.feed('<a href="/szdw/jsdw1/ayjscz/xxclyjs.htm">信息处理研究所</a>'
                  '<a href="/info/1062/4282.htm">热烈祝贺刘云浩教授当选院士</a>')
        institute = directory.Page()
        institute.feed('<a href="/info/1077/2329.htm">鲁继文</a>')
        profile = directory.Page()
        profile.feed('鲁继文 教授、博士生导师。研究方向：机器视觉感知、点云表征。'
                     '代表作 Point-BERT: Pre-training 3D Point Cloud Transformers (2022)。')
        root = 'https://www.au.tsinghua.edu.cn'
        pages = {root + '/': home,
                 root + '/szdw/jsdw1/ayjscz/kzyjcyjs.htm': team,
                 root + '/szdw/jsdw1/ayjscz/xxclyjs.htm': institute,
                 root + '/info/1077/2329.htm': profile}
        with patch.object(directory, 'fetch', side_effect=lambda url: (url, pages[url])):
            result = directory.scan(root + '/', ['点云'], degree='doctor')
        self.assertEqual([row['name'] for row in result['roster']], ['鲁继文'])
        self.assertEqual(result['official_role_paper_leads'][0]['name'], '鲁继文')

    def test_university_unit_index_can_reach_selected_college_directory(self):
        home = directory.Page()
        home.feed('<a href="/branch/ybsz.jsp">院部设置</a>')
        units = directory.Page()
        units.feed('<a href="https://scst.suda.edu.cn/">计算机科学与技术学院 软件学院</a>'
                   '<a href="https://med.suda.edu.cn/">医学部</a>'
                   '<a href="/news/meeting.htm">计算机科学与技术学院学术报告：最新研究成果</a>')
        college = directory.Page()
        college.feed('<a href="/teachers/list.htm">师资队伍</a>')
        listing = directory.Page()
        listing.feed('<a href="/cao/page.htm">曹敏</a>')
        profile = directory.Page()
        profile.feed('曹敏 研究方向：图文检索。硕士生导师。')
        pages = {
            'https://www.suda.edu.cn/': home,
            'https://www.suda.edu.cn/branch/ybsz.jsp': units,
            'https://scst.suda.edu.cn/': college,
            'https://scst.suda.edu.cn/teachers/list.htm': listing,
            'https://scst.suda.edu.cn/cao/page.htm': profile,
        }
        with patch.object(directory, 'fetch', side_effect=lambda url: (url, pages[url])):
            result = directory.scan('https://www.suda.edu.cn/', ['图文检索'],
                                    degree='master', unit_terms=['计算机'])
        self.assertEqual([row['name'] for row in result['roster']], ['曹敏'])
        self.assertIn({'label': '计算机科学与技术学院 软件学院', 'url': 'https://scst.suda.edu.cn/'},
                      result['unit_directory_links'])
        self.assertNotIn('https://med.suda.edu.cn/', result['directory_pages'])

    def test_unavailable_college_host_falls_back_to_official_university_root(self):
        home = directory.Page()
        home.feed('<a href="/branch/ybsz.jsp">院部设置</a>')
        units = directory.Page()
        units.feed('<a href="https://scst.suda.edu.cn/">计算机科学与技术学院</a>')
        college = directory.Page()
        college.feed('<a href="/teachers/list.htm">师资队伍</a>')
        listing = directory.Page()
        listing.feed('<a href="/cao/page.htm">曹敏</a>')
        profile = directory.Page()
        profile.feed('曹敏 硕士生导师。研究方向：图文检索。')
        pages = {'https://www.suda.edu.cn/': home,
                 'https://www.suda.edu.cn/branch/ybsz.jsp': units,
                 'https://scst.suda.edu.cn/': college,
                 'https://scst.suda.edu.cn/teachers/list.htm': listing,
                 'https://scst.suda.edu.cn/cao/page.htm': profile}

        def fake_fetch(url):
            if url == 'https://cst.suda.edu.cn':
                raise OSError('unavailable guessed college host')
            return url, pages[url]

        with patch.object(directory, 'fetch', side_effect=fake_fetch):
            result = directory.scan('https://cst.suda.edu.cn', ['图文检索'],
                                    degree='master', unit_terms=['计算机'])
        self.assertEqual(result['fallback_from'], 'https://cst.suda.edu.cn')
        self.assertEqual([row['name'] for row in result['roster']], ['曹敏'])

    def test_https_college_redirect_to_generic_root_retries_same_host_http(self):
        root = directory.Page()
        root.feed('浙江大学 首页')
        college = directory.Page()
        college.feed('<a href="/faculty/list.htm">师资队伍</a>')
        teachers = directory.Page()
        teachers.feed('<a href="/teacher/liu.htm">刘勇</a>')
        profile = directory.Page()
        profile.feed('刘勇 教授、博士生导师。研究方向：机器人感知与 SLAM。')
        pages = {
            'https://www.cse.zju.edu.cn/': ('https://www.zju.edu.cn/', root),
            'http://www.cse.zju.edu.cn/': ('http://www.cse.zju.edu.cn/', college),
            'http://www.cse.zju.edu.cn/faculty/list.htm':
                ('http://www.cse.zju.edu.cn/faculty/list.htm', teachers),
            'http://www.cse.zju.edu.cn/teacher/liu.htm':
                ('http://www.cse.zju.edu.cn/teacher/liu.htm', profile),
        }
        with patch.object(directory, 'fetch', side_effect=lambda url: pages[url]):
            result = directory.scan('https://www.cse.zju.edu.cn/', ['SLAM'], degree='doctor')
        self.assertEqual(result['http_retry_from'], 'https://www.cse.zju.edu.cn/')
        self.assertEqual(result['fallback_from'], '')
        self.assertIn('http://www.cse.zju.edu.cn/faculty/list.htm', result['directory_pages'])
        self.assertEqual([row['name'] for row in result['roster']], ['刘勇'])

    def test_follows_teacher_listing_and_keeps_exact_page_urls(self):
        landing = directory.Page()
        landing.feed('<a href="/teachers/list.htm">- 教师</a>')
        listing = directory.Page()
        listing.feed('<a href="/2026/c1/page.htm">李高峰</a><a href="/2026/c2/page.htm">王越</a>'
                     '<a href="https://other.edu.cn/p/page.htm">外校人</a>')
        first = directory.Page()
        first.feed('研究主题：机器人操作；模仿学习。教授、博士生导师。')
        second = directory.Page()
        second.feed('研究主题：机器人导航。教授。')

        def fake_fetch(url):
            pages = {
                'http://faculty.edu.cn/ys/list.htm': landing,
                'http://faculty.edu.cn/teachers/list.htm': listing,
                'http://faculty.edu.cn/2026/c1/page.htm': first,
                'http://faculty.edu.cn/2026/c2/page.htm': second,
            }
            return url, pages[url]

        with patch.object(directory, "fetch", side_effect=fake_fetch):
            result = directory.scan('http://faculty.edu.cn/ys/list.htm', ['机器人', '操作', '模仿学习'])
        self.assertEqual(result['source'], 'http://faculty.edu.cn/teachers/list.htm')
        self.assertEqual(result['links_found'], 2)
        self.assertEqual([x['name'] for x in result['roster']], ['李高峰', '王越'])
        self.assertEqual([x['name'] for x in result['matches']], ['李高峰', '王越'])
        self.assertIn('机器人操作', result['matches'][0]['snippets']['操作'])

    def test_cap_is_reported_without_claiming_complete_roster(self):
        listing = directory.Page()
        listing.feed('<a href="/a/page.htm">张三</a><a href="/b/page.htm">李四</a>')
        profile = directory.Page()
        profile.feed('研究方向：机器人。')

        def fake_fetch(url):
            return url, listing if url.endswith('list.htm') else profile

        with patch.object(directory, "fetch", side_effect=fake_fetch):
            result = directory.scan('http://faculty.edu.cn/list.htm', ['机器人'], max_profiles=1)
        self.assertEqual(result['links_found'], 2)
        self.assertEqual(result['profiles_scanned'], 1)
        self.assertEqual(result['unscanned_links'], 1)
        self.assertEqual(len(result['roster']), 2)

    def test_follows_nested_faculty_categories_without_treating_categories_as_people(self):
        home = directory.Page()
        home.feed('<a href="/szll/intro.htm">师资力量</a>')
        intro = directory.Page()
        intro.feed('<a href="/szll/intro.htm">院士风采</a>'
                   '<a href="/szll/mentors.htm">硕博导师</a>'
                   '<a href="/szll/optics.htm">现代光学研究所</a>')
        mentors = directory.Page()
        mentors.feed('<a href="/szll/intro.htm">院士风采</a>')
        optics = directory.Page()
        optics.feed('<a href="/szll/optics/one.htm">王志</a>'
                    '<a href="/szll/optics/two.htm">刘艳格</a>')
        person = directory.Page()
        person.feed('研究方向：光纤通信。教授。')
        pages = {'https://school.edu.cn/': home,
                 'https://school.edu.cn/szll/intro.htm': intro,
                 'https://school.edu.cn/szll/mentors.htm': mentors,
                 'https://school.edu.cn/szll/optics.htm': optics,
                 'https://school.edu.cn/szll/optics/one.htm': person,
                 'https://school.edu.cn/szll/optics/two.htm': person}
        with patch.object(directory, 'fetch', side_effect=lambda url: (url, pages[url])):
            result = directory.scan('https://school.edu.cn/', ['光纤通信'])
        self.assertEqual(result['source'], 'https://school.edu.cn/szll/optics.htm')
        self.assertEqual([x['name'] for x in result['roster']], ['王志', '刘艳格'])
        self.assertEqual(result['matches_found'], 2)

    def test_suda_cms_card_extracts_name_from_label_after_faculty_categories(self):
        home = directory.Page()
        home.feed('<a href="/11250/list.htm">导师简介</a>')
        categories = directory.Page()
        categories.feed('<a href="/30767/list.htm">教授</a>')
        listing = directory.Page()
        listing.feed('<a href="/0e/8d/c30767a528013/page.htm">张莉 江苏省杰青 zhangliml@suda.edu.cn</a>')
        profile = directory.Page()
        profile.feed('研究方向：计算机视觉、目标检测。教授。')
        pages = {'https://scst.suda.edu.cn/': home,
                 'https://scst.suda.edu.cn/11250/list.htm': categories,
                 'https://scst.suda.edu.cn/30767/list.htm': listing,
                 'https://scst.suda.edu.cn/0e/8d/c30767a528013/page.htm': profile}
        with patch.object(directory, 'fetch', side_effect=lambda url: (url, pages[url])):
            result = directory.scan('https://scst.suda.edu.cn/', ['计算机视觉', '目标检测'])
        self.assertEqual(result['roster'], [{
            'name': '张莉', 'url': 'https://scst.suda.edu.cn/0e/8d/c30767a528013/page.htm'}])
        self.assertEqual(result['matches'][0]['matched_terms'], ['计算机视觉', '目标检测'])

    def test_hdu_faculty_subdomain_profile_is_not_lost(self):
        home = directory.Page()
        home.feed('<a href="/12264/list.htm">师资队伍</a>')
        categories = directory.Page()
        categories.feed('<a href="/12289/list.htm">硕导简介</a>')
        listing = directory.Page()
        listing.feed('<a href="https://faculty.hdu.edu.cn/jsjxy/grs/main.htm">顾人舒</a>'
                     '<a href="/12289/list3.htm">尾页</a>')
        second_page = directory.Page()
        third_page = directory.Page()
        third_page.feed('<a href="https://faculty.hdu.edu.cn/jsjxy/yj/main.htm">俞俊</a>')
        profile = directory.Page()
        profile.feed('研究方向：计算机视觉。硕士生导师。')
        pages = {'https://computer.hdu.edu.cn/': home,
                 'https://computer.hdu.edu.cn/12264/list.htm': categories,
                 'https://computer.hdu.edu.cn/12289/list.htm': listing,
                 'https://computer.hdu.edu.cn/12289/list2.htm': second_page,
                 'https://computer.hdu.edu.cn/12289/list3.htm': third_page,
                 'https://faculty.hdu.edu.cn/jsjxy/grs/main.htm': profile,
                 'https://faculty.hdu.edu.cn/jsjxy/yj/main.htm': profile}
        with patch.object(directory, 'fetch', side_effect=lambda url: (url, pages[url])):
            result = directory.scan('https://computer.hdu.edu.cn/', ['计算机视觉'])
        self.assertEqual(result['roster'], [
            {'name': '顾人舒', 'url': 'https://faculty.hdu.edu.cn/jsjxy/grs/main.htm'},
            {'name': '俞俊', 'url': 'https://faculty.hdu.edu.cn/jsjxy/yj/main.htm'}])

    def test_master_role_and_official_paper_excerpt_are_visible_and_prioritized(self):
        listing = directory.Page()
        listing.feed('<a href="/a/page.htm">甲乙</a><a href="/b/page.htm">丙丁</a>')
        doctoral = directory.Page()
        doctoral.feed('甲乙 研究方向：计算机视觉、目标检测。博士生导师。')
        masters = directory.Page()
        masters.feed('丙丁 研究方向：计算机视觉。硕士生导师。代表作 Vision Detection, 2023.')
        pages = {'https://school.edu.cn/list.htm': listing,
                 'https://school.edu.cn/a/page.htm': doctoral,
                 'https://school.edu.cn/b/page.htm': masters}
        with patch.object(directory, 'fetch', side_effect=lambda url: (url, pages[url])):
            result = directory.scan('https://school.edu.cn/list.htm',
                                    ['计算机视觉', '目标检测'], degree='master')
        self.assertEqual(result['matches'][0]['name'], '丙丁')
        self.assertTrue(result['matches'][0]['master_supervisor_explicit'])
        self.assertIn('Vision Detection, 2023', result['matches'][0]['paper_excerpt'])
        self.assertFalse(result['matches'][1]['master_supervisor_explicit'])
        self.assertEqual([x['name'] for x in result['official_role_paper_leads']], ['丙丁'])

    def test_same_person_split_role_and_paper_pages_are_joined_by_homepage(self):
        listing = directory.Page()
        listing.feed('<a href="/old/page.htm">曹敏</a><a href="/new/page.htm">曹敏</a>')
        old = directory.Page()
        old.feed('曹敏 讲师 硕士生导师 研究方向：多模态学习。个人主页：http://web.suda.edu.cn/caomin/')
        new = directory.Page()
        new.feed('曹敏 副教授 研究方向：视觉-语言多模态学习。个人主页：web.suda.edu.cn/caomin/ '
                 '代表作 Vision-Grounded Text-Based Person Retrieval, 2026.')
        pages = {'https://scst.suda.edu.cn/list.htm': listing,
                 'https://scst.suda.edu.cn/old/page.htm': old,
                 'https://scst.suda.edu.cn/new/page.htm': new}
        with patch.object(directory, 'fetch', side_effect=lambda url: (url, pages[url])):
            result = directory.scan('https://scst.suda.edu.cn/list.htm', ['多模态'], degree='master')
        self.assertEqual(result['official_split_evidence_leads'], [{
            'name': '曹敏', 'personal_homepage_key': 'web.suda.edu.cn/caomin',
            'official_profile_url': 'https://scst.suda.edu.cn/new/page.htm',
            'official_supervisor_url': 'https://scst.suda.edu.cn/old/page.htm',
            'paper_excerpt': '代表作 Vision-Grounded Text-Based Person Retrieval, 2026.',
            'matched_terms': ['多模态'],
        }])

    def test_same_name_different_homepages_are_not_joined(self):
        listing = directory.Page()
        listing.feed('<a href="/old/page.htm">曹敏</a><a href="/new/page.htm">曹敏</a>')
        old = directory.Page()
        old.feed('曹敏 硕士生导师 研究方向：多模态。个人主页：http://web.suda.edu.cn/caomin/')
        other = directory.Page()
        other.feed('曹敏 研究方向：多模态。个人主页：http://web.suda.edu.cn/othercao/ '
                   '代表作 Text-Based Person Retrieval, 2026.')
        pages = {'https://scst.suda.edu.cn/list.htm': listing,
                 'https://scst.suda.edu.cn/old/page.htm': old,
                 'https://scst.suda.edu.cn/new/page.htm': other}
        with patch.object(directory, 'fetch', side_effect=lambda url: (url, pages[url])):
            result = directory.scan('https://scst.suda.edu.cn/list.htm', ['多模态'], degree='master')
        self.assertEqual(result['official_split_evidence_leads'], [])

    def test_role_with_exact_topic_but_undated_paper_is_kept_for_followup(self):
        listing = directory.Page()
        listing.feed('<a href="/a/page.htm">姚望舒</a>')
        profile = directory.Page()
        profile.feed('姚望舒 研究方向：目标检测、机器视觉。副教授，硕士生导师。代表作 Filter Pruning。')
        pages = {'https://school.edu.cn/list.htm': listing,
                 'https://school.edu.cn/a/page.htm': profile}
        with patch.object(directory, 'fetch', side_effect=lambda url: (url, pages[url])):
            result = directory.scan('https://school.edu.cn/list.htm', ['目标检测'], degree='master')
        self.assertEqual([x['name'] for x in result['official_role_without_dated_paper']], ['姚望舒'])
        self.assertEqual(result['official_role_paper_leads'], [])


if __name__ == '__main__':
    unittest.main()
