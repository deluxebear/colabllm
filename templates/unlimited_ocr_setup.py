# @title 1. Setup -- pinned official Transformers backend; model on local SSD
from pathlib import Path

OCR_WORK = Path('/content/unlimited-ocr')
OCR_WORK.mkdir(exist_ok=True)
if not shutil.which('uv'):
    subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', 'uv'], check=True)
OCR_PY = str(OCR_WORK / 'venv/bin/python')
if not Path(OCR_PY).exists():
    subprocess.run(['uv', 'venv', '--python', '3.10', str(OCR_WORK / 'venv')], check=True)
OCR_PACKAGES = ['torch==2.13.0', 'torchvision', 'pillow', 'transformers==4.52.1',
                'huggingface-hub==0.36.0', 'tokenizers==0.21.4', 'addict==2.4.0',
                'einops==0.8.2', 'easydict==1.13', 'pymupdf==1.27.2.2', 'requests']
OCR_MARKER = OCR_WORK / '.installed'
OCR_SIGNATURE = json.dumps(OCR_PACKAGES)
if not OCR_MARKER.exists() or OCR_MARKER.read_text() != OCR_SIGNATURE:
    subprocess.run(['uv', 'pip', 'install', '--python', OCR_PY, *OCR_PACKAGES], check=True)
    OCR_MARKER.write_text(OCR_SIGNATURE)
OCR_MODEL = OCR_WORK / 'model'
subprocess.run([OCR_PY, '-c', '''from huggingface_hub import snapshot_download
snapshot_download('baidu/Unlimited-OCR-2', revision='0480966938291445591f69f08a13b041c073a96a',
                  local_dir='/content/unlimited-ocr/model',
                  ignore_patterns=['assets/*', 'wheel/*', '*.pdf', '*.md'])
'''], check=True, env={**os.environ, 'HF_XET_CHUNK_CACHE_SIZE_BYTES': '0'})
shutil.rmtree('/root/.cache/huggingface/xet', ignore_errors=True)
OCR_POSTPROCESS = OCR_WORK / 'process_unlimitedOCR2.py'
if not OCR_POSTPROCESS.exists():
    urllib.request.urlretrieve('https://raw.githubusercontent.com/baidu/Unlimited-OCR/'
        'ea42e6d15ff93fdd0e8041fc0ac70880513453d3/utils/process_unlimitedOCR2.py', OCR_POSTPROCESS)
# Triton / Inductor compile on SSD; restore and persist caches by GPU and environment.
(OCR_WORK / 'colab_cache.py').write_text(OCR_CACHE_SOURCE)
sys.path.insert(0, str(OCR_WORK))
from colab_cache import restore_cache, save_cache
OCR_GPU = subprocess.check_output(['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'], text=True).strip().replace(' ', '_')
OCR_CACHE_KEY = 'torch213-uocr2-04809669-' + OCR_GPU
OCR_CACHE_PAIRS = []
for name in ('triton-cache', 'inductor-cache'):
    local = OCR_WORK / name
    restore_cache(local, Path(DATA_DIR) / name, Path(DATA_DIR) / 'kernel-archives', OCR_CACHE_KEY)
    OCR_CACHE_PAIRS.append(local)
print('Unlimited-OCR 2 setup complete', flush=True)
