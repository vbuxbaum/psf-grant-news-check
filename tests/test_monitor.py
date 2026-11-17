import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import monitor


def page(text="Applications open.", href="/apply/", extra=""):
    return f"""<html><nav>{extra}</nav><section class="main-content">
    <article class="text"><h1>PSF Grants Program</h1>
    <h1 id="table-of-contents">Table of Contents</h1><ul><li>{extra}</li></ul>
    <p>{text}</p><p>{'Grant eligibility and application requirements. ' * 15}</p>
    <p><a href="{href}">Apply here</a></p>
    <table><tr><td>Deadline</td><td>August 25</td></tr></table>
    <script>{extra}</script></article></section><footer>{extra}</footer></html>"""


class ExtractionTests(unittest.TestCase):
    def test_ignores_navigation_toc_scripts_and_footer(self):
        self.assertEqual(monitor.extract_content(page(extra="old")),
                         monitor.extract_content(page(extra="new")))

    def test_ignores_whitespace_and_inline_formatting(self):
        self.assertEqual(monitor.extract_content(page()), monitor.extract_content(
            page("Applications\n    <strong>open</strong>.")))

    def test_ignores_html_comments(self):
        self.assertEqual(monitor.extract_content(page()), monitor.extract_content(
            page("Applications <!-- deployment 123 -->open.")))

    def test_detects_text_links_and_table_changes(self):
        original = monitor.extract_content(page())
        for changed in [page("Applications closed."), page(href="/new-form/"),
                        page().replace("August 25", "September 1")]:
            with self.subTest(changed=changed):
                self.assertNotEqual(original, monitor.extract_content(changed))

    def test_resolves_relative_links(self):
        self.assertIn("https://www.python.org/apply/", monitor.extract_content(page()))

    def test_rejects_missing_or_empty_article(self):
        for html in ["<h1>Service unavailable</h1>",
                     '<section class="main-content"><article class="text">Grants</article></section>']:
            with self.subTest(html=html), self.assertRaises(ValueError):
                monitor.extract_content(html)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "snapshot.json"
        self.client = Mock()
        self.client.publish.return_value = "https://github.com/apyb/comunidade/discussions/1"
        self.before = monitor.extract_content(page())
        self.after = monitor.extract_content(page("Applications closed."))

    def baseline(self):
        monitor.check(self.path, self.before, self.client)
        return self.path.read_bytes()

    def test_baseline_and_unchanged_do_not_notify_or_rewrite(self):
        baseline = self.baseline()
        monitor.check(self.path, self.before, self.client)
        self.assertEqual(baseline, self.path.read_bytes())
        self.client.publish.assert_not_called()

    def test_changed_content_publishes_diff_then_saves(self):
        self.baseline()
        monitor.check(self.path, self.after, self.client)
        self.client.publish.assert_called_once()
        body = self.client.publish.call_args.args[1]
        self.assertIn("-Applications open.", body)
        self.assertIn("+Applications closed.", body)
        self.assertEqual(monitor.load_state(self.path)["content"], self.after)

    def test_publication_failure_preserves_state(self):
        baseline = self.baseline()
        self.client.publish.side_effect = TimeoutError("timeout")
        with self.assertRaises(TimeoutError):
            monitor.check(self.path, self.after, self.client)
        self.assertEqual(baseline, self.path.read_bytes())

    def test_dry_run_does_not_publish_or_save(self):
        monitor.check(self.path, self.before, dry_run=True)
        self.assertFalse(self.path.exists())
        baseline = self.baseline()
        monitor.check(self.path, self.after, self.client, dry_run=True)
        self.assertEqual(baseline, self.path.read_bytes())
        self.client.publish.assert_not_called()

    def test_corrupt_snapshot_is_not_silently_reinitialized(self):
        self.path.write_text('{"version": 1}')
        with self.assertRaises(ValueError):
            monitor.check(self.path, self.after, self.client)
        self.client.publish.assert_not_called()

    def test_retry_after_failed_state_save_reuses_marker(self):
        self.baseline()
        with patch.object(Path, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                monitor.check(self.path, self.after, self.client)
        first_marker = self.client.publish.call_args.args[2]
        monitor.check(self.path, self.after, self.client)
        self.assertEqual(first_marker, self.client.publish.call_args.args[2])

    def test_repeated_transition_is_a_new_event(self):
        self.baseline()
        monitor.check(self.path, self.after, self.client)
        first_marker = self.client.publish.call_args.args[2]
        monitor.check(self.path, self.before, self.client)
        monitor.check(self.path, self.after, self.client)
        self.assertNotEqual(first_marker, self.client.publish.call_args.args[2])


class GitHubTests(unittest.TestCase):
    def setUp(self):
        self.client = monitor.GitHub("fake-token", "apyb/comunidade", "Geral")

    def test_duplicate_lookup_paginates_without_creating(self):
        self.client.graphql = Mock(side_effect=[
            {"repository": {"discussions": {"nodes": [{"body": "other", "url": "other"}],
             "pageInfo": {"hasNextPage": True, "endCursor": "page2"}}}},
            {"repository": {"discussions": {"nodes": [{"body": "<!-- marker -->", "url": "found"}],
             "pageInfo": {"hasNextPage": False, "endCursor": None}}}},
        ])
        self.assertEqual(self.client.publish("title", "body", "<!-- marker -->"), "found")
        self.assertEqual(self.client.graphql.call_count, 2)
        self.assertEqual(self.client.graphql.call_args.args[1]["cursor"], "page2")

    def test_create_uses_resolved_category_and_repository(self):
        self.client.find_existing = Mock(return_value=None)
        self.client.graphql = Mock(side_effect=[
            {"repository": {"id": "repo-id", "discussionCategories": {
                "nodes": [{"name": "Geral", "id": "category-id"}]}}},
            {"createDiscussion": {"discussion": {"url": "created"}}},
        ])
        self.assertEqual(self.client.publish("title", "body", "marker"), "created")
        self.assertEqual(self.client.graphql.call_args.args[1]["input"],
                         {"repositoryId": "repo-id", "categoryId": "category-id",
                          "title": "title", "body": "body"})

    def test_unknown_category_fails(self):
        self.client.graphql = Mock(return_value={"repository": {
            "id": "repo-id", "discussionCategories": {"nodes": []}}})
        with self.assertRaisesRegex(ValueError, "Categoria"):
            self.client.destination()

    def test_graphql_errors_are_failures_even_with_http_success(self):
        response = Mock()
        response.read.return_value = json.dumps({"errors": [{"message": "Forbidden"}]}).encode()
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        with patch("monitor.urlopen", return_value=context), self.assertRaises(ValueError):
            self.client.graphql("query { viewer { login } }", {})

    def test_diff_is_bounded_and_cannot_close_its_fence(self):
        body = monitor.discussion_body("old", "```\n" + "á" * 60000, "<!-- marker -->", "now")
        self.assertLess(len(body.encode()), 60000)
        self.assertIn("````diff", body)
        self.assertIn("truncado", body)
        self.assertTrue(body.endswith("<!-- marker -->\n"))


if __name__ == "__main__":
    unittest.main()
