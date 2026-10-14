"""
Comparison utilities for PDFs and CSVs.
"""

from __future__ import annotations

import difflib
import io
import json
import os
import sys

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("Missing dependency. Run:  pip install pymupdf")


# ── Internal helpers ───────────────────────────────────────────────────────────

def _get_words(page: fitz.Page) -> list:
    return [(w[0], w[1], w[2], w[3], w[4]) for w in page.get_text("words")]


def _diff_word_lists(words_a: list, words_b: list) -> tuple[set, set]:
    seq_a = [w[4] for w in words_a]
    seq_b = [w[4] for w in words_b]
    sm = difflib.SequenceMatcher(None, seq_a, seq_b, autojunk=False)
    deleted: set[int]  = set()
    inserted: set[int] = set()
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace"):
            deleted.update(range(i1, i2))
        if tag in ("insert", "replace"):
            inserted.update(range(j1, j2))
    return deleted, inserted


def _build_combined_page(
    out_doc: fitz.Document,
    doc_a: fitz.Document,
    doc_b: fitz.Document,
    page_num: int,
    total_pages: int,
) -> None:
    has_a = page_num < len(doc_a)
    has_b = page_num < len(doc_b)

    ref_page = doc_a[page_num] if has_a else doc_b[page_num]
    pw = ref_page.rect.width
    ph = ref_page.rect.height

    DIVIDER = 2
    LABEL_H = 22
    canvas_w = pw * 2 + DIVIDER
    canvas_h = ph + LABEL_H

    new_page = out_doc.new_page(width=canvas_w, height=canvas_h)
    new_page.draw_rect(
        fitz.Rect(0, 0, canvas_w, canvas_h),
        color=None, fill=(0.96, 0.95, 0.93),
    )

    font_size = 9
    label_y   = LABEL_H - 5
    new_page.insert_text(
        (8, label_y),
        f"TRUTH  (page {page_num + 1}/{total_pages})  — removed text highlighted red",
        fontsize=font_size, color=(0.5, 0.1, 0.1),
    )
    new_page.insert_text(
        (pw + DIVIDER + 8, label_y),
        f"COMPARE  (page {page_num + 1}/{total_pages})  — added text highlighted green",
        fontsize=font_size, color=(0.1, 0.4, 0.1),
    )

    new_page.draw_line(
        (pw + DIVIDER / 2, 0),
        (pw + DIVIDER / 2, canvas_h),
        color=(0.7, 0.7, 0.7), width=1,
    )

    left_rect  = fitz.Rect(0,            LABEL_H, pw,       canvas_h)
    right_rect = fitz.Rect(pw + DIVIDER, LABEL_H, canvas_w, canvas_h)

    if has_a:
        new_page.show_pdf_page(left_rect,  doc_a, page_num)
    if has_b:
        new_page.show_pdf_page(right_rect, doc_b, page_num)

    words_a = _get_words(doc_a[page_num]) if has_a else []
    words_b = _get_words(doc_b[page_num]) if has_b else []
    deleted, inserted = _diff_word_lists(words_a, words_b)

    for idx in deleted:
        if idx < len(words_a):
            w = words_a[idx]
            rect = fitz.Rect(w[0], w[1] + LABEL_H, w[2], w[3] + LABEL_H)
            new_page.draw_rect(rect, color=None, fill=(1.0, 0.2, 0.2), fill_opacity=0.3, overlay=True)

    for idx in inserted:
        if idx < len(words_b):
            w = words_b[idx]
            rect = fitz.Rect(
                w[0] + pw + DIVIDER, w[1] + LABEL_H,
                w[2] + pw + DIVIDER, w[3] + LABEL_H,
            )
            new_page.draw_rect(rect, color=None, fill=(0.2, 0.85, 0.2), fill_opacity=0.3, overlay=True)


