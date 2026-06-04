"""
HTML report generator for comparison results.
Extracts pass/fail status from diff files and generates an HTML report.
"""

from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    raise ImportError("Missing dependency. Run: pip install pymupdf")

try:
    import pandas as pd
except ImportError:
    raise ImportError("Missing dependency. Run: pip install pandas openpyxl")
from utils.gdrive import (
    create_drive_service,
    find_folder_by_name,
    get_or_create_folder,
    upload_or_update_file,
)


REPORT_OUTPUTS = {
    "pdf": ("pdf", "application/pdf"),
    "png": ("png", "image/png"),
}


# ── PDF Pass/Fail Extraction ───────────────────────────────────────────────────

def extract_pdf_status(pdf_bytes: bytes) -> dict:
    """
    Extract comparison status from a PDF diff file.

    Reads the first page (summary page) to extract:
    - Words added
    - Words removed
    - Percentage changed
    """

    try:
        print(f"[STATUS][PDF] Reading diff PDF bytes={len(pdf_bytes)}")
        # Open PDF from bytes
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")

        if len(doc) == 0:
            raise ValueError("PDF has no pages")

        # Read first page
        first_page = doc[0]
        text = first_page.get_text()

        # Regex extraction
        words_added_match = re.search(r"Words\s+added\s*:\s*\+?(\d+)", text, re.IGNORECASE)
        words_removed_match = re.search(r"Words\s+removed\s*:\s*-?(\d+)", text, re.IGNORECASE)
        percent_match = re.search(r"(?:Percentage\s+changed|%\s*changed)\s*:\s*([\d.]+)", text, re.IGNORECASE)
        pages_match = re.search(r"Pages\s+compared\s*:\s*(\d+)", text, re.IGNORECASE)
        print(
            "[STATUS][PDF] Regex matches: "
            f"words_added={words_added_match.group(1) if words_added_match else None}, "
            f"words_removed={words_removed_match.group(1) if words_removed_match else None}, "
            f"percent_changed={percent_match.group(1) if percent_match else None}, "
            f"pages_compared={pages_match.group(1) if pages_match else None}"
        )

        words_added = int(words_added_match.group(1)) if words_added_match else 0
        words_removed = int(words_removed_match.group(1)) if words_removed_match else 0
        percent_changed = float(percent_match.group(1)) if percent_match else 0.0
        pages_compared = int(pages_match.group(1)) if pages_match else 1

        # Pass if no changes
        is_pass = words_added == 0 and words_removed == 0
        status = "PASS" if is_pass else "FAIL"

        details = (
            f"Added: +{words_added}, "
            f"Removed: -{words_removed}, "
            f"Changed: {percent_changed}%"
        )
        print(f"[STATUS][PDF] Result: status={status}, {details}")

        return {
            "status": status,
            "words_added": words_added,
            "words_removed": words_removed,
            "percent_changed": percent_changed,
            "pages_compared": pages_compared,
            "details": details
        }

    except Exception as e:
        print(f"[STATUS][PDF] ERROR: {e}")
        return {
            "status": "ERROR",
            "words_added": 0,
            "words_removed": 0,
            "percent_changed": 0.0,
            "pages_compared": 1,
            "details": f"Error reading PDF: {str(e)}"
        }


# ── CSV/Excel Pass/Fail Extraction ────────────────────────────────────────────

