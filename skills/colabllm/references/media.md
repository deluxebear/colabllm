# Avatar, video and ComfyUI

## LongCat Avatar

`longcat-avatar` defaults to G4. Read `generate`, `job` and `download --help`.

```bash
colabllm longcat-avatar generate --audio ./speech.wav --image ./person.png \
  --prompt 'A person speaking naturally in a bright room.' -o ./avatar.mp4

# Submit to the background and save to Drive; retain the returned job_id.
colabllm longcat-avatar generate --audio-drive 'materials/speech.wav' \
  --image-drive 'materials/person.png' --prompt-file ./prompt.txt --drive-output 'videos/avatar'
colabllm longcat-avatar job JOB_ID --wait --timeout 600 -o ./avatar.mp4
colabllm longcat-avatar download JOB_ID -o ./avatar.mp4
colabllm longcat-avatar ls-drive 'materials'
```

`-o` is a local download and implies waiting for generate/job; `--drive-output` is a remote Drive
folder. Default generation covers the full audio. Specifying `--frames` or `--segments` switches to
manual duration: use both deliberately for previews. Standard sampling permits CFG changes;
distilled mode uses its fixed fast sampling settings. Check help before changing those controls.
Ctrl+C/timeout stops the client wait while the submitted job continues. Query the original job ID
rather than resubmitting. Verify the downloaded video and report its actual path.

## LongCat Video

`longcat-video` defaults to G4 and offers a Gradio UI for text/image-to-video and continuation.
Use `up`, `status --json` and `info`; obtain browser credentials privately. It does not have Avatar's
`generate/job/download` commands. Inspect documented server capabilities before automating generation.

## ComfyUI

```bash
colabllm comfyui up --workflow WORKFLOW_URL_OR_TEMPLATE
colabllm comfyui pull-workflow WORKFLOW_URL_OR_TEMPLATE --timeout 600
colabllm comfyui pull-model MODEL_URL --dir loras --timeout 600
```

`comfyui` defaults to L4. Workflow/model downloads are staged on remote SSD, not downloaded to this
machine. Pulling a workflow downloads its referenced models; it does not submit an image/video
inference job. Submit generation through ComfyUI's documented workflow API or UI. A UI-format workflow
may need conversion to API format before queue submission; do not assume they are interchangeable.

User data and outputs can persist on Drive; SSD model weights disappear with the VM. Keep the runtime
alive until required outputs are saved. Check existing queue activity before restarting ComfyUI.
Model-directory and filename choices must match workflow expectations; use `pull-model --help` rather
than guessing remote filesystem locations. `-A/--all-instances` applies a pull to multiple existing
instances, so select the intended targets explicitly.
