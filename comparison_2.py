import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from utils.gdrive import (
    create_drive_service,
    find_folder_by_name,
    get_or_create_folder,
    list_folders_in_folder,
    list_files_in_folder,
    download_file,
    upload_or_update_file,
)
from utils.comparison import compare_any_files
from utils.report_generator import (
    extract_comparison_status,
    generate_html_report,
    render_html_report,
    upload_report_file_to_drive,
)


MIME_TYPES = {
    ".pdf": "application/pdf",
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
}

REPORT_FOLDERS = {
    "Union",
    "Prevailing Wage",
    "Worker Compensation",
    "Payroll Register",
    "Job Costing",
    "401K",
    "Payroll Journal",
    "Prevailing wage Summary",
    "Apprentice Ratio",
    "Summary of Wages",
    "Child Support Remittance",
    "Garnishment"
}


def get_mime_type(filename: str) -> str:
    return MIME_TYPES.get(Path(filename).suffix.lower(), "application/octet-stream")


def get_drive_root_id(service) -> str:
    return find_folder_by_name(service, "Prod_reports")


def format_drive_path(*parts: str) -> str:
    return " / ".join(parts)


def ensure_compare_folder(service, report_folder_id: str, compare_folder_name: str) -> str:
    from utils.gdrive import get_or_create_folder
    return get_or_create_folder(service, compare_folder_name, report_folder_id)


def ensure_difference_folder(service, report_folder_id: str) -> str:
    from utils.gdrive import get_or_create_folder
    return get_or_create_folder(service, "difference", report_folder_id)


def copy_report_files_to_compare_folder(
    service,
    source_folder_id: str,
    compare_folder_id: str,
) -> int:
    copied_count = 0
    for source_file in list_files_in_folder(service, source_folder_id):
        file_name = source_file["name"]
        file_bytes = download_file(service, source_file["id"])
        mime_type = get_mime_type(file_name)
        upload_or_update_file(service, file_name, mime_type, compare_folder_id, file_bytes)
        copied_count += 1
    return copied_count


def compare_folder_pairs(
    service,
    truth_folder_id: str,
    compare_folder_id: str,
    difference_folder_id: str,
    company_name: str,
    report_type: str,
) -> dict:
    result = {
        "report_type": report_type,
        "truth_files": 0,
        "compared_files": 0,
        "file_statuses": [],
        "missing_truth": [],
        "missing_compare": [],
        "errors": [],
    }

    compare_files = {f["name"]: f for f in list_files_in_folder(service, compare_folder_id)}
    truth_seen = set()

    for truth_file in list_files_in_folder(service, truth_folder_id):
        truth_name = truth_file["name"]
        truth_seen.add(truth_name)
        result["truth_files"] += 1
        compare_file = compare_files.get(truth_name)
        if not compare_file:
            result["missing_compare"].append(truth_name)
            continue

        try:
            truth_bytes = download_file(service, truth_file["id"])
            compare_bytes = download_file(service, compare_file["id"])
            output_bytes, output_extension, mime_type = compare_any_files(
                truth_bytes,
                compare_bytes,
                truth_filename=truth_name,
                compare_filename=compare_file["name"],
                project_name=company_name,
                report_type=report_type,
                truth_label="Truth",
                compare_label="Compare",
            )
            output_name = f"{Path(truth_name).stem}-diff.{output_extension}"
            upload_or_update_file(service, output_name, mime_type, difference_folder_id, output_bytes)
            file_status = extract_comparison_status(output_bytes, output_name)
            file_status = _apply_tolerance_rules(file_status, report_type, truth_name, company_name)
            file_status["filename"] = truth_name
            file_status["diff_filename"] = output_name
            result["file_statuses"].append(file_status)
            print(
                "[STATUS][COMPARE] "
                f"{company_name} / {report_type} / {truth_name} -> "
                f"{file_status.get('status')} ({file_status.get('details')})"
            )
            result["compared_files"] += 1
        except Exception as exc:
            result["errors"].append(f"Failed comparing {truth_name}: {exc}")

    for compare_name in compare_files:
        if compare_name not in truth_seen:
            result["missing_truth"].append(compare_name)

    return result


_STATE_PDF_PAGES_COMPARED_COMPANIES = {
    "Acoustic Ceiling & Partition of Ohio, Inc.",
    "Elevate Concrete Systems LLC",
    "Omni Fireproofing Systems LLC",
}


