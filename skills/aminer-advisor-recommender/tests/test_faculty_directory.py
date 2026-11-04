import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import faculty_directory as directory  # noqa: E402


class FacultyDirectoryTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