def extract_excel_status(excel_bytes: bytes) -> dict:
    """
    Extract comparison status from an Excel diff file.
    
    Checks all sheets for "CHANGED" status in the Status column.
    
    Returns:
        {
            "status": "PASS" | "FAIL",
            "sheets_checked": int,
            "rows_changed": int,
            "details": str
        }
    """
    try:
        print(f"[STATUS][EXCEL] Reading diff workbook bytes={len(excel_bytes)}")
        xls = pd.ExcelFile(io.BytesIO(excel_bytes))
        sheets_checked = 0
        total_different = 0
        total_changed = 0
        total_added = 0
        total_removed = 0
        sheets_with_status = 0
        changed_details = []
        
        for sheet_name in xls.sheet_names:
            df = pd.read_excel(io.BytesIO(excel_bytes), sheet_name=sheet_name, dtype=str)
            sheets_checked += 1
            print(f"[STATUS][EXCEL] Sheet '{sheet_name}': rows={len(df)}, columns={list(df.columns)}")
            
            # Check if Status column exists
            if "Status" in df.columns:
                sheets_with_status += 1
                statuses = df["Status"].fillna("").str.upper().str.strip()
                status_counts = statuses.value_counts().to_dict()
                changed_in_sheet = int((statuses == "CHANGED").sum())
                added_in_sheet = int((statuses == "ADDED").sum())
                removed_in_sheet = int((statuses == "REMOVED").sum())
                different_in_sheet = changed_in_sheet + added_in_sheet + removed_in_sheet
                print(f"[STATUS][EXCEL] Sheet '{sheet_name}' status counts: {status_counts}")
                if different_in_sheet > 0:
                    total_different += different_in_sheet
                    total_changed += changed_in_sheet
                    total_added += added_in_sheet
                    total_removed += removed_in_sheet
                    changed_details.append(
                        f"{sheet_name}: {changed_in_sheet} changed, "
                        f"{added_in_sheet} added, {removed_in_sheet} removed rows"
                    )
            else:
                print(f"[STATUS][EXCEL] Sheet '{sheet_name}' has no Status column")

        if sheets_checked > 0 and sheets_with_status == 0:
            raise ValueError("No Status column found in any sheet")
        
        is_pass = total_different == 0
        status = "PASS" if is_pass else "FAIL"
        
        if changed_details:
            details = ", ".join(changed_details)
        else:
            details = f"No changes detected across {sheets_checked} sheet(s)"
        print(
            "[STATUS][EXCEL] Result: "
            f"status={status}, sheets={sheets_checked}, changed={total_changed}, "
            f"added={total_added}, removed={total_removed}, total_different={total_different}"
        )
        
        return {
            "status": status,
            "sheets_checked": sheets_checked,
            "rows_changed": total_changed,
            "rows_added": total_added,
            "rows_removed": total_removed,
            "rows_different": total_different,
            "details": details
        }
    except Exception as e:
        print(f"[STATUS][EXCEL] ERROR: {e}")
        return {
            "status": "ERROR",
            "sheets_checked": 0,
            "rows_changed": 0,
            "rows_added": 0,
            "rows_removed": 0,
            "rows_different": 0,
            "details": f"Error reading Excel: {str(e)}"
        }


# ── Unified Pass/Fail Extractor ────────────────────────────────────────────────

def extract_comparison_status(diff_file_bytes: bytes, filename: str) -> dict:
    """
    Extract pass/fail status from any diff file (PDF or Excel).
    
    Args:
        diff_file_bytes: Raw bytes of the diff file
        filename: Filename to determine file type (.pdf, .xlsx, etc.)
    
    Returns:
        {
            "status": "PASS" | "FAIL" | "MISSING" | "ERROR",
            "file_type": "pdf" | "excel",
            "details": str,
            ... (type-specific fields)
        }
    """
    if diff_file_bytes is None:
        return {
            "status": "MISSING",
            "file_type": "unknown",
            "details": "Diff file not found"
        }
    
    ext = Path(filename).suffix.lower()
    
    if ext == ".pdf":
        result = extract_pdf_status(diff_file_bytes)
        result["file_type"] = "pdf"
        return result
    elif ext in (".xlsx", ".xls"):
        result = extract_excel_status(diff_file_bytes)
        result["file_type"] = "excel"
        return result
    else:
        return {
            "status": "ERROR",
            "file_type": "unknown",
            "details": f"Unsupported file type: {ext}"
        }


# ── HTML Report Generator ──────────────────────────────────────────────────────

