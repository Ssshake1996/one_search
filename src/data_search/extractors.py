"""Bounded, offline-only text extraction with source-specific locators.

Document parsers are imported on demand. The service additionally runs this
module in a time/memory bounded child process: PDF parsing can allocate before
an extraction callback gets control.
"""

from __future__ import annotations

import codecs
import csv
import posixpath
import zipfile
from html.parser import HTMLParser
from pathlib import Path

CHUNK_CHARS = 1200
MAX_CHUNKS = 20_000
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 20_000
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".log", ".py", ".pyi", ".js", ".mjs",
    ".cjs", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs", ".c", ".cpp",
    ".cc", ".cxx", ".h", ".hpp", ".cs", ".sh", ".bash", ".zsh", ".ps1",
    ".psm1", ".sql", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini",
    ".xml", ".conf", ".config", ".css", ".scss", ".sass", ".less", ".r",
    ".rb", ".php", ".swift", ".kt", ".kts", ".scala", ".vue", ".svelte",
    ".lua", ".pl", ".dart", ".ex", ".exs", ".erl", ".hrl", ".clj",
    ".cljs", ".fs", ".fsx", ".vb", ".bat", ".cmd", ".properties", ".env",
    ".rst", ".tex", ".gitignore", ".dockerignore", ".editorconfig",
}
TEXT_NAMES = {"dockerfile", "makefile", "gemfile", "rakefile", "license", "readme"}
NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


class _Limit(Exception):
    pass


class _Encrypted(Exception):
    pass


class _Collector:
    def __init__(self, max_chars: int):
        self.max_chars = max_chars
        self.length = 0
        self.chunks: list[dict] = []

    def add(self, text: str, locator: dict) -> None:
        if not text:
            return
        remaining = self.max_chars - self.length
        shortened = text[:remaining]
        line_number = locator.get("line_start")
        for offset in range(0, len(shortened), CHUNK_CHARS):
            if len(self.chunks) >= MAX_CHUNKS:
                raise _Limit("chunk_count_limit")
            piece = shortened[offset:offset + CHUNK_CHARS]
            location = dict(locator)
            if line_number is not None:
                location["line_start"] = line_number
                location["line_end"] = line_number + piece.count("\n") - int(piece.endswith("\n"))
                line_number += piece.count("\n")
            self.chunks.append({"text": piece, "locator": location})
            self.length += len(piece)
        if len(text) > remaining:
            raise _Limit("extracted_character_limit")


def _source_budget(max_chars: int) -> int:
    return min(64 * 1024 * 1024, max(1024 * 1024, max_chars * 16))


def _encoding(path: Path, budget: int) -> str:
    """Validate UTF-8 in bounded blocks, including Chinese after an ASCII prefix."""
    with path.open("rb") as stream:
        sample = stream.read(min(65536, budget))
        if sample.startswith(codecs.BOM_UTF8):
            return "utf-8-sig"
        if sample.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
            return "utf-32"
        if sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            return "utf-16"
        if sample and sample.count(b"\0") > len(sample) // 5:
            even = sample[::2].count(b"\0")
            odd = sample[1::2].count(b"\0")
            if odd > even * 4:
                return "utf-16-le"
            if even > odd * 4:
                return "utf-16-be"
            raise ValueError("binary_or_unknown_text_encoding")
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        checked = len(sample)
        try:
            decoder.decode(sample, final=False)
            while checked < budget:
                block = stream.read(min(65536, budget - checked))
                if not block:
                    decoder.decode(b"", final=True)
                    break
                checked += len(block)
                decoder.decode(block, final=False)
            return "utf-8"
        except UnicodeDecodeError:
            return "gb18030"


def _text_blocks(path: Path, collector: _Collector):
    budget = _source_budget(collector.max_chars)
    encoding = _encoding(path, budget)
    decoder = codecs.getincrementaldecoder(encoding)("strict")
    total = 0
    with path.open("rb") as stream:
        while total < budget:
            block = stream.read(min(8192, budget - total))
            if not block:
                tail = decoder.decode(b"", final=True)
                if tail:
                    yield tail
                return
            total += len(block)
            decoded = decoder.decode(block, final=False)
            controls = sum(ord(char) < 32 and char not in "\t\r\n\f" for char in decoded)
            if "\0" in decoded or (decoded and controls > len(decoded) / 10):
                raise ValueError("binary_or_unknown_text_encoding")
            yield decoded
        if stream.read(1):
            raise _Limit("source_read_budget")
        tail = decoder.decode(b"", final=True)
        if tail:
            yield tail


