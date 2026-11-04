"""The user skill is complete, relocatable and exports without overwriting agent files."""
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src/colabllm/lib'))
from cli_support import CliError
from skill_bundle import bundle_root, export_skill


class SkillBundleTest(unittest.TestCase):
    def test_reference_links_resolve_inside_the_standalone_skill(self):
        root = bundle_root()
        self.assertEqual(root, ROOT/'skills/colabllm')
        for path in root.rglob('*.md'):
            text = path.read_text()
            self.assertNotRegex(text, r'\bsk-[A-Za-z0-9]{20,}')
            self.assertNotIn('/home/' + 'eric', text)
            for target in re.findall(r'\]\(([^)]+)\)', text):
                if '://' in target:
                    continue
                resolved = (path.parent / target.split('#')[0]).resolve()
                self.assertTrue(resolved.is_relative_to(root.resolve()), target)
                self.assertTrue(resolved.is_file(), target)

    def test_export_copies_references_and_refuses_existing_target(self):
        with tempfile.TemporaryDirectory() as temp:
            target = export_skill(Path(temp)/'skills')
            self.assertEqual(target, Path(temp).resolve()/'skills/colabllm')
            self.assertEqual((target/'SKILL.md').read_bytes(), (bundle_root()/'SKILL.md').read_bytes())
            self.assertEqual(
                {p.name for p in (target/'references').glob('*.md')},
                {p.name for p in (bundle_root()/'references').glob('*.md')})
            (target/'SKILL.md').write_text('user customization')
            with self.assertRaises(CliError):
                export_skill(target.parent)
            self.assertEqual((target/'SKILL.md').read_text(), 'user customization')


if __name__ == '__main__':
    unittest.main()
