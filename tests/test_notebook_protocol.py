"""Only complete protocol lines may alter readiness, including across byte boundaries."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/colabllm/lib'))
from protocol import NotebookProtocol


class NotebookProtocolTest(unittest.TestCase):
    def test_log_mentions_are_not_ready(self):
        parser = NotebookProtocol()
        result = parser.feed(b'ERROR: expected CT_READY but startup failed\nprint("CT_READY")\nNOT_CT_READY\n')
        self.assertNotIn('ready', result)
        self.assertNotIn('ready', parser.feed(b'CT_READY'))
        self.assertTrue(parser.feed(b'\n')['ready'])

    def test_split_utf8_ansi_and_markers(self):
        parser = NotebookProtocol()
        data = '\x1b[32mCT_READY\x1b[0m\r\nCT_PUBLIC_URL=https://test.invalid\nCT_CREDENTIALS user=中文 pass=fake\n[colab] Executing cell 6/6\n'.encode()
        result = {}
        for value in data:
            result.update(parser.feed(bytes([value])))
        self.assertEqual(result, dict(ready=True, url='https://test.invalid', login_user='中文', login_pass='fake', last_cell_started=True))

    def test_eof_and_long_log_line(self):
        parser = NotebookProtocol()
        self.assertNotIn('ready', parser.feed(b'X' * 70000))
        self.assertNotIn('ready', parser.feed(b'CT_READY\n'))
        self.assertTrue(parser.feed(b'CT_READY', final=True)['ready'])

    def test_preflight_crash_detected_only_before_first_cell(self):
        parser = NotebookProtocol()
        updates = parser.feed(
            (
                "[colab] Parsing notebook 'x.ipynb'...\n"
                "╭─── Traceback (most recent call last) ───╮\n"
                "│ /fake/colab_cli/commands/execution.py:235 in exec_command │\n"
                "╰───╯\n"
                "RuntimeError: Connection was lost.\n"
            ).encode()
        )
        self.assertEqual(updates, {})
        self.assertFalse(parser.cell_started)
        self.assertEqual(parser.preflight_error, 'RuntimeError: Connection was lost.')

        started = NotebookProtocol()
        started.feed(
            (
                "[colab] Executing cell 1/7 - ct-0...\n"
                "RuntimeError: Connection was lost.\n"
            ).encode()
        )
        self.assertTrue(started.cell_started)
        self.assertIsNone(started.preflight_error)


if __name__ == '__main__':
    unittest.main()
