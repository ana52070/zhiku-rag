"""本地单线程 OCR；临时图像不离开服务器。"""
import io
import os
import shutil
import subprocess
import tempfile
import threading
from functools import lru_cache
from pathlib import Path
from PIL import Image, ImageOps

EXTRACTION_VERSION = 'local-ocr-1'
_engine_lock = threading.Lock()
_pdf_lock = threading.Lock()


@lru_cache(maxsize=1)
def engine_status():
    command = shutil.which('tesseract')
    enabled = os.environ.get('RAG_OCR_ENABLED', '1') != '0'
    languages = os.environ.get('RAG_OCR_LANGUAGES', 'chi_sim+eng')
    available = False
    engine_version = None
    if command and enabled:
        try:
            result = subprocess.run([command, '--list-langs'], capture_output=True, text=True, timeout=10)
            installed = set(result.stdout.splitlines()[1:])
            version = subprocess.run([command, '--version'], capture_output=True, text=True, timeout=10)
            engine_version = version.stdout.splitlines()[0].removeprefix('tesseract ').strip() if version.returncode == 0 and version.stdout else None
            available = result.returncode == 0 and all(lang in installed for lang in languages.split('+'))
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {'enabled': enabled, 'available': available, 'engine': 'Tesseract', 'engine_version': engine_version, 'local': True, 'device': 'CPU', 'languages': languages, 'version': EXTRACTION_VERSION}


def recognize_image(raw):
    status = engine_status()
    if not status['available']:
        raise ValueError('本地 OCR 未就绪，请安装 Tesseract 及中英文语言包。')
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > 40_000_000:
                raise ValueError('图片超过 OCR 像素上限。')
            image = ImageOps.exif_transpose(image).convert('RGB')
            if min(image.size) < 16:
                return ''
            image.thumbnail((4096, 4096))
            if image.width < 1200:
                ratio = min(3, 1200 / image.width)
                image = image.resize((int(image.width * ratio), int(image.height * ratio)))
            image = ImageOps.grayscale(image)
            with _engine_lock, tempfile.TemporaryDirectory(prefix='zhiku-ocr-') as directory:
                path = Path(directory) / 'image.png'; image.save(path, dpi=(300, 300))
                result = subprocess.run([shutil.which('tesseract'), str(path), 'stdout', '-l', status['languages'], '--psm', '3'],
                                        capture_output=True, timeout=30, env={**os.environ, 'OMP_THREAD_LIMIT': '1'})
                if result.returncode:
                    raise ValueError('本地 OCR 无法识别该图片。')
                return result.stdout.decode('utf-8', errors='replace').strip()
    except subprocess.TimeoutExpired:
        raise ValueError('图片 OCR 超时。') from None
    except (OSError, Image.DecompressionBombError):
        raise ValueError('图片格式无法进行 OCR。') from None


def render_pdf_page(content, index):
    import math
    import pypdfium2
    with _pdf_lock, pypdfium2.PdfDocument(content) as document:
        page = document[index]
        try:
            width, height = page.get_size()
            scale = min(300 / 72, math.sqrt(12_000_000 / max(width * height, 1)))
            bitmap = page.render(scale=scale, grayscale=True)
            try:
                image = bitmap.to_pil(); output = io.BytesIO(); image.save(output, format='PNG'); return output.getvalue()
            finally:
                bitmap.close()
        finally:
            page.close()