def _build_summary_page(
    out_doc: fitz.Document,
    doc_a: fitz.Document,
    doc_b: fitz.Document,
    previous_label: str,
    current_label: str,
    project_name: str,
    report_type: str,
) -> None:
    total_del = total_ins = total_eq = 0
    max_pages = max(len(doc_a), len(doc_b))

    for i in range(max_pages):
        wa = _get_words(doc_a[i]) if i < len(doc_a) else []
        wb = _get_words(doc_b[i]) if i < len(doc_b) else []
        deleted, inserted = _diff_word_lists(wa, wb)
        sm = difflib.SequenceMatcher(
            None, [w[4] for w in wa], [w[4] for w in wb], autojunk=False,
        )
        eq = sum(i2 - i1 for tag, i1, i2, _, _ in sm.get_opcodes() if tag == "equal")
        total_del += len(deleted)
        total_ins += len(inserted)
        total_eq  += eq

    total_words = total_del + total_eq
    pct = round((total_del + total_ins) / max(total_words + total_ins, 1) * 100, 1)

    pg = out_doc.new_page(width=595, height=842)
    pg.draw_rect(fitz.Rect(0, 0, 595, 60), color=None, fill=(0.1, 0.1, 0.1))
    pg.insert_text(
        (24, 38), "PDF COMPARISON",
        fontsize=16, color=(1, 1, 1), fontname="helv",
    )

    y = [90]

    def line(text, x=24, size=11, color=(0.15, 0.15, 0.15)):
        pg.insert_text((x, y[0]), text, fontsize=size, color=color, fontname="helv")
        y[0] += size + 8

    line(f"Project: {project_name}  |  Report type: {report_type}", size=10, color=(0.2, 0.2, 0.6))
    y[0] += 6

    line("Files compared", size=9, color=(0.5, 0.5, 0.5))
    line(f"  Truth (A) :  {previous_label}", size=10)
    line(f"  Compare  (B) :  {current_label}",  size=10)
    y[0] += 10

    line("Summary", size=9, color=(0.5, 0.5, 0.5))
    line(f"  Pages compared   :  {max_pages}",    size=10)
    line(f"  Words added      :  +{total_ins}",   size=10, color=(0.1, 0.45, 0.1))
    line(f"  Words removed    :  -{total_del}",   size=10, color=(0.6, 0.1, 0.1))
    line(f"  Words unchanged  :  {total_eq}",     size=10)
    line(f"  % changed        :  {pct}%",         size=10)
    y[0] += 14

    line("Highlight legend", size=9, color=(0.5, 0.5, 0.5))
    pg.draw_rect(fitz.Rect(24, y[0] - 10, 44, y[0] + 2), color=None, fill=(1.0, 0.2, 0.2), fill_opacity=0.5)
    pg.insert_text((50, y[0]), "Red   = text removed from Truth report",
                   fontsize=10, color=(0.15, 0.15, 0.15), fontname="helv")
    y[0] += 18

    pg.draw_rect(fitz.Rect(24, y[0] - 10, 44, y[0] + 2), color=None, fill=(0.2, 0.85, 0.2), fill_opacity=0.5)
    pg.insert_text((50, y[0]), "Green = text added in Compare report",
                   fontsize=10, color=(0.15, 0.15, 0.15), fontname="helv")
    y[0] += 30

    line("Side-by-side comparison follows on the next pages.", size=9, color=(0.5, 0.5, 0.5))


# ── Public API ─────────────────────────────────────────────────────────────────

def _get_file_extension(filename: str) -> str:
    return os.path.splitext(filename or "")[1].strip().lower()


COLUMN_MISMATCH_MARKER = "[COLUMN MISMATCH]"
COLUMN_MISMATCH_META_SEP = "||META||"


def _compute_column_mismatch(truth_columns: list, compare_columns: list) -> dict:
    """
    Compare the raw column lists (as read from each source file, before any
    reindexing/sorting) for count and order differences.
    """
    truth_columns = list(truth_columns)
    compare_columns = list(compare_columns)
    return {
        "truth_columns": truth_columns,
        "compare_columns": compare_columns,
        "column_count_mismatch": len(truth_columns) != len(compare_columns),
        "column_order_mismatch": truth_columns != compare_columns,
        "missing_in_compare": [c for c in truth_columns if c not in compare_columns],
        "extra_in_compare": [c for c in compare_columns if c not in truth_columns],
    }


