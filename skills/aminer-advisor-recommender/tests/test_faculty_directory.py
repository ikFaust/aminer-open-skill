import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import faculty_directory as directory  # noqa: E402


class FacultyDirectoryTests(unittest.TestCase):
    def test_cli_reports_unavailable_official_page_without_traceback(self):
        out = io.StringIO()
        error = directory.urllib.error.HTTPError(
            "https://school.edu.cn/faculty", 503, "Service Unavailable", None, None)
        with (patch.object(directory, "main", side_effect=error), redirect_stdout(out)):
            directory.cli_main()
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["errors"], [{"type": "http", "status": 503}])
        self.assertIn("no faculty evidence", payload["scope"])

    def test_individual_profile_requires_exact_name_and_reads_combined_degree_role(self):
        profile = directory.Page()
        profile.feed('彭亚新 创建时间：2024-09-24 彭亚新，教授，硕士生/博士生导师。'
                     '研究方向：具身智能、机械臂感知。')
        with patch.object(directory, 'fetch', return_value=(
                'https://ai.shu.edu.cn/info/1073/3348.htm', profile)):
            row = directory.inspect_person(('彭亚新', 'https://ai.shu.edu.cn/info/1073/3348.htm'),
                                           ['具身智能'], require_name=True)
        self.assertEqual(row['source_page_published_on'], '2024-09-24')
        self.assertTrue(row['master_supervisor_explicit'])
        self.assertTrue(row['doctor_supervisor_explicit'])
        self.assertEqual(row['matched_terms'], ['具身智能'])

        wrong = directory.Page()
        wrong.feed('张三丰，教授。研究方向：具身智能。')
        with patch.object(directory, 'fetch', return_value=(
                'https://school.edu.cn/profile.htm', wrong)):
            row = directory.inspect_person(('张三', 'https://school.edu.cn/profile.htm'),
                                           ['具身智能'], require_name=True)
        self.assertIn('exact requested name', row['error'])

        with patch.object(directory, 'fetch', return_value=(
                'https://other.edu.cn/profile.htm', profile)):
            row = directory.inspect_person(('彭亚新', 'https://ai.shu.edu.cn/info/1073/3348.htm'),
                                           ['具身智能'], require_name=True)
        self.assertIn('outside the original university domain', row['error'])

    def test_english_faculty_page_exposes_date_and_doctoral_role(self):
        profile = directory.Page()
        profile.feed('Gaoang Wang-Zhejiang University Personal homepage. Last updated 2026-05-08. '
                     'Wang Gaoang Assistant Professor / Researcher | Doctoral supervisor. '
                     'Research: computer vision, motion modeling, generative models.')
        with patch.object(directory, 'fetch', return_value=(
                'https://person.zju.edu.cn/en/gaoangwang', profile)):
            row = directory.inspect_person(('Gaoang Wang', 'https://person.zju.edu.cn/en/gaoangwang'),
                                           ['motion modeling'], require_name=True)
        self.assertEqual(row['source_page_published_on'], '2026-05-08')
        self.assertTrue(row['doctor_supervisor_explicit'])
        self.assertIn('Assistant Professor', row['role_excerpt'])

    def test_named_search_falls_back_to_exact_links_on_non_webplus_roster(self):
        listing = directory.Page()
        listing.feed('<a href="/a/page.htm">张三</a><a href="/b/page.htm">李四</a>')
        profile = directory.Page()
        profile.feed('研究方向：机器人操作。')

        def fake_fetch(url):
            return url, listing if url.endswith('list.htm') else profile

        output = io.StringIO()
        with patch.object(directory, 'fetch', side_effect=fake_fetch), \
             patch.object(sys, 'argv', ['faculty_directory.py', '--url',
                                       'https://faculty.edu.cn/list.htm', '--terms', '机器人操作',
                                       '--names', '李四,王五']), redirect_stdout(output):
            directory.main()
        result = json.loads(output.getvalue())
        self.assertEqual(result['search_mode'], 'linked_official_roster_fallback')
        self.assertEqual(result['profiles_scanned'], 1)
        self.assertEqual([row['name'] for row in result['matches']], ['李四'])
        self.assertEqual(result['not_found_in_linked_roster'], ['王五'])

    def test_webplus_name_search_accepts_only_exact_name_on_university_host(self):
        listing = directory.Page()
        listing.raw_html = 'url: "/_wp3services/generalQuery?queryObj=teacherHome"'
        payload = io.BytesIO(json.dumps({"data": [
            {"title": "徐杭", "cnUrl": "http://faculty.hdu.edu.cn/txgxxy/xh2/main.htm",
             "department": "通信工程学院", "career": "助理研究员"},
            {"title": "徐杭明", "cnUrl": "http://faculty.hdu.edu.cn/other/main.htm"},
            {"title": "徐杭", "cnUrl": "https://other.edu.cn/false/main.htm"},
        ]}).encode())
        with patch.object(directory.urllib.request, 'urlopen', return_value=payload) as call:
            endpoint, hits, errors = directory.webplus_named_profiles(
                listing, 'https://faculty.hdu.edu.cn/jsgjcx/list.htm', ['徐杭'])
        self.assertEqual(endpoint, 'https://faculty.hdu.edu.cn/_wp3services/generalQuery?queryObj=teacherHome')
        self.assertEqual(hits, [{'name': '徐杭',
            'url': 'http://faculty.hdu.edu.cn/txgxxy/xh2/main.htm',
            'department': '通信工程学院', 'career_from_index': '助理研究员'}])
        self.assertEqual(errors, [])
        self.assertIn(b'%E5%BE%90%E6%9D%AD', call.call_args.args[0].data)

    def test_webplus_name_search_requires_official_endpoint_advertisement(self):
        listing = directory.Page()
        listing.raw_html = '<title>普通页面</title>'
        with patch.object(directory.urllib.request, 'urlopen') as call:
            with self.assertRaisesRegex(ValueError, 'does not advertise'):
                directory.webplus_named_profiles(listing,
                    'https://faculty.hdu.edu.cn/jsgjcx/list.htm', ['徐杭'])
        call.assert_not_called()

    def test_same_host_dynamic_teacher_roster_exposes_official_people(self):
        listing = directory.Page()
        listing.raw_html = '''$.ajax({url: 'https://sais.sjtu.edu.cn/active/ajax_teacher_list.html',
            data: {cat_id:'18',cat_code:'faculty',type:global_type}});'''
        content = '<a href="https://sais.sjtu.edu.cn/faculty/1611.html">白婷</a>'
        payload = io.BytesIO(json.dumps({"content": content}).encode())
        with patch.object(directory.urllib.request, 'urlopen', return_value=payload) as call:
            endpoint, roster = directory.dynamic_teacher_page(
                listing, 'https://sais.sjtu.edu.cn/faculty.html')
        self.assertEqual(endpoint, 'https://sais.sjtu.edu.cn/active/ajax_teacher_list.html')
        self.assertIn(b'cat_id=18', call.call_args.args[0].data)
        self.assertEqual(directory.teacher_links(roster, endpoint, endpoint),
                         [('白婷', 'https://sais.sjtu.edu.cn/faculty/1611.html')])

    def test_dynamic_teacher_endpoint_must_stay_on_directory_host(self):
        listing = directory.Page()
        listing.raw_html = '''$.ajax({url:'https://other.edu.cn/active/ajax_teacher_list.html',
            data:{cat_id:'18',cat_code:'faculty'}});'''
        with patch.object(directory.urllib.request, 'urlopen') as call:
            with self.assertRaisesRegex(ValueError, 'official directory host'):
                directory.dynamic_teacher_page(listing, 'https://sais.sjtu.edu.cn/faculty.html')
        call.assert_not_called()

    def test_dynamic_computer_science_teacher_links_use_jiaoshiml_path(self):
        roster = directory.Page()
        roster.feed('<a href="https://www.cs.sjtu.edu.cn/jiaoshiml/zangbinyu.html">臧斌宇</a>')
        root = 'https://www.cs.sjtu.edu.cn/jiaoshiml.html'
        self.assertEqual(directory.teacher_links(roster, root, root),
                         [('臧斌宇', 'https://www.cs.sjtu.edu.cn/jiaoshiml/zangbinyu.html')])

    def test_degree_cli_keeps_roster_count_without_flooding_output(self):
        found = {'roster': [{'name': f'人{i}', 'url': f'https://school.edu.cn/faculty/{i}.html'}
                            for i in range(15)], 'unit_directory_links': [], 'note': 'test'}
        out = io.StringIO()
        argv = ['faculty_directory.py', '--url', 'https://school.edu.cn/faculty.html',
                '--terms', '机器人', '--degree', 'doctor']
        with (patch.object(sys, 'argv', argv),
              patch.object(directory, 'scan', return_value=found),
              redirect_stdout(out)):
            directory.main()
        payload = json.loads(out.getvalue())
        self.assertEqual(payload['roster_count'], 15)
        self.assertEqual(len(payload['roster_sample']), 10)
        self.assertEqual(payload['omitted_roster'], 5)
        self.assertNotIn('roster', payload)

    def test_navigation_keyword_is_not_research_evidence(self):
        profile = directory.Page()
        profile.feed('信息与通信工程系 研究方向：工业控制系统安全 个人简介 光纤实验室友情链接')
        with patch.object(directory, 'fetch', return_value=('https://school.edu.cn/teacher/a.htm', profile)):
            row = directory.inspect_person(('甲乙', 'https://school.edu.cn/teacher/a.htm'), ['通信', '光纤'])
        self.assertEqual(row['matched_terms'], [])
        self.assertEqual(row['match_scope'], 'research_section')

    def test_repeated_portal_research_label_uses_biography_not_header(self):
        profile = directory.Page()
        profile.feed('研究方向：视觉感知 邮箱 手机访问 个人简介 教育经历 工作经历 '
                     '研究领域 教学与课程 徐杭，主要从事计算机视觉，包括目标检测、实例分割。')
        with patch.object(directory, 'fetch', return_value=('https://faculty.hdu.edu.cn/xh/main.htm', profile)):
            row = directory.inspect_person(('徐杭', 'https://faculty.hdu.edu.cn/xh/main.htm'),
                                           ['目标检测'])
        self.assertEqual(row['matched_terms'], ['目标检测'])

    def test_portal_bibliography_exposes_recent_task_paper_without_english_name_guess(self):
        profile = directory.Page()
        profile.feed('周晓飞 副教授 硕士生导师 研究领域：目标检测。论文与著作 '
                     '[1] X. Zhou，“Dense Attention Network for Object Detection,” IEEE 2022. '
                     '[2] X. Zhou，“IFENet: Interaction and Fusion for Object Detection,” IEEE 2025.')
        with patch.object(directory, 'fetch', return_value=('https://faculty.hdu.edu.cn/zxf/main.htm', profile)):
            row = directory.inspect_person(('周晓飞', 'https://faculty.hdu.edu.cn/zxf/main.htm'),
                                           ['Object Detection'])
        self.assertIn('IFENet', row['paper_excerpt'])
        self.assertIn('2025', row['paper_excerpt'])
        self.assertTrue(row['master_supervisor_explicit'])
        with patch.object(directory, 'fetch', return_value=('https://faculty.hdu.edu.cn/zxf/main.htm', profile)):
            chinese_terms = directory.inspect_person(('周晓飞', 'https://faculty.hdu.edu.cn/zxf/main.htm'),
                                                     ['目标检测'])
        self.assertIn('IFENet', chinese_terms['paper_excerpt'])

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

    def test_embedded_official_faculty_roster_yields_same_school_profiles(self):
        listing = directory.Page()
        listing.raw_html = ('<div id="showdatainfo"></div><script>'
                            'var ret = [{"showTitle":"魏晨","url":"../../info/1069/2361.htm"},'
                            '{"showTitle":"外站人物","url":"https://outside.edu.cn/profile.htm"}];'
                            '</script>')
        listing.feed(listing.raw_html)
        profile = directory.Page()
        profile.feed('魏晨 教授、博士生导师。研究方向：无人机集群与多智能体协同控制。')
        root = 'https://dept3.buaa.edu.cn'
        pages = {root + '/szdw/qtjs/znxtykzgcx.htm': listing,
                 root + '/info/1069/2361.htm': profile}
        with patch.object(directory, 'fetch', side_effect=lambda url: (url, pages[url])):
            result = directory.scan(root + '/szdw/qtjs/znxtykzgcx.htm', ['无人机集群'])
        self.assertEqual([row['name'] for row in result['roster']], ['魏晨'])
        self.assertEqual(result['matches'][0]['name'], '魏晨')
        self.assertEqual(result['dynamic_roster_sources'][0]['links_found'], 1)

    def test_teaching_research_org_index_reaches_college_without_home_news_noise(self):
        home = directory.Page()
        home.feed('<a href="/jgsz/jxkyjg02.htm">教学科研机构</a>'
                  '<a href="/szdw/lyys1.htm">师资队伍</a>'
                  '<a href="/info/1002/111.htm">头条新闻</a>')
        units = directory.Page()
        units.feed('<a href="https://auto.buaa.edu.cn/">自动化科学与电气工程学院</a>')
        generic = directory.Page()
        generic.feed('师资队伍')
        college = directory.Page()
        college.feed('<a href="/szdw/list.htm">在职教师</a>')
        roster = directory.Page()
        roster.feed('<a href="/wei/page.htm">魏晨</a>')
        profile = directory.Page()
        profile.feed('魏晨 教授。研究方向：无人机集群。')
        pages = {
            'https://www.buaa.edu.cn/': home,
            'https://www.buaa.edu.cn/jgsz/jxkyjg02.htm': units,
            'https://www.buaa.edu.cn/szdw/lyys1.htm': generic,
            'https://auto.buaa.edu.cn/': college,
            'https://auto.buaa.edu.cn/szdw/list.htm': roster,
            'https://auto.buaa.edu.cn/wei/page.htm': profile,
        }
        with patch.object(directory, 'fetch', side_effect=lambda url: (url, pages[url])):
            result = directory.scan('https://www.buaa.edu.cn/', ['无人机集群'],
                                    unit_terms=['自动化'])
        self.assertIn('https://auto.buaa.edu.cn/', result['directory_pages'])
        self.assertEqual([row['name'] for row in result['roster']], ['魏晨'])
        self.assertEqual(result['matches'][0]['name'], '魏晨')

    def test_named_college_is_scanned_before_broad_matching_college_and_generic_roster(self):
        home = directory.Page()
        home.feed('<a href="/units.htm">教学科研机构</a>'
                  '<a href="/faculty/list.htm">师资队伍</a>')
        units = directory.Page()
        units.feed('<a href="https://me.buaa.edu.cn/">机械工程及自动化学院</a>'
                   '<a href="https://dept3.buaa.edu.cn/">自动化科学与电气工程学院</a>')
        broad = directory.Page()
        broad.feed('<a href="/info/1001/1.htm">李甲</a>')
        target = directory.Page()
        target.feed('<a href="/info/1001/2.htm">魏晨</a>')
        profile = directory.Page()
        profile.feed('魏晨 教授。研究方向：无人机集群。')
        pages = {'https://www.buaa.edu.cn/': home,
                 'https://www.buaa.edu.cn/units.htm': units,
                 'https://www.buaa.edu.cn/faculty/list.htm': broad,
                 'https://me.buaa.edu.cn/': broad,
                 'https://dept3.buaa.edu.cn/': target,
                 'https://dept3.buaa.edu.cn/info/1001/2.htm': profile}
        with patch.object(directory, 'fetch', side_effect=lambda url: (url, pages[url])):
            result = directory.scan('https://www.buaa.edu.cn/', ['无人机集群'],
                                    max_profiles=1,
                                    unit_terms=['自动化科学与电气工程学院', '自动化'])
        self.assertEqual(result['roster'][0]['name'], '魏晨')
        self.assertEqual(result['matches'][0]['name'], '魏晨')

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
