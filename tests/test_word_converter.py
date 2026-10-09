"""复杂绘图检测与真实转换保留原文件的回归验证。"""
import hashlib
import io
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from pypdf import PdfReader
from app.office_converter import needs_pdf_preview, word_pdf
from tests.test_rich_documents import office_fixture


class WordConverterTests(unittest.TestCase):
    def test_vml_shapes_use_conversion_and_plain_word_stays_browser_rendered(self):
        with tempfile.TemporaryDirectory() as directory:
            plain=Path(directory)/'plain.docx'; plain.write_bytes(office_fixture('.docx'))
            self.assertFalse(needs_pdf_preview(plain))
            complex_file=Path(directory)/'complex.docx'
            with zipfile.ZipFile(plain) as source,zipfile.ZipFile(complex_file,'w') as output:
                for item in source.infolist():
                    raw=source.read(item.filename)
                    if item.filename=='word/document.xml': raw=raw.replace(b'</w:body>',b'<w:p><w:r><w:pict><v:shape xmlns:v="urn:schemas-microsoft-com:vml"/></w:pict></w:r></w:p></w:body>')
                    output.writestr(item,raw)
            self.assertTrue(needs_pdf_preview(complex_file))

    @unittest.skipUnless(shutil.which('soffice') or shutil.which('libreoffice'),'本环境未安装 LibreOffice')
    def test_real_word_pdf_conversion_is_cached_and_keeps_source_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'original.docx'; source.write_bytes(office_fixture('.docx'))
            before=hashlib.sha256(source.read_bytes()).hexdigest()
            pdf=word_pdf(source, directory, 'fixture')
            self.assertGreater(len(PdfReader(pdf).pages),0)
            self.assertIn('Native Word text',''.join(page.extract_text() or '' for page in PdfReader(pdf).pages))
            self.assertEqual(word_pdf(source,directory,'fixture'),pdf)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),before)