def _write_column_mismatch_banner(ws, mismatch: dict, total_columns: int, fill) -> None:
    """
    If `mismatch` indicates a count or order difference, insert a highlighted
    banner row at the top of the sheet (before the header row) describing it,
    and embed a machine-readable JSON blob after a `||META||` separator so
    extract_excel_status() can parse it back out.
    """
    if not (mismatch["column_count_mismatch"] or mismatch["column_order_mismatch"]):
        return

    truth_columns = mismatch["truth_columns"]
    compare_columns = mismatch["compare_columns"]
    missing_in_compare = mismatch["missing_in_compare"]
    extra_in_compare = mismatch["extra_in_compare"]

    human_text = (
        f"{COLUMN_MISMATCH_MARKER} Truth has {len(truth_columns)} column(s), "
        f"Compare has {len(compare_columns)} column(s). "
        f"Missing in Compare: {missing_in_compare or 'None'}. "
        f"Extra in Compare: {extra_in_compare or 'None'}. "
        f"Order differs: {'Yes' if mismatch['column_order_mismatch'] else 'No'}."
    )
    meta_json = json.dumps({
        "truth_columns": truth_columns,
        "compare_columns": compare_columns,
        "missing_in_compare": missing_in_compare,
        "extra_in_compare": extra_in_compare,
        "column_count_mismatch": mismatch["column_count_mismatch"],
        "column_order_mismatch": mismatch["column_order_mismatch"],
    })

    ws.append([f"{human_text}{COLUMN_MISMATCH_META_SEP}{meta_json}"])
    banner_row = ws.max_row
    for col_idx in range(1, max(total_columns, 1) + 1):
        ws.cell(banner_row, col_idx).fill = fill
    banner_cell = ws.cell(banner_row, 1)
    banner_cell.font = banner_cell.font.copy(bold=True)
    banner_cell.alignment = banner_cell.alignment.copy(wrap_text=True)
    ws.row_dimensions[banner_row].height = 30
    if total_columns > 1:
        ws.merge_cells(start_row=banner_row, start_column=1, end_row=banner_row, end_column=total_columns)


def compare_pdfs(
    truth_pdf_bytes: bytes,
    compare_pdf_bytes: bytes,
    project_name: str = "Project",
    report_type: str = "Report",
    truth_label: str = "Truth",
    compare_label: str = "Compare",
) -> bytes:
    """
    Compare two PDFs and return a highlighted diff PDF as bytes.

    Args:
        truth_pdf_bytes: PDF bytes from the truth file (File A — left column).
        compare_pdf_bytes:  PDF bytes from the compare file  (File B — right column).
        project_name:       Human-readable project name shown on the summary page.
        report_type:        Type of report — shown on the summary page.
        truth_label:     Column A label.
        compare_label:      Column B label.

    Returns:
        Bytes of the generated diff PDF (summary page + side-by-side pages).
    """
    doc_a = fitz.open(stream=truth_pdf_bytes, filetype="pdf")
    doc_b = fitz.open(stream=compare_pdf_bytes,  filetype="pdf")
    out_doc = fitz.open()

    try:
        _build_summary_page(
            out_doc, doc_a, doc_b,
            truth_label, compare_label,
            project_name, report_type,
        )
        max_pages = max(len(doc_a), len(doc_b))
        for i in range(max_pages):
            _build_combined_page(out_doc, doc_a, doc_b, i, max_pages)

        buf = io.BytesIO()
        out_doc.save(buf)
        return buf.getvalue()
    finally:
        out_doc.close()
        doc_a.close()
        doc_b.close()


