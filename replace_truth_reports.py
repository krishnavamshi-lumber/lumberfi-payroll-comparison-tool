"""
Streamlit app: Replace Truth Reports in Google Drive.

Pick a company, report type, pay period, and date. The reports already
uploaded to Drive under "{pay_period}_{date}" are pushed into that pay
period's "{pay_period}_truth" folder, overwriting any existing truth file
with the same name. Truth files with no same-named match in the selected
date's folder are left untouched.

Run with:
    streamlit run replace_truth_reports.py
"""

from __future__ import annotations

import re
from datetime import date

import streamlit as st

from utils.gdrive import (
    create_drive_service,
    download_file,
    find_folder_by_name,
    get_or_create_folder,
    list_files_in_folder,
    list_folders_in_folder,
    upload_or_update_file,
)

DATED_FOLDER_RE = re.compile(r"^(.*)_(\d{4}-\d{2}-\d{2})$")


@st.cache_resource
def get_service():
    return create_drive_service()


@st.cache_data(ttl=300)
def list_companies(_service) -> list[str]:
    prod_reports_id = find_folder_by_name(_service, "Prod_reports")
    folders = list_folders_in_folder(_service, prod_reports_id)
    return sorted(f["name"] for f in folders)


@st.cache_data(ttl=300)
def list_report_types(_service, company_name: str) -> list[str]:
    prod_reports_id = find_folder_by_name(_service, "Prod_reports")
    company_id = find_folder_by_name(_service, company_name, parent_id=prod_reports_id)
    folders = list_folders_in_folder(_service, company_id)
    return sorted(f["name"] for f in folders)


@st.cache_data(ttl=300)
def list_pay_periods(_service, company_name: str, report_type: str) -> list[str]:
    """Every pay period that has either a truth folder or a dated report folder."""
    prod_reports_id = find_folder_by_name(_service, "Prod_reports")
    company_id = find_folder_by_name(_service, company_name, parent_id=prod_reports_id)
    report_id = find_folder_by_name(_service, report_type, parent_id=company_id)
    folders = list_folders_in_folder(_service, report_id)

    periods: set[str] = set()
    for f in folders:
        name = f["name"]
        if name == "difference" or "_compare_" in name:
            continue
        if name.endswith("_truth"):
            periods.add(name[: -len("_truth")])
            continue
        match = DATED_FOLDER_RE.match(name)
        if match:
            periods.add(match.group(1))
    return sorted(periods)


def resolve_report_folder_id(service, company_name: str, report_type: str) -> str:
    prod_reports_id = find_folder_by_name(service, "Prod_reports")
    company_id = find_folder_by_name(service, company_name, parent_id=prod_reports_id)
    return find_folder_by_name(service, report_type, parent_id=company_id)


def main() -> None:
    st.set_page_config(page_title="Replace Truth Reports", layout="centered")
    st.title("Replace Truth Reports")
    st.caption(
        "Copies a date's already-uploaded Drive reports into that pay period's "
        "truth folder, overwriting matching filenames. Existing truth files with "
        "no match in the selected date's folder are left untouched."
    )

    service = get_service()

    company_name = st.selectbox("Company Name", list_companies(service))
    if not company_name:
        st.stop()

    report_type = st.selectbox("Report", list_report_types(service, company_name))
    if not report_type:
        st.stop()

    pay_periods = list_pay_periods(service, company_name, report_type)
    if not pay_periods:
        st.warning(f"No report folders found yet for {company_name} / {report_type}.")
        st.stop()
    pay_period = st.selectbox("Pay Period", pay_periods)

    selected_date = st.date_input("Date", value=date.today())

    if "replace_ctx" in st.session_state:
        ctx = st.session_state["replace_ctx"]
        if (ctx["company_name"], ctx["report_type"], ctx["pay_period"], ctx["date"]) != (
            company_name, report_type, pay_period, selected_date,
        ):
            del st.session_state["replace_ctx"]

    if st.button("Look Up Reports for This Date"):
        report_id = resolve_report_folder_id(service, company_name, report_type)
        source_folder_name = f"{pay_period}_{selected_date.isoformat()}"

        try:
            source_folder_id = find_folder_by_name(service, source_folder_name, parent_id=report_id)
        except FileNotFoundError:
            st.error(
                f"No report folder named '{source_folder_name}' found under "
                f"{company_name} / {report_type}. Reports for that date may not "
                "have been downloaded to Drive yet."
            )
            st.stop()

        source_files = list_files_in_folder(service, source_folder_id)
        if not source_files:
            st.warning(f"'{source_folder_name}' exists but has no files in it.")
            st.stop()

        try:
            truth_folder_id = find_folder_by_name(service, f"{pay_period}_truth", parent_id=report_id)
            truth_files = list_files_in_folder(service, truth_folder_id)
        except FileNotFoundError:
            truth_folder_id = None
            truth_files = []

        truth_names = {f["name"] for f in truth_files}
        source_names = {f["name"] for f in source_files}

        st.session_state["replace_ctx"] = {
            "company_name": company_name,
            "report_type": report_type,
            "pay_period": pay_period,
            "date": selected_date,
            "report_id": report_id,
            "truth_folder_id": truth_folder_id,
            "source_folder_name": source_folder_name,
            "source_files": source_files,
            "will_overwrite": sorted(source_names & truth_names),
            "will_add": sorted(source_names - truth_names),
            "untouched": sorted(truth_names - source_names),
        }

    ctx = st.session_state.get("replace_ctx")
    if not ctx:
        return

    st.subheader("Preview")
    st.write(f"Source folder: `{ctx['source_folder_name']}`")

    if ctx["will_overwrite"]:
        st.markdown("**Will overwrite (same filename already in truth folder):**")
        for name in ctx["will_overwrite"]:
            st.text(f"  • {name}")

    if ctx["will_add"]:
        st.markdown("**Will add (new filename, not currently in truth folder):**")
        for name in ctx["will_add"]:
            st.text(f"  • {name}")

    if ctx["untouched"]:
        st.markdown(
            f"**Left unchanged:** {len(ctx['untouched'])} existing truth file(s) "
            "with no match in the selected date's folder."
        )
        with st.expander("Show unchanged files"):
            for name in ctx["untouched"]:
                st.text(f"  • {name}")

    st.warning(
        "This will permanently overwrite the truth files listed above in Google Drive. "
        "This action cannot be undone from this tool."
    )
    confirm = st.checkbox("I understand this will overwrite the truth reports for this pay period.")

    if st.button("Replace Truth Reports", disabled=not confirm, type="primary"):
        truth_folder_id = ctx["truth_folder_id"]
        if truth_folder_id is None:
            truth_folder_id = get_or_create_folder(service, f"{ctx['pay_period']}_truth", ctx["report_id"])

        total = len(ctx["source_files"])
        progress = st.progress(0.0)
        for i, file_info in enumerate(ctx["source_files"], start=1):
            file_bytes = download_file(service, file_info["id"])
            upload_or_update_file(
                service,
                file_info["name"],
                file_info.get("mimeType", "application/octet-stream"),
                truth_folder_id,
                file_bytes,
            )
            progress.progress(i / total)

        st.success(
            f"Replaced {total} truth report(s) for "
            f"{ctx['company_name']} / {ctx['report_type']} / {ctx['pay_period']}."
        )
        del st.session_state["replace_ctx"]


if __name__ == "__main__":
    main()
