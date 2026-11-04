"""Pure helpers used by service CLIs: segment planning, timeout semantics, endpoint safety."""
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/colabllm/lib"))

import avatar_sampling  # noqa: E402
import cli_support  # noqa: E402
import nemotron_audio  # noqa: E402


class HelperTest(unittest.TestCase):
    def test_plan_segments_covers_full_audio(self):
        parts, target, duration = avatar_sampling.plan_segments(82.037619, 93, 1, True)
        self.assertEqual(len(parts), 26)
        self.assertGreaterEqual(parts[0] + sum(n - 13 for n in parts[1:]), target)
        self.assertTrue(all(n % 4 == 1 and 17 <= n <= 93 for n in parts))
        self.assertAlmostEqual(duration, 82.037619)
        parts, total, seconds = avatar_sampling.plan_segments(0, 93, 2, False)
        self.assertEqual((parts, total), ([93, 93], 173))
        for bad in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                avatar_sampling.plan_segments(bad)

    def test_http_timeout_zero_means_unlimited(self):
        self.assertIsNone(cli_support.http_timeout(0))
        self.assertEqual(cli_support.http_timeout(5), 5)
        with patch.dict("os.environ", {"COLABLLM_COMMAND_TIMEOUT": "7"}):
            self.assertEqual(cli_support.http_timeout(None), 7)
        with self.assertRaises(ValueError):
            cli_support.http_timeout(-1)

    def test_timeouts_map_to_exit_124(self):
        for error in (TimeoutError("slow"), urllib.error.URLError(TimeoutError("slow")),
                      cli_support.CliError("deadline", 124)):
            with self.assertRaises(SystemExit) as ctx, patch("sys.stderr"):
                cli_support.run_cli(lambda error=error: (_ for _ in ()).throw(error))
            self.assertEqual(ctx.exception.code, 124, error)

    def test_endpoint_requires_explicit_token_for_new_origin(self):
        state = {"url": "https://a.example.com", "api_token": "saved"}
        self.assertEqual(cli_support.resolve_endpoint(state), ("https://a.example.com", "saved"))
        with self.assertRaises(cli_support.CliError):
            cli_support.resolve_endpoint(state, "https://b.example.com")
        self.assertEqual(cli_support.resolve_endpoint(state, "https://b.example.com", "t"),
                         ("https://b.example.com", "t"))
        with self.assertRaises(cli_support.CliError):
            cli_support.resolve_endpoint(state, "https://u:p@a.example.com")
        with self.assertRaises(cli_support.CliError):
            cli_support.resolve_endpoint(state, "https://a.example.com:notaport")

    def test_srt_and_prompt_helpers(self):
        srt = nemotron_audio.words_to_srt([
            {"word": "Hello", "start": 0.0, "end": 0.4, "speaker": "spk0"},
            {"word": "world.", "start": 0.5, "end": 0.9, "speaker": "spk0"},
            {"word": "Hi", "start": 3.0, "end": 3.2, "speaker": "spk1"},
        ])
        self.assertIn("1\n00:00:00,000 --> 00:00:00,900\n[spk0] Hello world.", srt)
        self.assertIn("[spk1] Hi", srt)
        prompt, _, reasoning, timestamps = nemotron_audio.build_prompt_and_settings(
            {"preset": "translate", "lang": "Japanese"})
        self.assertIn("Japanese", prompt)
        self.assertFalse(reasoning or timestamps)
        self.assertTrue(nemotron_audio.build_prompt_and_settings({"preset": "speaker"})[3])
        shifted = nemotron_audio.shift_ctc_timestamps({"words": [{"start": 1.0, "end": 2.0}]}, 10.0)
        self.assertEqual(shifted["words"][0], {"start": 11.0, "end": 12.0})
        self.assertEqual(nemotron_audio.cleanup_generation_loops("a" * 50), "a")


if __name__ == "__main__":
    unittest.main()