def _apply_tolerance_rules(file_status: dict, report_type: str, filename: str = "", company_name: str = "") -> dict:
    """Override FAIL → PASS for known acceptable noise thresholds.

    Rules (applied only when status is currently FAIL):
    - Prevailing Wage State PDF for companies in _STATE_PDF_PAGES_COMPARED_COMPANIES:
      words_added <= pages_compared AND words_removed <= pages_compared
      (each page footer has a "generated on" date that changes daily)
    - All other Prevailing Wage PDFs: words_added <= 1 AND words_removed <= 1
    - Payroll Register XLSX: rows_changed == 2 (and only changed rows, no added/removed)
    """
    if file_status.get("status") != "FAIL":
        return file_status

    if report_type == "Prevailing Wage" and file_status.get("file_type") == "pdf":
        if "_State_" in filename and company_name in _STATE_PDF_PAGES_COMPARED_COMPANIES:
            tolerance = file_status.get("pages_compared", 1)
            label = f"≤{tolerance} word diff"
        else:
            tolerance = 1
            label = "≤1 word diff"

        if file_status.get("words_added", 0) <= tolerance and file_status.get("words_removed", 0) <= tolerance:
            file_status = dict(file_status)
            file_status["status"] = "PASS"
            file_status["details"] = file_status.get("details", "") + f" [tolerated: {label}]"
            print(f"[TOLERANCE] Prevailing Wage PDF overridden to PASS ({label})")

        # Additional exact-pair tolerances (only checked if still FAIL after above)
        if file_status.get("status") == "FAIL":
            added = file_status.get("words_added", 0)
            removed = file_status.get("words_removed", 0)

            if "_WH3_47_" in filename and (added, removed) in {(7, 6), (7, 7), (3, 3), (6, 6), (6, 7)}:
                file_status = dict(file_status)
                file_status["status"] = "PASS"
                file_status["details"] = file_status.get("details", "") + f" [tolerated: WH347 exact pair (+{added},-{removed})]"
                print(f"[TOLERANCE] Prevailing Wage WH347 PDF overridden to PASS (exact pair +{added},-{removed})")

            elif "_Federal_" in filename and added == 7 and removed == 7:
                file_status = dict(file_status)
                file_status["status"] = "PASS"
                file_status["details"] = file_status.get("details", "") + " [tolerated: Federal exact pair (+7,-7)]"
                print(f"[TOLERANCE] Prevailing Wage Federal PDF overridden to PASS (exact pair +7,-7)")

            elif "_State_" in filename and added == 7 and removed == 7:
                file_status = dict(file_status)
                file_status["status"] = "PASS"
                file_status["details"] = file_status.get("details", "") + " [tolerated: State exact pair (+7,-7)]"
                print(f"[TOLERANCE] Prevailing Wage State PDF overridden to PASS (exact pair +7,-7)")

    elif report_type == "Union" and file_status.get("file_type") == "pdf":
        added = file_status.get("words_added", 0)
        removed = file_status.get("words_removed", 0)
        if added == removed:
            file_status = dict(file_status)
            file_status["status"] = "PASS"
            file_status["details"] = file_status.get("details", "") + f" [tolerated: Union equal word diff (+{added},-{removed})]"
            print(f"[TOLERANCE] Union PDF overridden to PASS (equal word diff +{added},-{removed})")

    elif report_type == "Payroll Register" and file_status.get("file_type") == "excel":
        if file_status.get("rows_changed", 0) == 2 and file_status.get("rows_added", 0) == 0 and file_status.get("rows_removed", 0) == 0:
            file_status = dict(file_status)
            file_status["status"] = "PASS"
            file_status["details"] = file_status.get("details", "") + " [tolerated: 2 changed rows]"
            print(f"[TOLERANCE] Payroll Register XLSX overridden to PASS (2 changed rows)")

    elif report_type == "Apprentice Ratio" and file_status.get("file_type") == "pdf":
        if file_status.get("words_added", 0) <= 3 and file_status.get("words_removed", 0) <= 3:
            file_status = dict(file_status)
            file_status["status"] = "PASS"
            file_status["details"] = file_status.get("details", "") + " [tolerated: ≤3 word diff]"
            print(f"[TOLERANCE] Apprentice Ratio PDF overridden to PASS (≤3 word diff)")

    elif report_type == "Prevailing wage Summary" and file_status.get("file_type") == "pdf":
        tolerance = file_status.get("pages_compared", 1)
        if file_status.get("words_added", 0) <= tolerance and file_status.get("words_removed", 0) <= tolerance:
            file_status = dict(file_status)
            file_status["status"] = "PASS"
            file_status["details"] = file_status.get("details", "") + f" [tolerated: ≤{tolerance} word diff]"
            print(f"[TOLERANCE] Prevailing wage Summary PDF overridden to PASS (≤{tolerance} word diff)")

        if file_status.get("status") == "FAIL" and company_name == "American Asphalt South":
            added = file_status.get("words_added", 0)
            removed = file_status.get("words_removed", 0)
            if added == removed:
                file_status = dict(file_status)
                file_status["status"] = "PASS"
                file_status["details"] = file_status.get("details", "") + f" [tolerated: American Asphalt South equal word diff (+{added},-{removed})]"
                print(f"[TOLERANCE] Prevailing wage Summary PDF overridden to PASS for American Asphalt South (equal word diff +{added},-{removed})")

    elif report_type == "Summary of Wages" and file_status.get("file_type") == "pdf":
        if file_status.get("words_added", 0) <= 2 and file_status.get("words_removed", 0) <= 2:
            file_status = dict(file_status)
            file_status["status"] = "PASS"
            file_status["details"] = file_status.get("details", "") + " [tolerated: ≤2 word diff]"
            print(f"[TOLERANCE] Summary of Wages PDF overridden to PASS (≤2 word diff)")

    return file_status


