"""实际 Tesseract 中英识别；部署环境必须执行，未安装引擎的本地环境跳过。"""
from pathlib import Path
import unittest
from app.ocr import engine_status, recognize_image


@unittest.skipUnless(engine_status()['available'], '本环境尚未安装中英文 Tesseract')
class OCRRuntimeTests(unittest.TestCase):
    def test_real_bilingual_image_recognition(self):
        text=recognize_image((Path(__file__).parent/'fixtures/ocr-bilingual.png').read_bytes())
        normalized=''.join(text.split())
        self.assertIn('研发部',normalized)
        self.assertIn('RADAR314159',normalized)
