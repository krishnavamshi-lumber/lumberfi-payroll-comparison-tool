import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from utils.comparison import compare_any_files
from utils.report_generator import (
    extract_comparison_status,
    generate_html_report,
    render_html_report,
)

PDF_EXTENSIONS = {".pdf"}
CSV_EXTENSIONS = {".csv", ".xlsx", ".xls"}

TYPE_EXTENSIONS = {
    "both": PDF_EXTENSIONS | CSV_EXTENSIONS,
    "pdf": PDF_EXTENSIONS,
    "csv": CSV_EXTENSIONS,
}


def run_local_comparison(input_dir: Path, truth_dir: Path, compare_types: str) -> int:
    allowed_extensions = TYPE_EXTENSIONS[compare_types]

    today = date.today().strftime("%Y-%m-%d")
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    output_dir = Path("worker_comp")
    output_dir.mkdir(exist_ok=True)

    truth_files = {
        f.name: f for f in truth_dir.iterdir()
        if f.is_file() and f.suffix.lower() in allowed_extensions
    }
    input_files = {
        f.name: f for f in input_dir.iterdir()
        if f.is_file() and f.suffix.lower() in allowed_extensions
    }

    all_names = sorted(truth_files.keys() | input_files.keys())
    processed_results = []

    for filename in all_names:
        in_truth = filename in truth_files
        in_input = filename in input_files

        if not in_truth:
            print(f"[MISSING] {filename}: in input but not in truth")
            processed_results.append({
                "company_name": filename,
                "report_type": "-",
                "base_period": "-",
                "status": "MISSING",
                "details": [{"file_statuses": [], "missing_truth": [filename], "missing_compare": [], "errors": []}],
                "diff_folder_id": None,
            })
            continue

        if not in_input:
            print(f"[MISSING] {filename}: in truth but not in input")
            processed_results.append({
                "company_name": filename,
                "report_type": "-",
                "base_period": "-",
                "status": "MISSING",
                "details": [{"file_statuses": [], "missing_truth": [], "missing_compare": [filename], "errors": []}],
                "diff_folder_id": None,
            })
            continue

        truth_bytes = truth_files[filename].read_bytes()
        input_bytes = input_files[filename].read_bytes()

        try:
            output_bytes, output_extension, _ = compare_any_files(
                truth_bytes,
                input_bytes,
                truth_filename=filename,
                compare_filename=filename,
                project_name="",
                report_type="",
                truth_label="Truth",
                compare_label="Compare",
            )
        except Exception as exc:
            print(f"[ERROR] Failed comparing {filename}: {exc}")
            processed_results.append({
                "company_name": filename,
                "report_type": "-",
                "base_period": "-",
                "status": "ERROR",
                "details": [{"file_statuses": [], "missing_truth": [], "missing_compare": [], "errors": [str(exc)]}],
                "diff_folder_id": None,
            })
            continue

        diff_name = f"{Path(filename).stem}-diff.{output_extension}"
        diff_path = output_dir / diff_name
        diff_path.write_bytes(output_bytes)
        print(f"[DIFF] Saved: {diff_path}")

        file_status = extract_comparison_status(output_bytes, diff_name)
        file_status["filename"] = filename
        file_status["diff_filename"] = diff_name

        overall_status = file_status["status"]
        print(f"[STATUS] {filename} -> {overall_status} ({file_status.get('details')})")

        processed_results.append({
            "company_name": filename,
            "report_type": "-",
            "base_period": "-",
            "status": overall_status,
            "details": [{"file_statuses": [file_status], "missing_truth": [], "missing_compare": [], "errors": []}],
            "diff_folder_id": None,
        })

    print("\n[STEP] Generating comparison report")
    html_content = generate_html_report(processed_results, timestamp=timestamp_str)
    report_bytes, report_extension, _ = render_html_report(html_content, output_format="pdf")

    report_path = output_dir / f"{today}_comparison_report.{report_extension}"
    report_path.write_bytes(report_bytes)
    print(f"[INFO] Report saved to: {report_path}")

    passed = sum(1 for r in processed_results if r["status"] == "PASS")
    failed = sum(1 for r in processed_results if r["status"] == "FAIL")
    missing = sum(1 for r in processed_results if r["status"] == "MISSING")
    errors = sum(1 for r in processed_results if r["status"] == "ERROR")
    print(f"\n[SUMMARY] {len(processed_results)} files — {passed} PASS, {failed} FAIL, {missing} MISSING, {errors} ERROR")

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Compare local input and truth folders (PDF/CSV) and store diffs in difference_local."
    )
    parser.add_argument("--input", required=True, help="Path to the folder with files to compare")
    parser.add_argument("--truth", required=True, help="Path to the folder with truth files")
    parser.add_argument(
        "--types",
        choices=["both", "pdf", "csv"],
        default="both",
        help="Which file types to compare: both, pdf only, or csv/xlsx/xls only (default: both)",
    )
    args = parser.parse_args()

    input_dir = Path(args.input)
    truth_dir = Path(args.truth)

    if not input_dir.is_dir():
        print(f"[ERROR] Input folder not found: {input_dir}")
        sys.exit(1)
    if not truth_dir.is_dir():
        print(f"[ERROR] Truth folder not found: {truth_dir}")
        sys.exit(1)

    sys.exit(run_local_comparison(input_dir, truth_dir, args.types))