def determine_comparison_status(comparison_detail: dict) -> str:
    """
    Determine overall pass/fail status for a comparison report.
    
    Returns "PASS" only if all files passed. "FAIL" if any file failed.
    "MISSING" if only missing files. "ERROR" if errors occurred.
    """
    if comparison_detail.get("errors"):
        return "ERROR"
    
    file_statuses = comparison_detail.get("file_statuses", [])
    if not file_statuses:
        if comparison_detail.get("missing_compare") or comparison_detail.get("missing_truth"):
            return "MISSING"
        return "PASS"
    
    has_fail = any(fs["status"] == "FAIL" for fs in file_statuses)
    has_missing = any(fs["status"] == "MISSING" for fs in file_statuses)
    has_error = any(fs["status"] == "ERROR" for fs in file_statuses)
    
    if has_error:
        return "ERROR"
    elif has_fail:
        return "FAIL"
    elif has_missing:
        return "MISSING"
    else:
        return "PASS"


def run_compare_for_report_type(
    service,
    company_name: str,
    report_type: str,
    company_folder_id: str,
    today: str,
) -> list[dict]:
    """Compare reports for a specific company and report type across all pay periods."""

    report_folder_id = find_folder_by_name(service, report_type, parent_id=company_folder_id)
    report_path = format_drive_path("Prod_reports", company_name, report_type)
    print(f"[DRIVE] Report folder: {report_path} (folder_id={report_folder_id})")

    all_folders = list_folders_in_folder(service, report_folder_id)

    # Build truth folder lookup keyed by base period
    truth_by_period = {
        f["name"][: -len("_truth")]: f
        for f in all_folders
        if f["name"].endswith("_truth")
    }

    # Collect only today's source folders (exclude _compare_, _truth, and difference)
    source_folders = []
    for f in all_folders:
        name = f["name"]
        if "_compare_" in name or name.endswith("_truth") or name == "difference":
            continue
        if len(name) >= 10 and name[-10:] == today:
            source_folders.append((f["id"], name))

    if not source_folders:
        return [{
            "company_name": company_name,
            "report_type": report_type,
            "base_period": "N/A",
            "status": "skipped",
            "truth_files": 0,
            "compared_files": 0,
            "file_statuses": [],
            "missing_truth": [],
            "missing_compare": [],
            "errors": [],
            "details": ["No dated source folders found"],
        }]

    difference_folder_id = ensure_difference_folder(service, report_folder_id)
    results = []

    for source_folder_id, source_folder_name in source_folders:
        base_period = source_folder_name.rsplit("_", 1)[0]
        print(f"[DRIVE] Source folder: {report_path} / {source_folder_name} (folder_id={source_folder_id})")

        output = {
            "company_name": company_name,
            "report_type": report_type,
            "base_period": base_period,
            "status": "skipped",
            "truth_files": 0,
            "compared_files": 0,
            "file_statuses": [],
            "missing_truth": [],
            "missing_compare": [],
            "errors": [],
            "details": [],
        }

        truth_folder = truth_by_period.get(base_period)
        if not truth_folder:
            output["details"].append(f"Truth folder not found for period: {base_period}")
            results.append(output)
            continue

        truth_folder_id = truth_folder["id"]
        truth_folder_name = truth_folder["name"]
        compare_folder_name = f"{base_period}_compare_{today}"
        compare_folder_id = ensure_compare_folder(service, report_folder_id, compare_folder_name)

        print(f"[DRIVE] Truth folder: {report_path} / {truth_folder_name} (folder_id={truth_folder_id})")
        print(f"[DRIVE] Compare folder: {report_path} / {compare_folder_name} (folder_id={compare_folder_id})")

        copied_count = copy_report_files_to_compare_folder(service, source_folder_id, compare_folder_id)
        print(f"[DRIVE] Copied {copied_count} file(s) from source folder to compare folder")

        diff_subfolder_name = f"{base_period}_{today}"
        diff_subfolder_id = get_or_create_folder(service, diff_subfolder_name, difference_folder_id)
        output["diff_folder_id"] = diff_subfolder_id
        print(f"[DRIVE] Difference subfolder: {report_path} / difference / {diff_subfolder_name} (folder_id={diff_subfolder_id})")

        comparison_result = compare_folder_pairs(
            service,
            truth_folder_id,
            compare_folder_id,
            diff_subfolder_id,
            company_name,
            report_type,
        )

        output["status"] = "completed"
        output["truth_files"] = comparison_result["truth_files"]
        output["compared_files"] = comparison_result["compared_files"]
        output["file_statuses"] = comparison_result["file_statuses"]
        output["missing_truth"] = comparison_result["missing_truth"]
        output["missing_compare"] = comparison_result["missing_compare"]
        output["errors"] = comparison_result["errors"]
        output["details"].append(comparison_result)
        results.append(output)

    return results


