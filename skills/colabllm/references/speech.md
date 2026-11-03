# Transcription and speech synthesis

## Nemotron transcription

Use `colabllm nemotron-transcribe transcribe --help`. Its default GPU is G4.

```bash
colabllm nemotron-transcribe transcribe ./meeting.wav --local --json -O ./transcripts
colabllm nemotron-transcribe transcribe 'drive://audio/meeting.wav' \
  --preset speaker --timestamps --save-drive --json -O ./transcripts
colabllm nemotron-transcribe transcribe --batch-file ./tasks.jsonl --json -O ./transcripts
colabllm nemotron-transcribe ls-drive 'audio' --json
colabllm nemotron-transcribe prompts
```

`--preset speaker` enables speaker-oriented timestamps; `--timestamps` can produce subtitles.
Use `--local` and `--drive` deliberately to distinguish sources. Media URLs may require yt-dlp,
FFmpeg or cookies, depending on source and chosen download path; inspect errors before switching
between remote and local downloading. Do not display cookie contents. Batch files can carry per-item
instructions. `--instances` and `--all-instances` may distribute work across existing ready instances;
these do not authorize launching or stopping additional services. Check partial batch errors.

`--timeout` here is an individual request/task timeout, not the total elapsed time of a whole batch.
Unlike OCR/Avatar jobs, transcription requests do not expose the same persistent job-resume contract;
do not invent a job ID or assume that disconnecting safely resumes a transcription batch.

## Qwen3-TTS

Use `colabllm qwen3-tts tts --help` and `voices --help`; default GPU is L4.

```bash
colabllm qwen3-tts voices --json
colabllm qwen3-tts tts '你好，欢迎使用语音服务。' --voice serena --lang Chinese -o ./speech.wav
colabllm qwen3-tts tts 'Hello.' --ref-audio ./reference.wav --ref-text 'Reference transcript.' -o ./clone.wav
colabllm qwen3-tts design-voice --instruct 'Warm, calm narrator' --name narrator -o ./narrator-reference.wav
```

Task selection distinguishes built-in CustomVoice speakers, VoiceDesign and Base voice cloning.
Use `--voice-design` / `--task` according to installed help. Reference audio can be local, Drive or
an HTTP URL when supported. `design-voice --name` registers a reusable voice, so choose an identifier
that does not overwrite a voice the user intends to preserve. `--save-drive` optionally persists
output remotely; `-o` is the local output file. Honor consent and the user's chosen reference voice.

## IndexTTS

Use `colabllm indextts tts --help`; default GPU is L4.

```bash
colabllm indextts voices --json
colabllm indextts tts '这是一段旁白。' --ref-audio ./reference.wav -o ./speech.wav
colabllm indextts tts '太让人惊喜了！' --ref-audio ./reference.wav \
  --emo-preset happy --emo-alpha 0.8 -o ./happy.wav
```

Select a named registered voice or reference audio as required by the runtime. Emotion controls
and language names differ from Qwen3-TTS; use the actual parser choices. Voice upload/delete commands
change persistent registered voices. Delete only the voice requested by the user.

For both TTS services, `--timeout` is the HTTP generation request limit. A network failure after
submission does not prove synthesis never started. Check response and local output before retrying;
verify the output file exists and is nonempty rather than treating exit output as an audio artifact.