def _plain(path: Path, out: _Collector) -> None:
    buffer = ""
    line = start = 1
    last = 1
    pending_cr = ""
    for block in _text_blocks(path, out):
        block = pending_cr + block
        pending_cr = "\r" if block.endswith("\r") else ""
        if pending_cr:
            block = block[:-1]
        block = block.replace("\r\n", "\n").replace("\r", "\n")
        for character in block:
            buffer += character
            last = line
            if character == "\n":
                line += 1
            if len(buffer) >= CHUNK_CHARS:
                out.add(buffer, {"line_start": start, "line_end": last})
                buffer = ""
                start = line
    if pending_cr:
        buffer += "\n"
        last = line
    if buffer:
        out.add(buffer, {"line_start": start, "line_end": last})


class _HTMLText(HTMLParser):
    def __init__(self, out: _Collector):
        super().__init__(convert_charrefs=True)
        self.out = out
        self.suppressed: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template", "noscript"}:
            self.suppressed.append(tag)

    def handle_endtag(self, tag):
        if self.suppressed and tag == self.suppressed[-1]:
            self.suppressed.pop()

    def handle_data(self, data):
        if not self.suppressed and data.strip():
            start = self.getpos()[0]
            self.out.add(data, {"line_start": start, "line_end": start + data.count("\n")})


def _html(path: Path, out: _Collector) -> None:
    parser = _HTMLText(out)
    for block in _text_blocks(path, out):
        parser.feed(block)
        if len(parser.rawdata) > _source_budget(out.max_chars):
            raise _Limit("html_token_limit")
    parser.close()


def _csv(path: Path, out: _Collector) -> None:
    # csv.reader expects complete physical lines. A bounded wrapper prevents one
    # giant field/record from bypassing the extraction memory budget.
    budget = _source_budget(out.max_chars)
    maximum_record = min(1024 * 1024, max(CHUNK_CHARS, out.max_chars + 1))
    previous_limit = csv.field_size_limit()
    csv.field_size_limit(maximum_record)
    try:
        with path.open("r", encoding=_encoding(path, budget), newline="") as stream:
            source_chars = 0
            record_chars = 0

            def lines():
                nonlocal source_chars, record_chars
                while True:
                    line = stream.readline(maximum_record + 1)
                    if not line:
                        return
                    if "\0" in line:
                        raise ValueError("binary_or_unknown_text_encoding")
                    source_chars += len(line)
                    record_chars += len(line)
                    if len(line) > maximum_record or record_chars > maximum_record:
                        raise _Limit("csv_record_limit")
                    if source_chars > budget:
                        raise _Limit("source_read_budget")
                    yield line

            reader = csv.reader(lines(), delimiter="\t" if path.suffix.lower() == ".tsv" else ",", strict=True)
            for number, row in enumerate(reader, 1):
                record_chars = 0
                if row:
                    out.add("\t".join(row), {"record": number, "columns": f"1:{len(row)}"})
    except csv.Error as exc:
        if "field larger" in str(exc):
            raise _Limit("csv_field_limit") from exc
        raise
    finally:
        csv.field_size_limit(previous_limit)


