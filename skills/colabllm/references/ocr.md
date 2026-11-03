# Unlimited-OCR

Read `colabllm unlimited-ocr ocr --help` and `job --help` for current controls.
Check `status --json`; start the runtime if needed. The default GPU is A100.

```bash
# Local files are uploaded; outputs can be downloaded to a local directory.
colabllm unlimited-ocr ocr ./document.pdf -O ./ocr-results
colabllm unlimited-ocr ocr ./documents --local -O ./ocr-results

# Remote Drive input can be processed without downloading it to this machine.
colabllm unlimited-ocr ocr 'drive://scans' --output-dir 'OCR/results' -O ./ocr-results

# Submit without waiting; retain the returned job ID.
colabllm unlimited-ocr ocr 'drive://scans' --no-wait
colabllm unlimited-ocr job JOB_ID --wait --timeout 600 -O ./ocr-results

# Resume a failed/interrupted job instead of creating duplicate work.
colabllm unlimited-ocr resume JOB_ID --wait --timeout 600 -O ./ocr-results
```

`--output-dir` refers to Drive; `-O/--local-output-dir` refers to this machine. Relative paths
that do not exist locally can be interpreted as Drive paths. Use `./`, `--local` or `drive://`
explicitly when resolving user input, and verify local files exist before upload.
Input directories recurse by default; `--no-recursive` limits traversal. Mixed local/Drive inputs
are supported. Uploads are persisted on Drive; do not remove them while jobs depend on them.

Results include `result.md`, raw text, cropped images referenced by the Markdown and a completion
record. Preserve their directory structure when delivering output. Existing unchanged results
are skipped; use `--overwrite` only when reprocessing is intended. Large jobs process page windows;
`--pages-per-chunk` changes memory use and cross-page context, not just speed.

The CLI normally prints JSON, with upload/wait progress on stderr. Waiting timeout or Ctrl+C
leaves the remote job active. Record the ID and use `job` later. A terminal job can contain failed
files: inspect its status and per-file errors before claiming all documents converted successfully.
