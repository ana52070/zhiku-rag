"""实际 Tesseract 中英识别；部署环境必须执行，未安装引擎的本地环境跳过。"""
from pathlib import Path
import unittest
from app.ocr import engine_status, recognize_image
from app.documents import extract_document
from tests.test_rich_documents import scanned_pdf


@unittest.skipUnless(engine_status()['available'], '本环境尚未安装中英文 Tesseract')
class OCRRuntimeTests(unittest.TestCase):
    def test_real_bilingual_image_recognition(self):
        text=recognize_image((Path(__file__).parent/'fixtures/ocr-bilingual.png').read_bytes())
        normalized=''.join(text.split())
        # OCR 不保证逐字无误；验证实际中英识别能力，错误例子记录在验收文档。
        self.assertIn('技术知识库',normalized)
        self.assertIn('RADAR314159',normalized)

    def test_real_scanned_pdf_rendering_and_recognition(self):
        raw=(Path(__file__).parent/'fixtures/ocr-bilingual.png').read_bytes()
        _,text,info=extract_document('scan.pdf',scanned_pdf(raw))
        normalized=''.join(text.split())
        self.assertIn('技术知识库',normalized)
        self.assertIn('RADAR314159',normalized)
        self.assertIn('OCR · PDF 第 1 页',text)
        self.assertEqual(info['recognized'],1)
