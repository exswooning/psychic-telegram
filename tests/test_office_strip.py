"""An Office export goes up without its comments (their import credits source-domain authors: a. -> b., 21 Nov 2026)."""
import io
import zipfile

from office_strip import strip_comments

CT = ('<Types><Override PartName="/word/document.xml" ContentType="d"/>'
      '<Override PartName="/word/comments.xml" ContentType="c"/></Types>')


def _zip(parts):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in parts.items():
            z.writestr(name, body)
    return buf.getvalue()


def test_a_docx_loses_its_comments_and_nothing_else():
    doc = ('<w:body><w:p><w:commentRangeStart w:id="0"/><w:r><w:t>Hello</w:t></w:r><w:commentRangeEnd w:id="0"/>'
           '<w:r><w:commentReference w:id="0"/></w:r></w:p></w:body>')
    rels = ('<Relationships><Relationship Id="r1" Type="x/styles" Target="styles.xml"/>'
            '<Relationship Id="r2" Type="x/comments" Target="comments.xml"/></Relationships>')
    out = zipfile.ZipFile(io.BytesIO(strip_comments(_zip({
        "[Content_Types].xml": CT, "word/document.xml": doc, "word/comments.xml": "<w:comments/>",
        "word/styles.xml": "<s/>", "word/_rels/document.xml.rels": rels}))))
    assert "word/comments.xml" not in out.namelist() and "word/styles.xml" in out.namelist()
    body = out.read("word/document.xml").decode()
    assert "comment" not in body and "<w:t>Hello</w:t>" in body
    assert "comments.xml" not in out.read("word/_rels/document.xml.rels").decode()
    assert "styles.xml" in out.read("word/_rels/document.xml.rels").decode()
    assert "/word/comments.xml" not in out.read("[Content_Types].xml").decode()


def test_an_xlsx_loses_its_comment_parts_and_drawing_reference():
    sheet = '<worksheet><sheetData/><legacyDrawing r:id="r3"/></worksheet>'
    rels = ('<Relationships><Relationship Id="r2" Type="x/comments" Target="../comments1.xml"/>'
            '<Relationship Id="r3" Type="x/vmlDrawing" Target="../drawings/vmlDrawing1.vml"/></Relationships>')
    out = zipfile.ZipFile(io.BytesIO(strip_comments(_zip({
        "xl/worksheets/sheet1.xml": sheet, "xl/worksheets/_rels/sheet1.xml.rels": rels,
        "xl/comments1.xml": "<c/>", "xl/drawings/vmlDrawing1.vml": "<v/>"}))))
    assert set(out.namelist()) == {"xl/worksheets/sheet1.xml", "xl/worksheets/_rels/sheet1.xml.rels"}
    assert "legacyDrawing" not in out.read("xl/worksheets/sheet1.xml").decode()
    assert "Relationship " not in out.read("xl/worksheets/_rels/sheet1.xml.rels").decode()


def test_a_file_without_comments_or_not_a_zip_is_returned_as_given():
    plain = _zip({"word/document.xml": "<w:body/>"})
    assert strip_comments(plain) is plain
    assert strip_comments(b"%PDF-1.4") == b"%PDF-1.4"