class _Archive:
    def __init__(self, path: Path, max_chars: int):
        with path.open("rb") as stream:
            header = stream.read(65536)
        if header.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
            if "EncryptedPackage".encode("utf-16-le") in header or "EncryptionInfo".encode("utf-16-le") in header:
                raise _Encrypted("password_protected_office_document")
            raise ValueError("invalid_ooxml_compound_container")
        self.zip = zipfile.ZipFile(path)
        self.budget = min(MAX_ARCHIVE_BYTES, max(8 * 1024 * 1024, max_chars * 32))
        self.consumed = 0
        try:
            infos = self.zip.infolist()
            if any(info.flag_bits & 1 for info in infos):
                raise _Encrypted("password_protected_archive")
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise _Limit("archive_entry_limit")
            if sum(info.file_size for info in infos) > self.budget:
                raise _Limit("archive_expansion_limit")
            if len({info.filename for info in infos}) != len(infos):
                raise ValueError("duplicate_archive_member")
        except Exception:
            self.zip.close()
            raise

    def close(self):
        self.zip.close()

    def tree(self, member: str):
        from defusedxml import ElementTree

        with self.zip.open(member) as stream:
            data = stream.read(self.budget - self.consumed + 1)
        self.consumed += len(data)
        if self.consumed > self.budget:
            raise _Limit("archive_expansion_limit")
        return ElementTree.fromstring(data, forbid_dtd=True)

    def relationships(self, member: str) -> dict[str, tuple[str, str]]:
        directory, filename = posixpath.split(member)
        relfile = posixpath.join(directory, "_rels", filename + ".rels")
        if relfile not in self.zip.namelist():
            return {}
        result = {}
        for rel in self.tree(relfile):
            if rel.get("TargetMode", "").lower() == "external":
                continue
            target = rel.get("Target", "")
            if not target or "\\" in target or ":" in target:
                continue
            resolved = posixpath.normpath(posixpath.join(directory, target)) if not target.startswith("/") else target.lstrip("/")
            if resolved.startswith("../"):
                continue
            result[rel.get("Id", "")] = (resolved, rel.get("Type", ""))
        return result


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _docx(archive: _Archive, out: _Collector) -> None:
    root = archive.tree("word/document.xml")
    if _local(root.tag) != "document":
        raise ValueError("invalid_word_document_root")
    number = 0
    table_number = 0
    for body in root:
        if _local(body.tag) != "body":
            continue
        for block in body:
            is_table = _local(block.tag) == "tbl"
            if is_table:
                table_number += 1
            for paragraph in block.iter():
                if _local(paragraph.tag) != "p":
                    continue
                number += 1
                text = "".join(
                    node.text or "" if _local(node.tag) == "t" else "\t" if _local(node.tag) == "tab" else "\n"
                    for node in paragraph.iter() if _local(node.tag) in {"t", "tab", "br", "cr"}
                )
                locator = {"paragraph": number}
                if is_table:
                    locator["table"] = table_number
                out.add(text, locator)


def _xlsx(archive: _Archive, out: _Collector) -> None:
    from openpyxl.styles.numbers import BUILTIN_FORMATS, is_date_format
    from openpyxl.utils.datetime import CALENDAR_MAC_1904, CALENDAR_WINDOWS_1900, from_excel

    shared = []
    if "xl/sharedStrings.xml" in archive.zip.namelist():
        for item in archive.tree("xl/sharedStrings.xml"):
            shared.append("".join(node.text or "" for node in item.iter() if _local(node.tag) == "t"))
    relations = archive.relationships("xl/workbook.xml")
    book = archive.tree("xl/workbook.xml")
    if _local(book.tag) != "workbook":
        raise ValueError("invalid_workbook_root")
    epoch = CALENDAR_WINDOWS_1900
    if any(_local(node.tag) == "workbookPr" and node.get("date1904") in {"1", "true"} for node in book):
        epoch = CALENDAR_MAC_1904
    date_styles = set()
    if "xl/styles.xml" in archive.zip.namelist():
        styles = archive.tree("xl/styles.xml")
        formats = dict(BUILTIN_FORMATS)
        for node in styles.iter():
            if _local(node.tag) == "numFmt":
                formats[int(node.get("numFmtId"))] = node.get("formatCode", "")
        for node in styles:
            if _local(node.tag) == "cellXfs":
                for number, style in enumerate(node):
                    if is_date_format(formats.get(int(style.get("numFmtId", "0")), "")):
                        date_styles.add(number)
    for sheet in book.iter():
        if _local(sheet.tag) != "sheet":
            continue
        name = sheet.get("name", "")
        relationship = sheet.get(f"{{{NS['r']}}}id")
        if relationship is None:
            relationship = next((v for k, v in sheet.attrib.items() if _local(k) == "id"), None)
        target = relations.get(relationship)
        if not target:
            raise ValueError("missing_worksheet_relationship")
        out.add(name, {"sheet": name, "cells": None})
        root = archive.tree(target[0])
        shared_formulas = {}
        for cell in root.iter():
            if _local(cell.tag) != "c":
                continue
            ref = cell.get("r")
            if not ref:
                raise ValueError("missing_cell_reference")
            values = {_local(child.tag): child for child in cell}
            formula = values.get("f")
            value_element = values.get("v")
            value = value_element.text or "" if value_element is not None else ""
            kind = cell.get("t")
            if kind == "s" and value:
                value = shared[int(value)]
            elif kind == "inlineStr":
                value = "".join(node.text or "" for node in cell.iter() if _local(node.tag) == "t")
            elif kind == "b":
                value = "TRUE" if value == "1" else "FALSE"
            elif value and kind in {None, "n"} and int(cell.get("s", "0")) in date_styles:
                value = from_excel(float(value), epoch).isoformat()
            if formula is not None:
                expression = "=" + (formula.text or "")
                if formula.get("t") == "shared":
                    shared_index = formula.get("si")
                    if formula.text:
                        shared_formulas[shared_index] = (expression, ref)
                    elif shared_index in shared_formulas:
                        from openpyxl.formula.translate import Translator

                        master_expression, origin = shared_formulas[shared_index]
                        expression = Translator(master_expression, origin=origin).translate_formula(ref)
                    else:
                        expression = "[shared formula expression unavailable]"
                text = expression + ("\n[cached] " + value if value else "\n[cached value unavailable]")
            else:
                text = value
            locator = {"sheet": name, "cells": ref}
            if formula is not None:
                locator["formula"] = True
                locator["cached_value_present"] = bool(value)
                if formula.get("t") == "shared":
                    locator["shared_formula_index"] = formula.get("si")
            out.add(text, locator)