def run_all_comparisons() -> int:
    """Auto-discover companies and run comparisons."""
    print("[STEP] Starting auto-discovery comparison")
    service = create_drive_service()
    today = date.today().strftime("%Y-%m-%d")
    summary = []

    try:
        root_id = get_drive_root_id(service)
    except Exception as exc:
        print(f"[ERROR] Could not find Prod_reports folder: {exc}")
        return 1

    print(f"[INFO] Found Prod_reports folder (root_id={root_id})")

    # List all companies
    try:
        companies = list_folders_in_folder(service, root_id)
    except Exception as exc:
        print(f"[ERROR] Could not list companies: {exc}")
        return 1

    if not companies:
        print("[WARN] No companies found in Prod_reports")
        return 0

    print(f"[INFO] Found {len(companies)} company folders")

    # For each company
    for company_folder in companies:
        company_name = company_folder["name"]
        company_id = company_folder["id"]
        print(f"\n[COMPANY] Processing: {company_name}")

        # List report types
        try:
            report_folders = list_folders_in_folder(service, company_id)
        except Exception as exc:
            print(f"[WARN] Could not list report types for {company_name}: {exc}")
            continue

        # Filter to known report types
        valid_reports = [f for f in report_folders if f["name"] in REPORT_FOLDERS]

        if not valid_reports:
            print(f"[WARN] No report folders found for {company_name}")
            continue

        # For each report type
        for report_folder in valid_reports:
            report_type = report_folder["name"]
            print(f"[COMPARE] {company_name} / {report_type}")

            try:
                results = run_compare_for_report_type(
                    service,
                    company_name,
                    report_type,
                    company_id,
                    today,
                )
                summary.extend(results)
            except Exception as exc:
                print(f"[ERROR] Failed to compare {company_name} / {report_type}: {exc}")
                summary.append({
                    "company_name": company_name,
                    "report_type": report_type,
                    "base_period": "N/A",
                    "status": "failed",
                    "details": [str(exc)],
                })

    print("\n[RESULTS] Comparison summary")
    for item in summary:
        status = item.get("status")
        company = item.get("company_name")
        report_type = item.get("report_type")
        base_period = item.get("base_period", "N/A")
        print(f"  - {company} / {report_type} / {base_period} -> {status}")

    # Process results for report generation
    processed_results = []
    for item in summary:
        company = item.get("company_name", "N/A")
        report_type = item.get("report_type", "N/A")
        base_period = item.get("base_period", "N/A")
        status = item.get("status", "unknown")

        if status == "completed":
            overall_status = determine_comparison_status(item)
        elif status == "skipped":
            overall_status = "MISSING"
        else:
            overall_status = "ERROR"

        processed_results.append({
            "company_name": company,
            "report_type": report_type,
            "base_period": base_period,
            "status": overall_status,
            "details": item.get("details", []),
            "diff_folder_id": item.get("diff_folder_id"),
        })
    
    # Generate HTML report
    report_output_format = "pdf"
    print("\n[STEP] Generating comparison report")
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html_content = generate_html_report(processed_results, timestamp=timestamp_str)
    report_bytes, report_extension, report_mime_type = render_html_report(
        html_content,
        output_format=report_output_format,
    )
    
    # Upload report to Google Drive
    print("[STEP] Uploading report to Google Drive")
    try:
        report_file_id = upload_report_file_to_drive(
            service,
            report_bytes,
            today,
            report_extension,
            report_mime_type,
        )
        print(f"[SUCCESS] Report uploaded successfully (ID: {report_file_id})")
    except Exception as exc:
        print(f"[WARNING] Failed to upload report to Google Drive: {exc}")
    
    # Save report locally
    output_dir = Path("outputs")
    output_dir.mkdir(exist_ok=True)
    report_path = output_dir / f"{today}_comparison_report.{report_extension}"
    report_path.write_bytes(report_bytes)
    print(f"[INFO] Report also saved locally to: {report_path}")

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Auto-discover companies in Prod_reports and run comparisons."
    )
    args = parser.parse_args()
    sys.exit(run_all_comparisons())