def compare_worker_comp_csvs(
    truth_csv_bytes: bytes,
    compare_csv_bytes: bytes,
    truth_label: str = "Truth",
    compare_label: str = "Compare",
) -> bytes:
    """
    Compare two Worker Compensation CSV files using key-based row matching.

    Rows are matched by every column in the file (an exact-match key) rather
    than position, so inserted or reordered rows do not cascade false CHANGEs.
    Because the key spans all columns, any row with a differing value won't
    pair up with its counterpart — it surfaces as a REMOVED/ADDED pair rather
    than a CHANGED row with the differing cell highlighted.

    Duplicate keys are paired first-to-first. Extra rows on either side are
    reported as ADDED or REMOVED.
    """
    try:
        import pandas as pd
        from collections import defaultdict
        from openpyxl import Workbook
        from openpyxl.styles import PatternFill, Font, Alignment
        from openpyxl.utils import get_column_letter
    except ImportError as exc:
        raise ImportError(
            f"Missing dependency for tabular comparison: {exc}. "
            "Run:  pip install pandas openpyxl"
        ) from exc

    RED_FILL    = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    GREEN_FILL  = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    YELLOW_FILL = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
    ORANGE_FILL = PatternFill(start_color="FFD8A8", end_color="FFD8A8", fill_type="solid")

    def _read_csv(file_bytes: bytes):
        return pd.read_csv(io.BytesIO(file_bytes), dtype=str, keep_default_na=False).fillna("")

    df_a = _read_csv(truth_csv_bytes)
    df_b = _read_csv(compare_csv_bytes)

    column_mismatch = _compute_column_mismatch(df_a.columns, df_b.columns)

    columns = list(dict.fromkeys(list(df_a.columns) + list(df_b.columns)))
    df_a = df_a.reindex(columns=columns, fill_value="")
    df_b = df_b.reindex(columns=columns, fill_value="")

    active_key_cols = columns

    def _row_key(row: dict) -> str:
        return "|".join(str(row.get(col, "")).strip() for col in active_key_cols)

    def _group_by_key(df):
        groups: dict[str, list[dict]] = defaultdict(list)
        key_order: list[str] = []
        for _, row in df.iterrows():
            k = _row_key(row.to_dict())
            if k not in groups:
                key_order.append(k)
            groups[k].append(row.to_dict())
        return groups, key_order

    groups_a, order_a = _group_by_key(df_a)
    groups_b, order_b = _group_by_key(df_b)

    seen: set[str] = set(order_a)
    all_keys = list(order_a)
    for k in order_b:
        if k not in seen:
            all_keys.append(k)
            seen.add(k)

    wb = Workbook()
    ws = wb.active
    ws.title = "Worker Comp Comparison"

    headers = ["Row", "Status"]
    for col in columns:
        headers.append(f"{truth_label}  {col}")
        headers.append(f"{compare_label}  {col}")

    _write_column_mismatch_banner(ws, column_mismatch, len(headers), ORANGE_FILL)

    ws.append(headers)
    header_row = ws.max_row
    for cell in ws[header_row]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(wrap_text=True)

    output_row_num = 0
    empty_row = {col: "" for col in columns}

    for key in all_keys:
        rows_a = groups_a.get(key, [])
        rows_b = groups_b.get(key, [])
        max_count = max(len(rows_a), len(rows_b))

        for i in range(max_count):
            row_a = rows_a[i] if i < len(rows_a) else empty_row
            row_b = rows_b[i] if i < len(rows_b) else empty_row

            output_row_num += 1

            if i >= len(rows_a):
                status = "ADDED"
            elif i >= len(rows_b):
                status = "REMOVED"
            elif any(row_a.get(col, "") != row_b.get(col, "") for col in columns):
                status = "CHANGED"
            else:
                status = "UNCHANGED"

            row_data = [output_row_num, status]
            changed_flags = []
            for col in columns:
                row_data.append(row_a.get(col, ""))
                row_data.append(row_b.get(col, ""))
                changed_flags.append(row_a.get(col, "") != row_b.get(col, ""))

            ws.append(row_data)
            excel_row = ws.max_row

            if status == "ADDED":
                for col_idx in range(3, len(headers) + 1):
                    ws.cell(excel_row, col_idx).fill = GREEN_FILL
            elif status == "REMOVED":
                for col_idx in range(3, len(headers) + 1):
                    ws.cell(excel_row, col_idx).fill = RED_FILL
            elif status == "CHANGED":
                col_pointer = 3
                for changed in changed_flags:
                    if changed:
                        ws.cell(excel_row, col_pointer).fill = YELLOW_FILL
                        ws.cell(excel_row, col_pointer + 1).fill = YELLOW_FILL
                    col_pointer += 2

    for col_idx, col in enumerate(ws.columns, start=1):
        max_len = max((len(str(cell.value)) for cell in col if cell.value), default=0)
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 2, 40)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def compare_any_files(
    truth_bytes: bytes,
    compare_bytes: bytes,
    truth_filename: str,
    compare_filename: str,
    project_name: str = "Project",
    report_type: str = "Report",
    truth_label: str = "Truth",
    compare_label: str = "Compare",
) -> tuple[bytes, str, str]:
    """
    Select the correct comparison function based on file extension.

    Returns:
        A tuple of (output_bytes, output_extension, mime_type).
    """
    truth_ext = _get_file_extension(truth_filename)
    compare_ext = _get_file_extension(compare_filename)

    if truth_ext != compare_ext:
        raise ValueError(
            f"Truth and compare files must have the same extension: "
            f"{truth_ext} != {compare_ext}"
        )

    if truth_ext == ".pdf":
        return (
            compare_pdfs(
                truth_bytes,
                compare_bytes,
                project_name=project_name,
                report_type=report_type,
                truth_label=truth_label,
                compare_label=compare_label,
            ),
            "pdf",
            "application/pdf",
        )

    if truth_ext in (".csv", ".xlsx", ".xls"):
        compare_fn = (
            compare_worker_comp_csvs
            if report_type == "Worker Compensation"
            else compare_csvs
        )
        return (
            compare_fn(
                truth_bytes,
                compare_bytes,
                truth_label=truth_label,
                compare_label=compare_label,
            ),
            "xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    raise ValueError(f"Unsupported file type for comparison: {truth_ext}")


def _read_tabular_file(file_bytes: bytes) -> dict[str, object]:
    import pandas as pd

    if file_bytes.startswith(b'PK\x03\x04'):
        sheets = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None, dtype=str)
        return {name: df.fillna("") for name, df in sheets.items()}

    df = pd.read_csv(io.BytesIO(file_bytes), dtype=str, keep_default_na=False).fillna("")
    return {"Sheet1": df}