def generate_html_report(comparison_results: list[dict], timestamp: str = None) -> str:
    """
    Generate an HTML report from comparison results.
    
    Args:
        comparison_results: List of comparison result dicts from compare_folder_pairs()
        timestamp: Optional timestamp string (defaults to now)
    
    Returns:
        HTML string
    """
    if timestamp is None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Aggregate statistics
    total_comparisons = len(comparison_results)
    passed_count = sum(1 for r in comparison_results if r.get("status") == "PASS")
    failed_count = sum(1 for r in comparison_results if r.get("status") == "FAIL")
    missing_count = sum(1 for r in comparison_results if r.get("status") == "MISSING")
    error_count = sum(1 for r in comparison_results if r.get("status") == "ERROR")
    
    # Build rows for the report table
    rows_html = ""
    for result in comparison_results:
        company = result.get("company_name", "N/A")
        report_type = result.get("report_type", "N/A")
        payperiod = result.get("base_period", "N/A")
        status = result.get("status", "UNKNOWN")
        details = result.get("details", "")
        
        # Format status with color
        if status == "PASS":
            status_color = "#28a745"  # Green
            status_html = f'<span style="color: {status_color}; font-weight: bold;">✓ PASS</span>'
        elif status == "FAIL":
            status_color = "#dc3545"  # Red
            status_html = f'<span style="color: {status_color}; font-weight: bold;">✗ FAIL</span>'
        elif status == "MISSING":
            status_color = "#ffc107"  # Yellow
            status_html = f'<span style="color: {status_color}; font-weight: bold;">⚠ MISSING</span>'
        else:
            status_color = "#6c757d"  # Gray
            status_html = f'<span style="color: {status_color}; font-weight: bold;">? {status}</span>'
        
        # Drive folder link
        diff_folder_id = result.get("diff_folder_id")
        if diff_folder_id:
            drive_url = f"https://drive.google.com/drive/folders/{diff_folder_id}"
            drive_link_html = (
                f'<div style="margin-bottom:6px;">'
                f'<a href="{drive_url}" target="_blank" '
                f'style="color:#007bff;font-size:0.85em;text-decoration:none;">&#128193; View Diff Files</a>'
                f'</div>'
            )
        else:
            drive_link_html = ""

        # Format details as bullet points
        if isinstance(details, list):
            items_html = ""
            for detail in details:
                if isinstance(detail, dict):
                    for file_status in detail.get("file_statuses", []):
                        fname = file_status.get("filename", file_status.get("diff_filename", "File"))
                        fs = file_status.get("status", "UNKNOWN")
                        fd = file_status.get("details", "")
                        color = "#28a745" if fs == "PASS" else "#dc3545" if fs == "FAIL" else "#ffc107"
                        icon = "&#10003;" if fs == "PASS" else "&#10007;" if fs == "FAIL" else "&#9888;"
                        items_html += (
                            f'<li style="margin-bottom:3px;">'
                            f'<span style="color:{color};font-weight:bold;">{icon} {fs}</span>'
                            f' &mdash; <strong>{fname}</strong>: {fd}'
                            f'</li>'
                        )
                    for fname in detail.get("missing_compare", []):
                        items_html += f'<li style="color:#ffc107;">&#9888; Missing in compare: {fname}</li>'
                    for fname in detail.get("missing_truth", []):
                        items_html += f'<li style="color:#ffc107;">&#9888; Missing in truth: {fname}</li>'
                    for err in detail.get("errors", []):
                        items_html += f'<li style="color:#dc3545;">&#10007; Error: {err}</li>'
                else:
                    items_html += f'<li>{detail}</li>'
            bullet_list = (
                f'<ul style="margin:4px 0;padding-left:16px;list-style:none;">{items_html}</ul>'
                if items_html else ""
            )
            detail_text = drive_link_html + bullet_list
        else:
            detail_text = drive_link_html + (str(details) if details else "")
        
        rows_html += f"""
        <tr>
            <td style="border: 1px solid #ddd; padding: 10px;">{company}</td>
            <td style="border: 1px solid #ddd; padding: 10px;">{report_type}</td>
            <td style="border: 1px solid #ddd; padding: 10px;">{payperiod}</td>
            <td style="border: 1px solid #ddd; padding: 10px; text-align: center;">{status_html}</td>
            <td style="border: 1px solid #ddd; padding: 10px; font-size: 0.9em;">{detail_text}</td>
        </tr>
        """
    
    # Build the HTML document
    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Comparison Report</title>
        <style>
            body {{
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                margin: 0;
                padding: 20px;
                background-color: #f5f5f5;
            }}
            .container {{
                max-width: 1200px;
                margin: 0 auto;
                background-color: white;
                padding: 20px;
                border-radius: 8px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }}
            h1 {{
                color: #333;
                text-align: center;
                border-bottom: 3px solid #007bff;
                padding-bottom: 10px;
            }}
            .summary {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 15px;
                margin-bottom: 30px;
            }}
            .summary-card {{
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                padding: 20px;
                border-radius: 8px;
                text-align: center;
                box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            }}
            .summary-card h3 {{
                margin: 0;
                font-size: 0.9em;
                opacity: 0.9;
            }}
            .summary-card .number {{
                font-size: 2.5em;
                font-weight: bold;
                margin-top: 10px;
            }}
            .summary-card.passed {{
                background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%);
            }}
            .summary-card.failed {{
                background: linear-gradient(135deg, #ee0979 0%, #ff6a00 100%);
            }}
            .summary-card.missing {{
                background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
            }}
            .summary-card.error {{
                background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
            }}
            .timestamp {{
                text-align: right;
                color: #666;
                font-size: 0.9em;
                margin-bottom: 20px;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                margin-top: 20px;
            }}
            th {{
                background-color: #007bff;
                color: white;
                padding: 12px;
                text-align: left;
                font-weight: 600;
            }}
            td {{
                border: 1px solid #ddd;
                padding: 10px;
            }}
            tr:nth-child(even) {{
                background-color: #f9f9f9;
            }}
            tr:hover {{
                background-color: #f0f0f0;
            }}
            .footer {{
                text-align: center;
                margin-top: 30px;
                padding-top: 20px;
                border-top: 1px solid #ddd;
                color: #666;
                font-size: 0.9em;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Comparison Report</h1>
            
            <div class="timestamp">Generated: {timestamp}</div>
            
            <div class="summary">
                <div class="summary-card">
                    <h3>Total Comparisons</h3>
                    <div class="number">{total_comparisons}</div>
                </div>
                <div class="summary-card passed">
                    <h3>Passed</h3>
                    <div class="number">{passed_count}</div>
                </div>
                <div class="summary-card failed">
                    <h3>Failed</h3>
                    <div class="number">{failed_count}</div>
                </div>
                <div class="summary-card missing">
                    <h3>Missing Files</h3>
                    <div class="number">{missing_count}</div>
                </div>
                <div class="summary-card error">
                    <h3>Errors</h3>
                    <div class="number">{error_count}</div>
                </div>
            </div>
            
            <table>
                <thead>
                    <tr>
                        <th>Company Name</th>
                        <th>Report Type</th>
                        <th>Pay Period</th>
                        <th>Status</th>
                        <th>Details</th>
                    </tr>
                </thead>
                <tbody>
                    {rows_html}
                </tbody>
            </table>
            
            <div class="footer">
                <p>This report was automatically generated. For detailed comparisons, check the diff files in Google Drive.</p>
            </div>
        </div>
    </body>
    </html>
    """
    
    return html


def _launch_chromium(playwright):
    launch_attempts = (
        {},
        {"channel": "chrome"},
        {"channel": "msedge"},
    )
    last_error = None

    for launch_options in launch_attempts:
        try:
            return playwright.chromium.launch(headless=True, **launch_options)
        except Exception as exc:
            last_error = exc

    raise RuntimeError(
        "Unable to launch Chromium for report rendering. "
        "Run 'playwright install chromium' or install Chrome/Edge."
    ) from last_error


def render_html_report(html_content: str, output_format: str = "pdf") -> tuple[bytes, str, str]:
    """
    Render the HTML report to a portable artifact.

    Args:
        html_content: HTML string generated by generate_html_report()
        output_format: "pdf" or "png"

    Returns:
        Tuple of (file bytes, file extension, mime type)
    """
    output_format = output_format.lower()
    if output_format not in REPORT_OUTPUTS:
        supported = ", ".join(sorted(REPORT_OUTPUTS))
        raise ValueError(f"Unsupported report output format '{output_format}'. Use one of: {supported}.")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ImportError("Missing dependency. Run: pip install playwright") from exc

    extension, mime_type = REPORT_OUTPUTS[output_format]

    with sync_playwright() as playwright:
        browser = _launch_chromium(playwright)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.set_content(html_content, wait_until="networkidle")

            if output_format == "pdf":
                page.add_style_tag(content="""
                    @page { size: A4 landscape; margin: 10mm; }
                    @media print {
                        body { background: #fff; padding: 0; }
                        .container { max-width: none; box-shadow: none; }
                        table { font-size: 10px; table-layout: fixed; }
                        th, td { word-break: break-word; }
                    }
                """)
                rendered_bytes = page.pdf(
                    format="A4",
                    landscape=True,
                    print_background=True,
                    margin={
                        "top": "12mm",
                        "right": "10mm",
                        "bottom": "12mm",
                        "left": "10mm",
                    },
                )
            else:
                rendered_bytes = page.screenshot(full_page=True, type="png")
        finally:
            browser.close()

    return rendered_bytes, extension, mime_type

# ── Google Drive Integration ───────────────────────────────────────────────────

def get_or_create_reports_folder(service) -> str:
    """
    Get or create the root "reports" folder in Google Drive.
    
    Returns:
        Folder ID of the reports folder
    """
    try:
        # Try to find existing reports folder at root level
        reports_id = find_folder_by_name(service, "reports")
        return reports_id
    except FileNotFoundError:
        # Create new reports folder at root
        # Get root folder ID
        results = service.files().list(
            q="trashed=false and 'me' in owners",
            spaces='drive',
            pageSize=1,
            fields='files(id)',
            corpora='user'
        ).execute()
        
        # Upload to root by not specifying parent
        from googleapiclient.http import MediaFileUpload
        from googleapiclient.errors import HttpError
        import os as os_module
        
        file_metadata = {
            'name': 'reports',
            'mimeType': 'application/vnd.google-apps.folder'
        }
        folder = service.files().create(body=file_metadata, fields='id').execute()
        return folder.get('id')


def upload_report_file_to_drive(
    service,
    file_bytes: bytes,
    date_str: str,
    extension: str,
    mime_type: str,
) -> str:
    """
    Upload a rendered comparison report to Google Drive in the reports folder.
    """
    reports_folder_id = get_or_create_reports_folder(service)
    filename = f"{date_str}_comparison_report.{extension}"

    file_id = upload_or_update_file(
        service,
        filename,
        mime_type,
        reports_folder_id,
        file_bytes,
    )

    return file_id


def upload_report_to_drive(service, html_content: str, date_str: str, output_format: str = "pdf") -> str:
    """
    Render and upload the comparison report to Google Drive.
    
    Args:
        service: Google Drive service instance
        html_content: HTML string to render
        date_str: Date string for filename (e.g., "2024-01-15")
        output_format: "pdf" or "png"
    
    Returns:
        File ID of the uploaded report
    """
    file_bytes, extension, mime_type = render_html_report(html_content, output_format=output_format)
    return upload_report_file_to_drive(service, file_bytes, date_str, extension, mime_type)
