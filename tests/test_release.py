"""Release tags must select the package version and its own changelog entry."""
from pathlib import Path
import runpy
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
release_notes = runpy.run_path(str(ROOT / 'tools/check_release.py'))['release_notes']


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'pyproject.toml').write_text('[project]\nversion = "0.2.0"\n')
        (self.root / 'CHANGELOG.md').write_text(
            '# Changelog\n\n## [Unreleased]\n\nFuture change\n\n'
            '## [0.2.0] - 2026-11-03\n\n- Release change\n\n'
            '## [0.1.0] - 2026-10-01\n\n- Older change\n')

    def test_extracts_only_selected_release(self):
        self.assertEqual(release_notes(self.root, 'v0.2.0'), '- Release change\n')

    def test_rejects_wrong_tag_or_branch(self):
        for tag in ('v0.1.0', '0.2.0', 'master', 'v0.2.0rc1'):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                release_notes(self.root, tag)

    def test_rejects_missing_empty_or_duplicate_changelog_entry(self):
        for text in ('# Changelog\n', '## [0.2.0] - 2026-11-03\n\n',
                     '## [0.2.0] - 2026-11-03\nA\n## [0.2.0] - 2026-11-03\nB\n'):
            with self.subTest(text=text):
                (self.root / 'CHANGELOG.md').write_text(text)
                with self.assertRaises(ValueError):
                    release_notes(self.root, 'v0.2.0')

    def test_rejects_noncanonical_version(self):
        (self.root / 'pyproject.toml').write_text('[project]\nversion = "0.02.0"\n')
        with self.assertRaises(ValueError):
            release_notes(self.root, 'v0.02.0')


if __name__ == '__main__':
    unittest.main()