def _normalize_sheet_title(name: str, existing: set[str]) -> str:
    safe = name[:31].replace('/', '_').replace('\\', '_').replace('*', '_').replace('?', '_')
    candidate = safe or 'Sheet'
    suffix = 1
    while candidate in existing:
        candidate = f"{safe[:28]}_{suffix}" if len(safe) > 28 else f"{safe}_{suffix}"
        suffix += 1
    existing.add(candidate)
    return candidate


def compare_csvs(
    truth_csv_bytes: bytes,
    compare_csv_bytes: bytes,
    truth_label: str = "Truth",
    compare_label: str = "Compare",
) -> bytes:
    """
    Compare two tabular files (CSV or Excel) and return a highlighted Excel workbook as bytes.

    For Excel files, all sheets are compared by sheet name. If both files contain
    a sheet named "Sheet1" and "Sheet2", the function compares those sheets
    pairwise. If one workbook has extra sheets, those sheets are compared with an
    empty sheet.

    Rows are compared by position. Columns are aligned by name.
    Colour coding:
      Red    = row removed from truth
      Green  = row added to compare
      Yellow = row present in both but with changed cell values

    Args:
        truth_csv_bytes: Raw bytes of the truth file (CSV or Excel).
        compare_csv_bytes: Raw bytes of the compare file (CSV or Excel).
        truth_label:     Label used in header for the A (truth) columns.
        compare_label:      Label used in header for the B (compare) columns.

    Returns:
        Bytes of an .xlsx workbook with highlighted differences.
    """
    try:
        import pandas as pd
        from openpyxl import Workbook
        from openpyxl.styles import PatternFill, Font, Alignment
        from openpyxl.utils import get_column_letter
    except ImportError as exc:
        raise ImportError(
            f"Missing dependency for tabular comparison: {exc}. "
            "Run:  pip install pandas openpyxl"
        ) from exc

    RED_FILL = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    GREEN_FILL = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    YELLOW_FILL = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
    ORANGE_FILL = PatternFill(start_color="FFD8A8", end_color="FFD8A8", fill_type="solid")

    tabs_a = _read_tabular_file(truth_csv_bytes)
    tabs_b = _read_tabular_file(compare_csv_bytes)
    sheet_names = list(dict.fromkeys(list(tabs_a) + list(tabs_b)))

    wb = Workbook()
    existing_titles: set[str] = set()
    first_sheet = True

    for sheet_name in sheet_names:
        df_a = tabs_a.get(sheet_name, pd.DataFrame())
        df_b = tabs_b.get(sheet_name, pd.DataFrame())

        column_mismatch = _compute_column_mismatch(df_a.columns, df_b.columns)

        columns = sorted(set(df_a.columns) | set(df_b.columns))
        df_a = df_a.reindex(columns=columns, fill_value="")
        df_b = df_b.reindex(columns=columns, fill_value="")

        if first_sheet:
            ws = wb.active
            first_sheet = False
        else:
            ws = wb.create_sheet()

        ws.title = _normalize_sheet_title(sheet_name, existing_titles)

        headers = ["Row", "Status"]
        for col in columns:
            headers.append(f"{truth_label}  {col}")
            headers.append(f"{compare_label}  {col}")

        _write_column_mismatch_banner(ws, column_mismatch, len(headers), ORANGE_FILL)

        ws.append(headers)
        header_row = ws.max_row
        for cell in ws[header_row]:
            cell.font = Font(bold=True)
            cell.alignment = Alignment(wrap_text=True)

        max_rows = max(len(df_a), len(df_b))

        for row_index in range(max_rows):
            row_a = df_a.iloc[row_index].to_dict() if row_index < len(df_a) else {col: "" for col in columns}
            row_b = df_b.iloc[row_index].to_dict() if row_index < len(df_b) else {col: "" for col in columns}

            if row_index >= len(df_a):
                status = "ADDED"
            elif row_index >= len(df_b):
                status = "REMOVED"
            else:
                status = "CHANGED" if any(row_a[col] != row_b[col] for col in columns) else "UNCHANGED"

            row = [row_index + 1, status]
            changed_flags = []
            for col in columns:
                row.append(row_a.get(col, ""))
                row.append(row_b.get(col, ""))
                changed_flags.append(row_a.get(col, "") != row_b.get(col, ""))

            ws.append(row)
            excel_row = ws.max_row

            if status == "ADDED":
                for col_idx in range(3, len(headers) + 1):
                    ws.cell(excel_row, col_idx).fill = GREEN_FILL
            elif status == "REMOVED":
                for col_idx in range(3, len(headers) + 1):
                    ws.cell(excel_row, col_idx).fill = RED_FILL
            else:
                col_pointer = 3
                for changed in changed_flags:
                    if changed:
                        ws.cell(excel_row, col_pointer).fill = YELLOW_FILL
                        ws.cell(excel_row, col_pointer + 1).fill = YELLOW_FILL
                    col_pointer += 2

        for col_idx, col in enumerate(ws.columns, start=1):
            max_len = max((len(str(cell.value)) for cell in col if cell.value), default=0)
            ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 2, 40)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()