def _pptx(archive: _Archive, out: _Collector) -> None:
    presentation = archive.tree("ppt/presentation.xml")
    if _local(presentation.tag) != "presentation":
        raise ValueError("invalid_presentation_root")
    relations = archive.relationships("ppt/presentation.xml")
    slide_number = 0
    for item in presentation.iter():
        if _local(item.tag) != "sldId":
            continue
        slide_number += 1
        relationship = next((v for k, v in item.attrib.items() if k.startswith("{") and _local(k) == "id"), None)
        target = relations.get(relationship)
        if not target:
            raise ValueError("missing_slide_relationship")
        root = archive.tree(target[0])
        for paragraph in root.iter():
            if _local(paragraph.tag) == "p":
                out.add("".join(node.text or "" for node in paragraph.iter() if _local(node.tag) == "t"), {"slide": slide_number, "notes": False})
        for note_target, relation_type in archive.relationships(target[0]).values():
            if relation_type.endswith("/notesSlide"):
                notes = archive.tree(note_target)
                for shape in notes.iter():
                    if _local(shape.tag) != "sp":
                        continue
                    placeholders = [node.get("type") for node in shape.iter() if _local(node.tag) == "ph"]
                    if any(kind in {"sldNum", "dt", "hdr", "ftr", "sldImg"} for kind in placeholders):
                        continue
                    for paragraph in shape.iter():
                        if _local(paragraph.tag) == "p":
                            out.add("".join(node.text or "" for node in paragraph.iter() if _local(node.tag) == "t"), {"slide": slide_number, "notes": True})


def _pdf(path: Path, out: _Collector) -> str | None:
    from pypdf import PdfReader

    reader = PdfReader(path)
    try:
        if reader.is_encrypted:
            raise _Encrypted("password_protected_pdf")
        empty_pages = 0
        for number, page in enumerate(reader.pages, 1):
            before = out.length

            def visitor(text, *_):
                out.add(text, {"page": number})

            page.extract_text(visitor_text=visitor)
            if out.length == before:
                empty_pages += 1
        if empty_pages:
            return f"{empty_pages} page(s) had no extractable text; OCR is not enabled"
        return None
    finally:
        reader.close()


def extract(path: str, max_chars: int = 2_000_000) -> dict:
    """Return bounded source text without executing code or loading external data."""
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1:
        return {"status": "error", "reason": "max_chars_must_be_positive_integer", "chunks": [], "truncated": False}
    out = _Collector(max_chars)
    status, reason, truncated = "ready", None, False
    try:
        file = Path(path)
        suffix = file.suffix.lower()
        if suffix in TEXT_EXTENSIONS or file.name.lower() in TEXT_NAMES or file.name.lower() in TEXT_EXTENSIONS:
            _plain(file, out)
        elif suffix in {".html", ".htm"}:
            _html(file, out)
        elif suffix in {".csv", ".tsv"}:
            _csv(file, out)
        elif suffix in {".docx", ".xlsx", ".pptx"}:
            archive = _Archive(file, max_chars)
            try:
                {".docx": _docx, ".xlsx": _xlsx, ".pptx": _pptx}[suffix](archive, out)
            finally:
                archive.close()
        elif suffix == ".pdf":
            reason = _pdf(file, out)
            if not out.chunks:
                status = "unsupported"
                reason = reason or "no_extractable_text; OCR is not enabled"
        else:
            status, reason = "unsupported", "unsupported_content_type"
    except _Limit as exc:
        status, reason, truncated = "partial", str(exc), True
    except _Encrypted as exc:
        status, reason = "encrypted", str(exc)
        out.chunks = []
    except Exception as exc:
        status, reason = "error", f"{type(exc).__name__}: {exc}"[:500]
        out.chunks = []
    return {"status": status, "reason": reason, "chunks": out.chunks, "truncated": truncated}
