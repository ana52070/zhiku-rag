"""实际 Tesseract 中英识别；部署环境必须执行，未安装引擎的本地环境跳过。"""
from pathlib import Path
import unittest
from app.ocr import engine_status, recognize_image


@unittest.skipUnless(engine_status()['available'], '本环境尚未安装中英文 Tesseract')
class OCRRuntimeTests(unittest.TestCase):
    def test_real_bilingual_image_recognition(self):
        text=recognize_image((Path(__file__).parent/'fixtures/ocr-bilingual.png').read_bytes())
        normalized=''.join(text.split())
        # OCR 不保证逐字无误；验证实际中英识别能力，错误例子记录在验收文档。
        self.assertIn('技术知识库',normalized)
        self.assertIn('RADAR314159',normalized)
