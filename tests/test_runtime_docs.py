"""Runtime guides and skill copies must track installed parser contracts without running jobs."""
import ast
import contextlib
import io
from pathlib import Path
import re
import runpy
import shlex
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src/colabllm/lib'))
from colabsvc import build_parser, extract_instance_arg


class RuntimeDocumentationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = {p.stem: runpy.run_path(str(p)) for p in (ROOT/'src/colabllm/services').glob('*.py')
                       if p.with_suffix('.ipynb').is_file()}

    def test_every_runtime_has_synchronized_portable_guide_and_skill_index(self):
        sync = runpy.run_path(str(ROOT/'tools/sync_runtime_docs.py'))
        self.assertEqual(sync['synchronize'](check=True), len(self.modules))
        index = (ROOT/'skills/colabllm/SKILL.md').read_text()
        for name in self.modules:
            with self.subTest(runtime=name):
                self.assertIn(f'(references/{name}.md)', index)
                text = (ROOT/'docs'/(name+'.md')).read_text()
                self.assertIn('(runtime-common.md)', text)
                self.assertIn('colabllm '+name+' up', text)

    def test_documented_runtime_examples_parse_without_side_effects(self):
        counts = {name: 0 for name in self.modules}
        for name in self.modules:
            path = ROOT/'docs'/(name+'.md')
            for block in re.findall(r'```bash\n(.*?)```', path.read_text().replace('\\\n', ' '), re.S):
                for line in block.splitlines():
                    if not line.startswith('colabllm '):
                        continue
                    args = shlex.split(line, comments=True)
                    if len(args) < 3 or args[1] not in self.modules:
                        continue
                    module = self.modules[args[1]]
                    parser = build_parser(module['SERVICE'], module.get('configure_cli'))
                    _, remaining = extract_instance_arg(args[2:])
                    with self.subTest(file=path.name, example=line), contextlib.redirect_stdout(io.StringIO()):
                        try:
                            parser.parse_args(remaining)
                        except SystemExit as result:
                            self.assertEqual(result.code, 0, line)
                    counts[args[1]] += 1
        self.assertTrue(all(counts.values()), counts)

    def test_guides_have_valid_local_links_python_and_json(self):
        for name in [*self.modules, 'runtime-common']:
            path = ROOT/'docs'/(name+'.md')
            text = path.read_text()
            self.assertNotIn('443998' + '.xyz', text)
            self.assertNotIn('/home/' + 'eric', text)
            for link in re.findall(r'\]\(([^)]+)\)', text):
                if '://' in link:
                    continue
                self.assertTrue((path.parent/link.split('#')[0]).is_file(), link)
            for block in re.findall(r'```python\n(.*?)```', text, re.S):
                ast.parse(block, filename=path.name)
            import json
            for block in re.findall(r'```json\n(.*?)```', text, re.S):
                json.loads(block)
            for block in re.findall(r'```jsonl\n(.*?)```', text, re.S):
                for line in block.splitlines():
                    if line.strip():
                        json.loads(line)


if __name__ == '__main__':
    unittest.main()
