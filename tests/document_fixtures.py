"""小型真实 PDF 夹具，仅供行为验证。"""
import io
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def text_pdf():
    pdf = PdfWriter()
    page = pdf.add_blank_page(width=200, height=200)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
    stream = DecodedStreamObject(); stream.set_data(b'BT /F1 12 Tf 20 150 Td (PDF preview test) Tj ET')
    page[NameObject('/Contents')] = stream
    output = io.BytesIO(); pdf.write(output)
    return output.getvalue()
