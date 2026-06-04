import argparse
import json
import os
import re
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from utils.gdrive import (
    create_drive_service,
    get_or_create_folder as drive_get_or_create_folder,
    upload_or_update_file,
)

# ── Thread-local state so every log line knows which port it came from ──
_thread_state = threading.local()

def log(msg: str) -> None:
    """Print with a [port=XXXX] prefix when called from a worker thread."""
    port = getattr(_thread_state, "port", None)
    prefix = f"[port={port}] " if port is not None else ""
    print(f"{prefix}{msg}", flush=True)


class FailureLogger:
    """Thread-safe real-time failure/skip/zero-byte log writer."""

    def __init__(self, filepath: Path):
        self._filepath = filepath
        self._lock = threading.Lock()
        with filepath.open("w", encoding="utf-8") as f:
            f.write("Report Download Failure Log\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 80 + "\n\n")

    def _write(self, line: str) -> None:
        with self._lock:
            with self._filepath.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()

    def _ctx(self) -> tuple[str, str]:
        company = getattr(_thread_state, "company_name", "unknown")
        start = getattr(_thread_state, "start_date", "?")
        end = getattr(_thread_state, "end_date", "?")
        return company, f"{start} to {end}"

    def log_skip(self, report_name: str, project: str = "") -> None:
        company, pay_period = self._ctx()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        parts = [f"[{ts}] SKIPPED", f"Company: {company}", f"Pay Period: {pay_period}", f"Report: {report_name}"]
        if project:
            parts.append(f"Project: {project}")
        self._write(" | ".join(parts))

    def log_failure(self, report_name: str, project: str = "", reason: str = "") -> None:
        company, pay_period = self._ctx()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        parts = [f"[{ts}] FAILED", f"Company: {company}", f"Pay Period: {pay_period}", f"Report: {report_name}"]
        if project:
            parts.append(f"Project: {project}")
        if reason:
            parts.append(f"Reason: {reason[:300]}")
        self._write(" | ".join(parts))

    def log_zero_byte(self, filename: str) -> None:
        company, pay_period = self._ctx()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        parts = [f"[{ts}] ZERO_BYTE", f"Company: {company}", f"Pay Period: {pay_period}", f"File: {filename}"]
        self._write(" | ".join(parts))


_failure_logger: FailureLogger | None = None


BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DEFAULT_CONFIG_PATH = BASE_DIR / "garnishment_report.json"


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def safe_text(text: str) -> str:
    cleaned = re.sub(r'[<>:"/|?*\n\r]+', "_", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:240]


def load_config(config_path: Path) -> list[dict]:
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, list):
        raise ValueError("Configuration file must contain a JSON array of company jobs.")
    return config


def get_drive_service() -> object:
    return create_drive_service()


def get_or_create_folder(service, parent_id: str, name: str) -> str:
    return drive_get_or_create_folder(service, name, parent_id)


def upload_file(service, filepath: Path, folder_id: str, name: str, mimetype: str) -> None:
    file_id = upload_or_update_file(service, name, mimetype, folder_id, filepath.read_bytes())
    log(f"Uploaded {name} to Google Drive folder_id={folder_id} file_id={file_id}")


def log_drive_folder(label: str, path_parts: list[str], folder_id: str) -> None:
    log(f"[DRIVE] {label}: {' / '.join(path_parts)} (folder_id={folder_id})")


def get_base_url(page) -> str:
    match = re.match(r"^(https?://[^/]+)", page.url)
    if not match:
        raise RuntimeError(f"Unable to determine base URL from current page: {page.url}")
    return match.group(1)


def select_company(page, company_name: str) -> bool:
    try:
        chevrons = page.locator('svg[data-testid="Chevron DownIcon"]')
        expect(chevrons.last).to_be_visible(timeout=120000)
        chevrons.last.click()
        page.wait_for_timeout(1000)

        search_inputs = page.locator('input[placeholder="Search"]')
        expect(search_inputs.last).to_be_visible(timeout=120000)
        search_input = search_inputs.last
        search_input.fill(company_name)
        search_input.press("Enter")
        page.wait_for_timeout(1000)

        company_option = page.locator(f'//li[contains(normalize-space(.), "{company_name}")]').first
        expect(company_option).to_be_visible(timeout=120000)
        company_option.click()
        page.wait_for_timeout(10000)
        return True
    except Exception as exc:
        log(f"[ERROR] Failed to select company '{company_name}': {exc}")
        return False


def navigate_to_report(page, report_path: str) -> None:
    url = get_base_url(page).rstrip("/") + report_path
    page.goto(url, timeout=60000, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        log("[WARN] networkidle wait timed out; continuing after DOM content loaded")
    page.wait_for_timeout(7000)
    log(f"[OK] Navigated to report page: {url}")


def select_prevailing_wage_project(page, project_name: str) -> bool:

    page.wait_for_timeout(10000)
    try:
        project_dropdown = page.locator('div[data-testid="reports-select-project-dropdown"]')
        expect(project_dropdown).to_be_visible(timeout=120000)
        project_dropdown.click()
        page.wait_for_timeout(1000)

        search_inputs = page.locator('input[placeholder="Search Projects"]')
        expect(search_inputs.last).to_be_visible(timeout=120000)
        project_search = search_inputs.last
        project_search.fill(project_name)
        project_search.press("Enter")
        page.wait_for_timeout(2000)
        project_option = page.locator(f'//li[contains(normalize-space(.), "{project_name}")]')
        expect(project_option).to_be_visible(timeout=120000)
        project_option.click()
        page.wait_for_timeout(5000)
        return True
    except Exception as exc:
        log(f"[ERROR] Failed to select project '{project_name}': {exc}")
        return False


def ensure_download_button(page):
    button = page.locator('button[data-testid="reports-download-button"]')
    if button.count() > 0:
        return button
    return page.get_by_role("button", name="Download Report")


def save_and_upload_download(
    service, download, local_path: Path, folder_id: str, upload_name: str, mimetype: str
) -> bool:
    # Give every download a unique permanent filename using pid + timestamp.
    # No temp files, no deletion before save, no rename — just write directly.
    unique_id = f"{os.getpid()}_{int(time.time() * 1000)}"
    save_path = local_path.parent / f"{unique_id}_{local_path.name}"

    try:
        download.save_as(str(save_path))

        if not save_path.exists():
            log(f"[ERROR] save_as completed but file not found on disk: {upload_name}")
            if _failure_logger:
                _failure_logger.log_failure(upload_name, reason="file not found on disk after save_as")
            return False

        file_size = save_path.stat().st_size
        if file_size == 0:
            # Log the download's failure reason if Playwright captured one
            failure = download.failure()
            log(f"[ERROR] Downloaded file is 0 bytes: {upload_name} | browser failure reason: {failure!r}")
            if _failure_logger:
                _failure_logger.log_zero_byte(upload_name)
            return False

        log(f"[DEBUG] File ready for upload: {upload_name} ({file_size:,} bytes)")
        upload_file(service, save_path, folder_id, upload_name, mimetype)
        return True

    except Exception as exc:
        log(f"[ERROR] Failed to save/upload '{upload_name}': {exc}")
        if _failure_logger:
            _failure_logger.log_failure(upload_name, reason=str(exc)[:300])
        traceback.print_exc()
        return False

    finally:
        # Clean up after successful upload — comment this out if you want
        # to keep local copies for inspection
        save_path.unlink(missing_ok=True)


def download_pdf_report(service, page, folder_id: str, filename: str) -> bool:
    try:
        download_btn = ensure_download_button(page)
        expect(download_btn).to_be_enabled(timeout=120000)
        download_btn.click()

        pdf_option = page.locator('//li[contains(normalize-space(.), "PDF")]')
        expect(pdf_option).to_be_visible(timeout=120000)
        with page.expect_download(timeout=120000) as dl:
            pdf_option.click()
        success = save_and_upload_download(service, dl.value, DOWNLOAD_DIR / filename, folder_id, filename, "application/pdf")
        page.click("body", position={"x": 100, "y": 100})
        if not success:
            log(f"[WARN] PDF upload failed for '{filename}'")
            return False
        return True
    except Exception as exc:
        log(f"[WARN] Failed to download PDF report '{filename}': {exc}")
        if _failure_logger:
            _failure_logger.log_failure(filename, reason=str(exc)[:300])
        return False


def download_csv_report(service, page, folder_id: str, filename: str, menu_selector: str | None = None) -> bool:
    try:
        download_btn = ensure_download_button(page)
        expect(download_btn).to_be_enabled(timeout=120000)

        # Resolve which dropdown item to click.
        # Previously the else-branch skipped clicking the download button, which
        # left the dropdown closed; Playwright would briefly resolve the locator
        # against a still-animating-closed menu from a prior action, then the
        # element detached before the click landed.  Both paths now open the
        # dropdown explicitly, and the button click is inside expect_download so
        # the event is never missed.
        selector = menu_selector or 'li[data-testid="reports-download-csv-option"]'

        with page.expect_download(timeout=120000) as dl:
            download_btn.click()
            option = page.locator(selector)
            expect(option).to_be_visible(timeout=120000)
            option.click()

        save_and_upload_download(service, dl.value, DOWNLOAD_DIR / filename, folder_id, filename, "text/csv")
        page.click("body", position={"x": 100, "y": 100})
        return True
    except Exception as exc:
        log(f"[WARN] Failed to download CSV report '{filename}': {exc}")
        if _failure_logger:
            _failure_logger.log_failure(filename, reason=str(exc)[:300])
        return False


def select_pay_period(page, start_date: str, end_date: str) -> bool:
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    formatted_period = f"{start_dt.strftime('%m/%d/%Y')} – {end_dt.strftime('%m/%d/%Y')}"

    page.click("body", position={"x": 100, "y": 100})
    page.wait_for_timeout(2000)
    page.click("body", position={"x": 100, "y": 100})
    try:
        pay_period_dropdown = page.locator('svg[data-testid="ExpandMoreIcon"]')
        expect(pay_period_dropdown).to_be_visible(timeout=120000)
        pay_period_dropdown.click()
        page.wait_for_timeout(2000)

        pay_period_locator = page.locator(
            f'//ul[@data-testid="pay-period-dropdown"]'
            f' //div[.//p[contains(normalize-space(), "Paid")]]'
            f' //li[contains(normalize-space(.), "{formatted_period}")]'
        )
        expect(pay_period_locator.first).to_be_visible(timeout=120000)
        target = pay_period_locator.nth(0) if pay_period_locator.count() > 1 else pay_period_locator
        checkbox = target.locator('input[type="checkbox"]')
        checkbox.click()
        page.click("body", position={"x": 100, "y": 100})
        page.wait_for_timeout(5000)
        return True
    except Exception as exc:
        log(f"[WARN] Pay period not found for '{formatted_period}': {exc}")
        return False


def select_pay_period_without_paid_section(page, start_date: str, end_date: str) -> bool:
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    formatted_period = f"{start_dt.strftime('%m/%d/%Y')} – {end_dt.strftime('%m/%d/%Y')}"

    page.click("body", position={"x": 100, "y": 100})
    try:
        pay_period_dropdown = page.locator('svg[data-testid="ExpandMoreIcon"]')
        expect(pay_period_dropdown).to_be_visible(timeout=120000)
        pay_period_dropdown.click()
        page.wait_for_timeout(2000)

        pay_period_locator = page.locator(
            f'//ul[@data-testid="pay-period-dropdown"]'
            f' //li[contains(normalize-space(.), "{formatted_period}")]'
        )
        expect(pay_period_locator.first).to_be_visible(timeout=120000)
        target_li = pay_period_locator.first

        # Structure 2: checkbox is directly inside the <li>
        checkbox = target_li.locator('input[type="checkbox"]')
        if checkbox.count() == 0:
            # Structure 1: grouped periods where the <li> sits inside a child MuiBox-root div,
            # and the checkbox lives in a sibling MuiBox-root div — 2 ancestor levels up
            checkbox = target_li.locator(
                'xpath=ancestor::div[contains(@class,"MuiBox-root")][2]//input[@type="checkbox"]'
            )

        if checkbox.count() > 0:
            expect(checkbox.first).to_be_attached(timeout=120000)
            checkbox.first.check(force=True)
        else:
            target_li.click()

        page.click("body", position={"x": 100, "y": 100})
        page.wait_for_timeout(5000)

        return True
    except Exception as exc:
        log(f"[WARN] Dropdown method failed for '{formatted_period}': {exc}")
        # Fallback: try using date input field
        try:
            date_input = page.locator('input[placeholder="MM/DD/YYYY – MM/DD/YYYY"]')
            expect(date_input).to_be_visible(timeout=120000)
            date_input.dblclick()
            page.keyboard.press("Control+A")
            date_input.fill(formatted_period)
            page.wait_for_timeout(2000)
            expect(date_input).to_have_value(formatted_period, timeout=120000)
            page.wait_for_timeout(5000)
            return True
        except Exception as exc_fallback:
            log(f"[WARN] Fallback date input method also failed for '{formatted_period}': {exc_fallback}")
            return False


def select_prevailing_wage_week(page, start_date: str, end_date: str) -> bool:
    """Navigate to the correct week for prevailing wage reports using week navigation buttons."""
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    target_period_display = f"{start_dt.strftime('%b %d')} - {end_dt.strftime('%b %d')}"

    try:
        current_week_locator = page.locator('div[data-testid="reports-current-week"]')
        if current_week_locator.count() == 0:
            current_week_locator = page.locator('div:has-text("–")')

        expect(current_week_locator).to_be_visible(timeout=120000)

        max_week_attempts = 50
        week_found = False

        for week_attempt in range(1, max_week_attempts + 1):
            current_week_text = current_week_locator.text_content() or ""
            log(f"[INFO] Week attempt {week_attempt}/{max_week_attempts}: {current_week_text}")

            if target_period_display in current_week_text:
                week_found = True
                log(f"[OK] Correct week found: {current_week_text}")
                page.wait_for_timeout(2000)
                return True

            try:
                date_match = re.search(r"(\w{3} \d{1,2}) - (\w{3} \d{1,2})", current_week_text)
                if date_match:
                    displayed_start_str = date_match.group(1) + f" {start_dt.year}"
                    displayed_end_str = date_match.group(2) + f" {end_dt.year}"
                    displayed_start = datetime.strptime(displayed_start_str, "%b %d %Y").date()

                    if displayed_start > start_dt.date():
                        prev_button = page.locator('button[data-testid="reports-previous-week-button"]')
                        if prev_button.count() > 0:
                            prev_button.click()
                            log("[INFO] Navigating to previous week")
                    else:
                        next_button = page.locator('button[data-testid="reports-next-week-button"]')
                        if next_button.count() > 0:
                            next_button.click()
                            log("[INFO] Navigating to next week")
            except (ValueError, AttributeError):
                pass

            page.wait_for_timeout(1000)

        log(f"[WARN] Week '{target_period_display}' not found after {max_week_attempts} attempts")
        return False

    except Exception as exc:
        log(f"[WARN] Failed to navigate to prevailing wage week: {exc}")
        return False


def format_pay_period_for_report(start_date: str, end_date: str) -> str:
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    # Extract the date part from end_date (remove any suffix like "(Regular)" or "(Off Cycle)")
    end_date_clean = end_date.split()[0] if end_date else end_date
    end_dt = datetime.strptime(end_date_clean, "%Y-%m-%d")
    return f"{start_dt.strftime('%d %b %Y')} - {end_dt.strftime('%d %b %Y')}"

def format_pay_period_for_summary_of_wages(start_date: str, end_date: str) -> str:
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    # Extract the date part from end_date (remove any suffix like "(Regular)" or "(Off Cycle)")
    end_date_clean = end_date.split()[0] if end_date else end_date
    end_dt = datetime.strptime(end_date_clean, "%Y-%m-%d")
    return f"{start_dt.strftime('%m/%d/%Y')} – {end_dt.strftime('%m/%d/%Y')}"


def extract_pay_period_suffix(end_date: str) -> str:
    """Extract the suffix from end_date like '(Regular)' or '(Off Cycle)'."""
    match = re.search(r'\((.*?)\)$', end_date.strip())
    return f"({match.group(1)})" if match else ""


def select_pay_period_for_payroll_register(page, start_date: str, end_date: str, item_index: int = 0) -> bool:
    """Select a pay period on the Payroll Register UI.

    Args:
        item_index: 0-based index of which matching item to pick when multiple exist.
                    Defaults to 0 (first match). Pass 1 to pick the second match, etc.
    """
    pay_period_text = format_pay_period_for_report(start_date, end_date)
    suffix = extract_pay_period_suffix(end_date)
    # Build the full search text with suffix if present (e.g., "04 May 2026 - 10 May 2026 (Regular)")
    search_text = f"{pay_period_text} {suffix}".strip()
    item_index = item_index - 1 if item_index >= 0 else 0
    log(f"[DEBUG] Looking for pay period with text: '{search_text}' (item_index={item_index})")

    try:
        select_button = page.locator('//button[contains(normalize-space(.), "Select the pay period")]')
        expect(select_button).to_be_visible(timeout=120000)
        select_button.click()
        page.wait_for_timeout(3000)

        max_view_more_attempts = 15
        view_more_clicked_count = 0

        for attempt in range(max_view_more_attempts + 1):
            try:
                # First try to find exact match with suffix if present
                if suffix:
                    pay_period_item = page.locator(f'//li[contains(normalize-space(.), "{search_text}")]')
                    count = pay_period_item.count()
                    log(f"[DEBUG] Found {count} matching items with full search text on attempt {attempt}")
                    if count > item_index:
                        target = pay_period_item.nth(item_index)
                        expect(target).to_be_visible(timeout=120000)
                        target.click()
                        page.wait_for_timeout(10000)
                        log(f"[OK] Selected payroll period with suffix (index={item_index}): {search_text}")
                        return True
                    # Not enough items yet — click View More and retry, skip fallback
                    if attempt < max_view_more_attempts:
                        view_more_button = page.locator('//div[normalize-space(text())="View More"]')
                        if view_more_button.count() > 0:
                            log(f"[DEBUG] Clicking View More button (attempt {attempt + 1})")
                            view_more_button.click()
                            page.wait_for_timeout(4000)
                            view_more_clicked_count += 1
                        else:
                            log(f"[DEBUG] No View More button found after {view_more_clicked_count} clicks, cannot load more items for '{search_text}'")
                            break
                    continue

                # Fallback: find by date and then filter by suffix
                pay_period_items = page.locator(f'//li[contains(normalize-space(.), "{pay_period_text}")]')
                count = pay_period_items.count()
                if count > 0:
                    log(f"[DEBUG] Found {count} items matching date text on attempt {attempt}")
                    if suffix:
                        # With suffix: pick the item_index-th item whose text contains the suffix
                        match_count = 0
                        for i in range(count):
                            item_text = pay_period_items.nth(i).text_content() or ""
                            log(f"[DEBUG] Checking item {i}: {item_text}")
                            if suffix.strip("()") in item_text:
                                if match_count == item_index:
                                    expect(pay_period_items.nth(i)).to_be_visible(timeout=120000)
                                    pay_period_items.nth(i).click()
                                    page.wait_for_timeout(10000)
                                    log(f"[OK] Selected payroll period (index={item_index}): {item_text.strip()}")
                                    return True
                                match_count += 1
                    else:
                        # No suffix: pick the item_index-th item with no parenthetical suffix
                        match_count = 0
                        for i in range(count):
                            item_text = pay_period_items.nth(i).text_content() or ""
                            log(f"[DEBUG] Checking item {i}: {item_text}")
                            if "(" not in item_text and ")" not in item_text:
                                if match_count == item_index:
                                    expect(pay_period_items.nth(i)).to_be_visible(timeout=120000)
                                    pay_period_items.nth(i).click()
                                    page.wait_for_timeout(10000)
                                    log(f"[OK] Selected payroll period (index={item_index}): {item_text.strip()}")
                                    return True
                                match_count += 1
                else:
                    log(f"[DEBUG] No items found on attempt {attempt}, searching for View More button...")

            except Exception as exc:
                log(f"[DEBUG] Exception on attempt {attempt}: {exc}")

            # Try View More button to load more options
            if attempt < max_view_more_attempts:
                view_more_button = page.locator('//div[normalize-space(text())="View More"]')
                if view_more_button.count() > 0:
                    log(f"[DEBUG] Clicking View More button (attempt {attempt + 1})")
                    view_more_button.click()
                    page.wait_for_timeout(4000)  # Increased wait time for DOM to update
                    view_more_clicked_count += 1
                else:
                    log(f"[DEBUG] No more View More buttons found after {view_more_clicked_count} clicks")
                    break

        log(f"[WARN] Payroll register pay period not found for '{search_text}' at index {item_index} after {view_more_clicked_count} View More clicks")
        return False
    except Exception as exc:
        log(f"[WARN] Payroll register pay period not found for '{search_text}': {exc}")
        return False

def select_pay_period_for_summary_of_wages(page, start_date: str, end_date: str, item_index: int = 0) -> bool:
    """Select a pay period on the Summary of Wages (and Child Support) UI.

    Args:
        item_index: 0-based index of which matching item to pick when multiple exist.
                    Defaults to 0 (first match). Pass 1 to pick the second match, etc.
    """
    page.click("body", position={"x": 100, "y": 100})
    page.click("body", position={"x": 100, "y": 100})
    pay_period_text = format_pay_period_for_summary_of_wages(start_date, end_date)
    log(f"[DEBUG] Looking for pay period with text: '{pay_period_text}' (item_index={item_index})")
    suffix = extract_pay_period_suffix(end_date)
    # Build the full search text with suffix if present (e.g., "04/27/2026 – 05/03/2026 (Off-Cycle)")
    search_text = f"{pay_period_text} {suffix}".strip()

    def click_item_via_checkbox(target_li):
        """Click a pay period item using its checkbox, falling back to direct click."""
        # Structure 2: checkbox is directly inside the <li>
        checkbox = target_li.locator('input[type="checkbox"]')
        if checkbox.count() == 0:
            # Structure 1: grouped periods where the <li> sits inside a child MuiBox-root div,
            # and the checkbox lives in a sibling MuiBox-root div — 2 ancestor levels up
            checkbox = target_li.locator(
                'xpath=ancestor::div[contains(@class,"MuiBox-root")][2]//input[@type="checkbox"]'
            )
        if checkbox.count() > 0:
            expect(checkbox.first).to_be_attached(timeout=120000)
            checkbox.first.check(force=True)
        else:
            target_li.click()

    try:
        select_button = page.locator('//p[contains(normalize-space(.), "Select")]')
        expect(select_button).to_be_visible(timeout=120000)
        select_button.click()
        page.wait_for_timeout(3000)

        max_view_more_attempts = 15
        view_more_clicked_count = 0

        for attempt in range(max_view_more_attempts + 1):
            # If suffix is present, keep clicking View More until we have enough items
            if suffix:
                pay_period_item = page.locator(f'//li[contains(normalize-space(.), "{search_text}")]')
                count = pay_period_item.count()
                log(f"[DEBUG] Suffix search attempt {attempt}: found {count} items for '{search_text}'")
                if count > item_index:
                    target = pay_period_item.nth(item_index)
                    expect(target).to_be_visible(timeout=120000)
                    click_item_via_checkbox(target)
                    page.click("body", position={"x": 100, "y": 100})
                    page.wait_for_timeout(10000)
                    log(f"[OK] Selected payroll period with suffix (index={item_index}): {search_text}")
                    return True
            else:
                # No suffix: count items without any parenthetical suffix
                pay_period_items = page.locator(f'//li[contains(normalize-space(.), "{pay_period_text}")]')
                count = pay_period_items.count()
                log(f"[DEBUG] No-suffix search attempt {attempt}: found {count} items for '{pay_period_text}'")
                match_count = 0
                for i in range(count):
                    item_text = pay_period_items.nth(i).text_content() or ""
                    log(f"[DEBUG] Checking item {i}: {item_text}")
                    if "(" not in item_text and ")" not in item_text:
                        if match_count == item_index:
                            expect(pay_period_items.nth(i)).to_be_visible(timeout=120000)
                            click_item_via_checkbox(pay_period_items.nth(i))
                            page.click("body", position={"x": 100, "y": 100})
                            page.wait_for_timeout(10000)
                            log(f"[OK] Selected payroll period (index={item_index}): {item_text.strip()}")
                            return True
                        match_count += 1

            if attempt < max_view_more_attempts:
                view_more_button = page.locator('//div[normalize-space(text())="View More"]')
                if view_more_button.count() > 0:
                    log(f"[DEBUG] Clicking View More button (attempt {attempt + 1})")
                    view_more_button.click()
                    page.wait_for_timeout(4000)
                    view_more_clicked_count += 1
                else:
                    log(f"[DEBUG] No more View More buttons found after {view_more_clicked_count} clicks")
                    break

        log(f"[WARN] Payroll period not found for '{search_text}' at index {item_index} after {view_more_clicked_count} View More clicks")
        return False
    except Exception as exc:
        log(f"[WARN] Payroll period not found for '{search_text}': {exc}")
        return False


def download_payroll_register_report(service, page, company_name: str, folder_id: str, start_date: str, end_date: str, pay_period_index: int = 0) -> None:
    navigate_to_report(page, "/reports/payroll/payroll_register")
    page.wait_for_timeout(5000)

    if not select_pay_period_for_payroll_register(page, start_date, end_date, pay_period_index):
        log("[INFO] Skipping payroll register report section because pay period was not found.")
        if _failure_logger:
            _failure_logger.log_skip("Payroll Register")
        return

    download_button = ensure_download_button(page)
    expect(download_button).to_be_visible(timeout=120000)
    try:
        page.wait_for_function("button => !button.disabled", arg=download_button.element_handle(), timeout=120000)
        download_button.click()

        pdf_option = page.locator('//li[contains(normalize-space(.), "PDF")]')
        expect(pdf_option).to_be_visible(timeout=120000)
        with page.expect_download(timeout=60000) as pdf_dl:
            pdf_option.click()
        pdf_filename = safe_text(f"Payroll_Register_{end_date}.pdf")
        save_and_upload_download(service, pdf_dl.value, DOWNLOAD_DIR / pdf_filename, folder_id, pdf_filename, "application/pdf")
        log(f"[OK] Payroll register PDF saved: {pdf_filename}")
    except Exception as exc:
        log(f"[WARN] Payroll register PDF download failed: {exc}")
        if _failure_logger:
            _failure_logger.log_failure(safe_text(f"Payroll_Register_{end_date}.pdf"), reason=str(exc)[:300])

    try:
        download_button.click()
        excel_option = page.locator('//li[contains(normalize-space(.), "Excel")]')
        expect(excel_option).to_be_visible(timeout=120000)
        with page.expect_download(timeout=60000) as excel_dl:
            excel_option.click()
        excel_filename = safe_text(f"Payroll_Register_{end_date}.xlsx")
        save_and_upload_download(service, excel_dl.value, DOWNLOAD_DIR / excel_filename, folder_id, excel_filename, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        log(f"[OK] Payroll register Excel saved: {excel_filename}")
    except Exception as exc:
        log(f"[WARN] Payroll register Excel download failed: {exc}")
        if _failure_logger:
            _failure_logger.log_failure(safe_text(f"Payroll_Register_{end_date}.xlsx"), reason=str(exc)[:300])


def download_prevailing_wage_reports(service, page, company_name: str, projects: list[str], folder_id: str, start_date: str, end_date: str) -> None:
    navigate_to_report(page, "/reports/payroll/prevailing_wage")
    page.wait_for_timeout(5000)

    if not select_prevailing_wage_week(page, start_date, end_date):
        log("[INFO] Skipping prevailing wage report section because the correct week was not found.")
        if _failure_logger:
            _failure_logger.log_skip("Prevailing Wage")
        return

    for project in projects:
        if not select_prevailing_wage_project(page, project):
            if _failure_logger:
                _failure_logger.log_skip("Prevailing Wage", project=project)
            continue
        page.wait_for_timeout(10000)

        try:
            federal_tab = page.locator('button[data-testid="reports-federal-tab"]')
            expect(federal_tab).to_be_visible(timeout=120000)
            federal_tab.click()
            page.wait_for_timeout(15000)
            
            try:
                old_format = page.locator('//button[contains(normalize-space(.), "Revert to Old Format")]')
                expect(old_format).to_be_visible(timeout=120000)
                if old_format.count() > 0:
                    old_format.click()
                    page.wait_for_timeout(60000)
            except Exception as exc:
                log(f"[INFO] Revert to Old Format button not found, already in old format")
            
            federal_name = safe_text(f"Prevailing_Wage_Report_Federal_{project}_{end_date}.pdf")
            if download_pdf_report(service, page, folder_id, federal_name):
                log(f"[OK] Federal report saved: {federal_name}")
            
            # ── CRITICAL: Wait for button to be fully ready before next download ──
            page.wait_for_timeout(3000)
            download_btn = ensure_download_button(page)
            try:
                page.wait_for_function("button => !button.disabled", arg=download_btn.element_handle(), timeout=30000)
            except Exception:
                log(f"[WARN] Download button did not re-enable after federal download, waiting extra time...")
                page.wait_for_timeout(5000)
                
        except Exception as exc:
            log(f"[WARN] Federal tab missing or failed for project '{project}': {exc}")
        
        try:
            wh3_47_format = page.locator('//button[contains(normalize-space(.), "Switch to New Format")]')
            if wh3_47_format.count() > 0:
                wh3_47_format.click()
                page.wait_for_timeout(120000)
                # Wait for PDF viewer to appear and load
                # pdf_viewer = page.locator('pdf-viewer#viewer')
                # expect(pdf_viewer).to_be_visible(timeout=120000)
                # page.wait_for_timeout(3000)
                
                # ── CRITICAL: Validate button is ready in NEW format context ──
                page.wait_for_timeout(3000)
                download_btn = ensure_download_button(page)
                try:
                    page.wait_for_function("button => !button.disabled", arg=download_btn.element_handle(), timeout=30000)
                except Exception:
                    log(f"[WARN] Download button not ready after format switch, waiting extra...")
                    page.wait_for_timeout(5000)
                
                wh3_47_name = safe_text(f"Prevailing_Wage_Report_WH3_47_{project}_{end_date}.pdf")
                if download_pdf_report(service, page, folder_id, wh3_47_name):
                    log(f"[OK] WH3-47 report saved: {wh3_47_name}")
                
                # ── CRITICAL: Wait after download before State tab click ──
                page.wait_for_timeout(3000)
                download_btn = ensure_download_button(page)
                try:
                    page.wait_for_function("button => !button.disabled", arg=download_btn.element_handle(), timeout=30000)
                except Exception:
                    log(f"[WARN] Download button did not re-enable after WH3-47 download")
                    page.wait_for_timeout(5000)
        except Exception as exc:
            log(f"[WARN] WH3-47 format switch missing or failed for project '{project}': {exc}")

        try:
            state_tab = page.locator('button[data-testid="reports-state-tab"]')
            if state_tab.count() > 0:
                state_tab.click()
                page.wait_for_timeout(2000)
                
                # ── CRITICAL: Validate button is ready in State context ──
                page.wait_for_timeout(3000)
                download_btn = ensure_download_button(page)
                try:
                    page.wait_for_function("button => !button.disabled", arg=download_btn.element_handle(), timeout=30000)
                except Exception:
                    log(f"[WARN] Download button not ready after state tab click, waiting extra...")
                    page.wait_for_timeout(5000)
                
                state_name = safe_text(f"Prevailing_Wage_Report_State_{project}_{end_date}.pdf")
                if download_pdf_report(service, page, folder_id, state_name):
                    log(f"[OK] State report saved: {state_name}")
                
                # ── CRITICAL: Wait after download before LCP download ──
                page.wait_for_timeout(3000)
                download_btn = ensure_download_button(page)
                try:
                    page.wait_for_function("button => !button.disabled", arg=download_btn.element_handle(), timeout=30000)
                except Exception:
                    log(f"[WARN] Download button did not re-enable after state download")
                    page.wait_for_timeout(5000)
        except Exception as exc:
            log(f"[WARN] State tab missing or failed for project '{project}': {exc}")

        lcp_name = safe_text(f"Prevailing_Wage_Report_LCP_{project}_{end_date}.csv")
        if download_csv_report(service, page, folder_id, lcp_name):
            log(f"[OK] LCP report saved: {lcp_name}")


def download_prevailing_wage_summary_reports(service, page, company_name: str, projects: list[str], folder_id: str, start_date: str, end_date: str) -> None:
    navigate_to_report(page, "/reports/payroll/prevailing_wage")
    page.wait_for_timeout(5000)

    if not select_prevailing_wage_week(page, start_date, end_date):
        log("[INFO] Skipping prevailing wage report section because the correct week was not found.")
        if _failure_logger:
            _failure_logger.log_skip("Prevailing Wage Summary")
        return

    for project in projects:
        if not select_prevailing_wage_project(page, project):
            if _failure_logger:
                _failure_logger.log_skip("Prevailing Wage Summary", project=project)
            continue
        page.wait_for_timeout(120000)

        try:
            download_button = ensure_download_button(page)
            expect(download_button).to_be_enabled(timeout=120000)
            download_button.click()
            page.wait_for_timeout(1000)

            summary_option = page.locator('//li[contains(normalize-space(.), "Summary")]')
            expect(summary_option).to_be_visible(timeout=120000)
            with page.expect_download(timeout=120000) as dl:
                summary_option.click()
            
            summary_filename = safe_text(f"Prevailing_Wage_Summary_{project}_{end_date}.pdf")
            save_and_upload_download(service, dl.value, DOWNLOAD_DIR / summary_filename, folder_id, summary_filename, "application/pdf")
            log(f"[OK] Prevailing wage summary report saved: {summary_filename}")
            page.click("body", position={"x": 100, "y": 100})
            page.wait_for_timeout(2000)
        except Exception as exc:
            log(f"[WARN] Prevailing wage summary download failed for project '{project}': {exc}")
            if _failure_logger:
                _failure_logger.log_failure("Prevailing Wage Summary", project=project, reason=str(exc)[:300])



def download_summary_of_wages_report(service, page, company_name: str, folder_id: str, start_date: str, end_date: str) -> None:
    navigate_to_report(page, "/reports/payroll/census_report")
    page.wait_for_timeout(5000)

    # Strip any suffix (e.g. "(Off-Cycle)") from end_date before passing to the calendar picker
    end_date_clean = end_date.split()[0] if end_date else end_date
    if not select_date_range_from_calendar(page, end_date_clean):
        log("[WARN] Failed to select date range from calendar for Summary of Wages, continuing...")

    page.wait_for_timeout(10000)
    page.click("body", position={"x": 100, "y": 100})
    if not select_pay_period_for_summary_of_wages(page, start_date, end_date):
        log("[INFO] Skipping summary of wages report section because pay period was not found.")
        if _failure_logger:
            _failure_logger.log_skip("Summary of Wages")
        return

    # Download CSV report
    try:
        download_button = ensure_download_button(page)
        expect(download_button).to_be_visible(timeout=120000)
        page.wait_for_function("button => !button.disabled", arg=download_button.element_handle(), timeout=120000)
        download_button.click()
        page.wait_for_timeout(1000)

        csv_option = page.locator('//li[contains(normalize-space(.), "CSV")]')
        expect(csv_option).to_be_visible(timeout=120000)
        with page.expect_download(timeout=60000) as csv_dl:
            csv_option.click()

        csv_filename = safe_text(f"Summary_of_wages_{end_date}.csv")
        save_and_upload_download(service, csv_dl.value, DOWNLOAD_DIR / csv_filename, folder_id, csv_filename, "text/csv")
        log(f"[OK] Summary of wages CSV saved: {csv_filename}")
        page.click("body", position={"x": 100, "y": 100})
        page.wait_for_timeout(2000)
    except Exception as exc:
        log(f"[WARN] Summary of wages CSV download failed: {exc}")
        if _failure_logger:
            _failure_logger.log_failure(safe_text(f"Summary_of_wages_{end_date}.csv"), reason=str(exc)[:300])

    # Download PDF report
    try:
        download_button = ensure_download_button(page)
        expect(download_button).to_be_visible(timeout=120000)
        page.wait_for_function("button => !button.disabled", arg=download_button.element_handle(), timeout=120000)
        download_button.click()
        page.wait_for_timeout(1000)

        pdf_option = page.locator('//li[contains(normalize-space(.), "PDF")]')
        expect(pdf_option).to_be_visible(timeout=120000)
        with page.expect_download(timeout=60000) as pdf_dl:
            pdf_option.click()

        pdf_filename = safe_text(f"Summary_of_wages_{end_date}.pdf")
        save_and_upload_download(service, pdf_dl.value, DOWNLOAD_DIR / pdf_filename, folder_id, pdf_filename, "application/pdf")
        log(f"[OK] Summary of wages PDF saved: {pdf_filename}")
        page.click("body", position={"x": 100, "y": 100})
        page.wait_for_timeout(2000)
    except Exception as exc:
        log(f"[WARN] Summary of wages PDF download failed: {exc}")
        if _failure_logger:
            _failure_logger.log_failure(safe_text(f"Summary_of_wages_{end_date}.pdf"), reason=str(exc)[:300])


def download_child_support_report(service, page, company_name: str, folder_id: str, start_date: str, end_date: str, pay_period_index: int = 0) -> None:
    navigate_to_report(page, "/reports/payroll/child_support_payments")
    page.wait_for_timeout(5000)

    # Strip any suffix (e.g. "(Off-Cycle)") from end_date before passing to the calendar picker
    end_date_clean = end_date.split()[0] if end_date else end_date
    if not select_date_range_from_calendar(page, end_date_clean):
        log("[WARN] Failed to select date range from calendar for Child Support, continuing...")

    page.wait_for_timeout(5000)
    page.click("body", position={"x": 100, "y": 100})
    if not select_pay_period_for_summary_of_wages(page, start_date, end_date, item_index=pay_period_index):
        log("[INFO] Skipping child support report section because pay period was not found.")
        if _failure_logger:
            _failure_logger.log_skip("Child Support Remittance")
        return

    try:
        download_button = ensure_download_button(page)
        expect(download_button).to_be_visible(timeout=120000)
        page.wait_for_function("button => !button.disabled", arg=download_button.element_handle(), timeout=120000)
        with page.expect_download(timeout=60000) as csv_dl:
            download_button.click()

        csv_filename = safe_text(f"Child_Support_Remittance_{end_date}.csv")
        save_and_upload_download(service, csv_dl.value, DOWNLOAD_DIR / csv_filename, folder_id, csv_filename, "text/csv")
        log(f"[OK] Child Support Remittance CSV saved: {csv_filename}")
        page.click("body", position={"x": 100, "y": 100})
        page.wait_for_timeout(2000)
    except Exception as exc:
        log(f"[WARN] Child Support Remittance CSV download failed: {exc}")
        if _failure_logger:
            _failure_logger.log_failure(safe_text(f"Child_Support_Remittance_{end_date}.csv"), reason=str(exc)[:300])


def download_garnishment_report(service, page, company_name: str, folder_id: str, start_date: str, end_date: str, pay_period_index: int = 0) -> None:
    navigate_to_report(page, "/reports/payroll/garnishment_report")
    page.wait_for_timeout(5000)

    # Strip any suffix (e.g. "(Off-Cycle)") from end_date before passing to the calendar picker
    end_date_clean = end_date.split()[0] if end_date else end_date
    if not select_date_range_from_calendar(page, end_date_clean):
        log("[WARN] Failed to select date range from calendar for Garnishment, continuing...")

    page.wait_for_timeout(5000)
    page.click("body", position={"x": 100, "y": 100})
    if not select_pay_period_for_summary_of_wages(page, start_date, end_date, item_index=pay_period_index):
        log("[INFO] Skipping garnishment report section because pay period was not found.")
        if _failure_logger:
            _failure_logger.log_skip("Garnishment Report")
        return

    try:
        download_button = ensure_download_button(page)
        expect(download_button).to_be_visible(timeout=120000)
        page.wait_for_function("button => !button.disabled", arg=download_button.element_handle(), timeout=120000)
        with page.expect_download(timeout=60000) as dl:
            download_button.click()

        filename = safe_text(f"Garnishment_Report_{end_date}.csv")
        save_and_upload_download(service, dl.value, DOWNLOAD_DIR / filename, folder_id, filename, "text/csv")
        log(f"[OK] Garnishment Report saved: {filename}")
        page.click("body", position={"x": 100, "y": 100})
        page.wait_for_timeout(2000)
    except Exception as exc:
        log(f"[WARN] Garnishment Report download failed: {exc}")
        if _failure_logger:
            _failure_logger.log_failure(safe_text(f"Garnishment_Report_{end_date}.csv"), reason=str(exc)[:300])


def download_union_reports(service, page, company_name: str, report_names: list[str], folder_id: str, start_date: str, end_date: str, custom_report: bool = False) -> None:
    navigate_to_report(page, "/reports/payroll/union_report")
    page.wait_for_timeout(5000)

    if not select_date_range_from_calendar(page, end_date):
        log("[WARN] Failed to select date range from calendar, attempting alternative method...")

    if not select_pay_period_for_401k(page, start_date, end_date):
        log("[INFO] Skipping union report section because pay period was not found.")
        if _failure_logger:
            _failure_logger.log_skip("Union Report")
        return

    union_dropdown = page.locator('div[data-testid="select-union-dropdown"]')
    expect(union_dropdown).to_be_visible(timeout=120000)

    if not report_names:
        union_dropdown.click()
        page.wait_for_timeout(1000)
        union_items = page.locator('li[data-testid="project-option-menu-item"]')
        names = [union_items.nth(i).text_content().strip() for i in range(union_items.count())]
        report_names = [name for name in names if name]
        page.click("body", position={"x": 100, "y": 100})

    for report_name in report_names:
        safe_report = safe_text(report_name)
        union_dropdown.click()
        page.wait_for_timeout(1000)

        option = page.locator(
            f'//li[@data-testid="project-option-menu-item" and normalize-space()="{report_name}"]'
        )
        expect(option).to_be_visible(timeout=120000)
        option.click()
        page.wait_for_timeout(5000)

        download_button = ensure_download_button(page)
        expect(download_button).to_be_visible(timeout=120000)
        try:
            page.wait_for_function("button => !button.disabled", arg=download_button.element_handle(), timeout=120000)
            download_button.click()
            pdf_option = page.locator('//li[contains(normalize-space(.), "PDF")]')
            expect(pdf_option).to_be_visible(timeout=120000)
            with page.expect_download(timeout=60000) as pdf_dl:
                pdf_option.click()
            pdf_filename = safe_text(f"Union_Report_{report_name}_{end_date}.pdf")
            save_and_upload_download(service, pdf_dl.value, DOWNLOAD_DIR / pdf_filename, folder_id, pdf_filename, "application/pdf")
            log(f"[OK] Union report PDF saved: {pdf_filename}")
        except Exception as exc:
            log(f"[WARN] Union PDF download failed for '{report_name}': {exc}")
            if _failure_logger:
                _failure_logger.log_failure(safe_text(f"Union_Report_{report_name}_{end_date}.pdf"), project=report_name, reason=str(exc)[:300])

        try:
            excel_option = page.locator('//li[contains(normalize-space(.), "Excel")]')
            expect(excel_option).to_be_visible(timeout=120000)
            with page.expect_download(timeout=60000) as excel_dl:
                excel_option.click()
            excel_filename = safe_text(f"Union_Report_{report_name}_{end_date}.xlsx")
            save_and_upload_download(service, excel_dl.value, DOWNLOAD_DIR / excel_filename, folder_id, excel_filename, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            log(f"[OK] Union report Excel saved: {excel_filename}")
            page.click("body", position={"x": 100, "y": 100})
        except Exception as exc:
            log(f"[WARN] Union Excel download failed for '{report_name}': {exc}")
            if _failure_logger:
                _failure_logger.log_failure(safe_text(f"Union_Report_{report_name}_{end_date}.xlsx"), project=report_name, reason=str(exc)[:300])

        # Download custom union reports if flag is set
        if custom_report:
            # Custom reports with variations for different union codes
            custom_reports = [
                (["134 Dues EIT"], "134 Dues EIT"),
                (["134a EIT", "134c EIT", "134m EIT"], "EIT"),  # Try variations: 134a, 134c, 134m
                (["134a NEBF", "134c NEBF", "134m NEBF"], "NEBF")  # Try variations: 134a, 134c, 134m
            ]
            
            for search_options, file_suffix in custom_reports:
                report_downloaded = False
                for custom_name in search_options:
                    try:
                        download_button = ensure_download_button(page)
                        expect(download_button).to_be_visible(timeout=120000)
                        download_button.click()
                        page.wait_for_timeout(1000)
                        
                        custom_option = page.locator(f'//li[contains(normalize-space(.), "{custom_name}")]')
                        if custom_option.count() == 0:
                            log(f"[DEBUG] Option '{custom_name}' not found, trying next variation...")
                            page.click("body", position={"x": 100, "y": 100})
                            page.wait_for_timeout(500)
                            continue
                        
                        expect(custom_option).to_be_visible(timeout=120000)
                        with page.expect_download(timeout=60000) as custom_dl:
                            custom_option.click()
                        
                        custom_filename = safe_text(f"Union_Report_{report_name}_{custom_name}_{end_date}.xlsx")
                        save_and_upload_download(service, custom_dl.value, DOWNLOAD_DIR / custom_filename, folder_id, custom_filename, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                        log(f"[OK] Union custom report '{custom_name}' saved: {custom_filename}")
                        # page.click("body", position={"x": 100, "y": 100})
                        page.wait_for_timeout(1000)
                        report_downloaded = True
                        break  # Successfully downloaded, move to next custom report type
                    except Exception as exc:
                        log(f"[DEBUG] Union custom report '{custom_name}' not available: {exc}")
                        # page.click("body", position={"x": 100, "y": 100})
                        page.wait_for_timeout(500)
                        continue
                
                if not report_downloaded:
                    log(f"[WARN] None of the variations for '{file_suffix}' were available for '{report_name}'")


def select_date_range_from_calendar(page, end_date: str) -> bool:
    """Select a date range from the calendar picker using month and year.
    
    Args:
        page: Playwright page object
        end_date: Date string in format YYYY-MM-DD
    
    Returns:
        True if date range was successfully selected, False otherwise
    """
    try:
        page.click("body", position={"x": 100, "y": 100})
        page.click("body", position={"x": 100, "y": 100})
        page.wait_for_timeout(2000)
        # Parse the end_date to get month and year
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        target_year = end_dt.year
        target_month = end_dt.strftime("%b")  # Get month abbreviation (Jan, Feb, etc.)
        
        # Click on the date range selector to open the calendar
        date_range_selector = page.locator('div[data-testid="select-date-range"]')
        expect(date_range_selector).to_be_visible(timeout=120000)
        date_range_selector.click()
        page.wait_for_timeout(2000)
        
        # Get the year display using a more specific selector that finds the year between the chevrons
        # The year is in the middle of two IconButton elements within the MuiBox
        year_display = page.locator('button:has(svg[data-testid="ChevronLeftIcon"])').locator('..').locator('p.MuiTypography-body1')
        
        # Navigate to correct year if needed
        current_year_text = year_display.text_content() or ""
        current_year = int(current_year_text.strip()) if current_year_text.strip().isdigit() else datetime.now().year
        
        while current_year < target_year:
            right_chevron = page.locator('button:has(svg[data-testid="ChevronRightIcon"])')
            expect(right_chevron).to_be_visible(timeout=120000)
            right_chevron.click()
            page.wait_for_timeout(500)
            current_year_text = year_display.text_content() or ""
            current_year = int(current_year_text.strip()) if current_year_text.strip().isdigit() else current_year
        
        while current_year > target_year:
            left_chevron = page.locator('button:has(svg[data-testid="ChevronLeftIcon"])')
            expect(left_chevron).to_be_visible(timeout=120000)
            left_chevron.click()
            page.wait_for_timeout(500)
            current_year_text = year_display.text_content() or ""
            current_year = int(current_year_text.strip()) if current_year_text.strip().isdigit() else current_year
        
        # Click on the correct month button - find month buttons within the calendar popup
        # These are buttons with text content matching the month abbreviation
        month_buttons = page.locator('div.MuiBox-root button').filter(has_text=target_month)
        expect(month_buttons).to_be_visible(timeout=120000)
        month_buttons.click()
        page.wait_for_timeout(2000)
        
        log(f"[OK] Date range selected: {target_month} {target_year}")
        return True
        
    except Exception as exc:
        log(f"[WARN] Failed to select date range from calendar for '{end_date}': {exc}")
        return False


def download_worker_compensation_report(service, page, company_name: str, folder_id: str, start_date: str, end_date: str) -> None:
    navigate_to_report(page, "/reports/payroll/worker_compensation")
    
    # First, select the date range from the calendar picker
    if not select_date_range_from_calendar(page, end_date):
        log("[WARN] Failed to select date range from calendar, attempting alternative method...")
    
    page.wait_for_timeout(60000)
    if not select_pay_period_without_paid_section(page, start_date, end_date):
        log("[INFO] Skipping worker compensation report section because pay period was not found.")
        if _failure_logger:
            _failure_logger.log_skip("Worker Compensation")
        return

    # page.wait_for_timeout(30000)
    download_button = ensure_download_button(page)
    try:
        expect(download_button).to_be_visible(timeout=120000)
        page.wait_for_function("button => !button.disabled", arg=download_button.element_handle(), timeout=120000)
        with page.expect_download(timeout=60000) as csv_dl:
            download_button.click()
        filename = safe_text(f"Worker_Compensation_{end_date}.csv")
        save_and_upload_download(service, csv_dl.value, DOWNLOAD_DIR / filename, folder_id, filename, "text/csv")
        log(f"[OK] Worker Compensation report saved: {filename}")
    except Exception as exc:
        log(f"[WARN] Worker Compensation download failed: {exc}")
        if _failure_logger:
            _failure_logger.log_failure(safe_text(f"Worker_Compensation_{end_date}.csv"), reason=str(exc)[:300])


def download_job_costing_report(service, page, company_name: str, folder_id: str, start_date: str, end_date: str) -> None:
    navigate_to_report(page, "/reports/payroll/job_costing")
    page.wait_for_timeout(10000)

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    formatted_range = f"{start_dt.strftime('%m/%d/%Y')} – {end_dt.strftime('%m/%d/%Y')}"

    try:
        date_input = page.locator('input[placeholder="MM/DD/YYYY – MM/DD/YYYY"]')
        expect(date_input).to_be_visible(timeout=120000)
        date_input.dblclick()
        page.keyboard.press("Control+A")
        date_input.fill(formatted_range)
        page.wait_for_timeout(2000)
        expect(date_input).to_have_value(formatted_range, timeout=120000)
        page.wait_for_timeout(10000)

        download_button = ensure_download_button(page)
        expect(download_button).to_be_visible(timeout=120000)
        page.wait_for_function("button => !button.disabled", arg=download_button.element_handle(), timeout=120000)

        with page.expect_download(timeout=60000) as csv_dl:
            download_button.click()
            excel_option = page.locator('//li[contains(normalize-space(.), "Excel")]')
            excel_option.click()

        filename = safe_text(f"Job_Costing_allprojects_{end_date}.csv")
        save_and_upload_download(service, csv_dl.value, DOWNLOAD_DIR / filename, folder_id, filename, "text/csv")
        log(f"[OK] Job Costing report saved: {filename}")
    except Exception as exc:
        log(f"[WARN] Job Costing download failed: {exc}")
        if _failure_logger:
            _failure_logger.log_failure(safe_text(f"Job_Costing_allprojects_{end_date}.csv"), reason=str(exc)[:300])

def download_apprentice_ratio_reports(service, page, company_name: str, folder_id: str, start_date: str, end_date: str) -> None:
    navigate_to_report(page, "/reports/payroll/apprentice_ratio")
    page.wait_for_timeout(10000)

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    formatted_range = f"{start_dt.strftime('%m/%d/%Y')} – {end_dt.strftime('%m/%d/%Y')}"

    try:
        date_input = page.locator('input[placeholder="MM/DD/YYYY – MM/DD/YYYY"]')
        expect(date_input).to_be_visible(timeout=120000)
        date_input.dblclick()
        page.keyboard.press("Control+A")
        date_input.fill(formatted_range)
        page.wait_for_timeout(2000)
        expect(date_input).to_have_value(formatted_range, timeout=120000)
        page.wait_for_timeout(10000)

        page.click("body", position={"x": 100, "y": 100})

        download_button = page.locator('//button[contains(normalize-space(.), "Download")]')
        expect(download_button).to_be_visible(timeout=120000)
        page.wait_for_function("button => !button.disabled", arg=download_button.element_handle(), timeout=120000)

        with page.expect_download(timeout=60000) as csv_dl:
            download_button.click()
            excel_option = page.locator('//li[contains(normalize-space(.), "CSV")]')
            excel_option.click()

        filename = safe_text(f"Apprentice_Ratio_{end_date}.csv")
        save_and_upload_download(service, csv_dl.value, DOWNLOAD_DIR / filename, folder_id, filename, "text/csv")
        log(f"[OK] Apprentice Ratio report saved: {filename}")

        with page.expect_download(timeout=60000) as pdf_dl:
            download_button.click()
            pdf_option = page.locator('//li[contains(normalize-space(.), "PDF")]')
            pdf_option.click()
        
        pdf_filename = safe_text(f"Apprentice_Ratio_{end_date}.pdf")
        save_and_upload_download(service, pdf_dl.value, DOWNLOAD_DIR / pdf_filename, folder_id, pdf_filename, "application/pdf")
        log(f"[OK] Apprentice Ratio PDF report saved: {pdf_filename}")
    except Exception as exc:
        log(f"[WARN] Apprentice Ratio download failed: {exc}")
        if _failure_logger:
            _failure_logger.log_failure(safe_text(f"Apprentice_Ratio_{end_date}"), reason=str(exc)[:300])


def download_payroll_journal_report(service, page, company_name: str, folder_id: str, start_date: str, end_date: str) -> None:
    navigate_to_report(page, "/reports/payroll/payroll_journal_report")
    page.click("body", position={"x": 100, "y": 100})

    if not select_date_range_from_calendar(page, end_date):
        log("[WARN] Failed to select date range from calendar for Payroll Journal, continuing...")
    
    page.wait_for_timeout(30000)
    if not select_pay_period_for_401k(page, start_date, end_date):
        log("[INFO] Skipping Payroll Journal report because pay period was not found.")
        if _failure_logger:
            _failure_logger.log_skip("Payroll Journal")
        return

    # page.wait_for_timeout(15000)

    download_button = ensure_download_button(page)
    try:
        expect(download_button).to_be_visible(timeout=120000)
        page.wait_for_function("button => !button.disabled", arg=download_button.element_handle(), timeout=120000)
        filename = safe_text(f"Payroll_Journal_{end_date}.csv")
        with page.expect_download(timeout=60000) as csv_dl:
            download_button.click()
        save_and_upload_download(service, csv_dl.value, DOWNLOAD_DIR / filename, folder_id, filename, "text/csv")
        log(f"[OK] Payroll Journal report saved: {filename}")
    except Exception as exc:
        log(f"[WARN] Payroll Journal download failed: {exc}")
        if _failure_logger:
            _failure_logger.log_failure(safe_text(f"Payroll_Journal_{end_date}.csv"), reason=str(exc)[:300])


def select_pay_period_for_401k(page, start_date: str, end_date: str) -> bool:
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    formatted_period = f"{start_dt.strftime('%m/%d/%Y')} – {end_dt.strftime('%m/%d/%Y')}"

    page.click("body", position={"x": 100, "y": 100})
    page.wait_for_timeout(2000)
    try:
        pay_period_dropdown = page.locator('svg[data-testid="ExpandMoreIcon"]')
        expect(pay_period_dropdown).to_be_visible(timeout=120000)
        pay_period_dropdown.click()
        page.wait_for_timeout(2000)

        # Scope to "Paid" section, match the period, exclude Off-Cycle items
        pay_period_locator = page.locator(
            f'//ul[@data-testid="pay-period-dropdown"]'
            f' //div[.//p[contains(normalize-space(), "Paid")]]'
            f' //li[contains(normalize-space(.), "{formatted_period}")'
            f' and not(contains(normalize-space(.), "(Off-Cycle)"))]'
        )
        expect(pay_period_locator.first).to_be_visible(timeout=120000)
        target_li = pay_period_locator.first

        # Structure 2: checkbox is directly inside the <li>
        checkbox = target_li.locator('input[type="checkbox"]')
        if checkbox.count() == 0:
            # Structure 1: checkbox is in a sibling MuiBox-root div — 2 ancestor levels up
            checkbox = target_li.locator(
                'xpath=ancestor::div[contains(@class,"MuiBox-root")][2]//input[@type="checkbox"]'
            )

        if checkbox.count() > 0:
            expect(checkbox.first).to_be_attached(timeout=120000)
            checkbox.first.check(force=True)
        else:
            target_li.click()

        page.click("body", position={"x": 100, "y": 100})
        page.wait_for_timeout(5000)
        return True
    except Exception as exc:
        log(f"[WARN] 401K pay period not found for '{formatted_period}': {exc}")
        return False


def download_401k_report(service, page, company_name: str, folder_id: str, start_date: str, end_date: str) -> None:
    navigate_to_report(page, "/reports/payroll/401k_report")
    page.click("body", position={"x": 100, "y": 100})

    if not select_date_range_from_calendar(page, end_date):
        log("[WARN] Failed to select date range from calendar for 401K report, continuing...")

    if not select_pay_period_for_401k(page, start_date, end_date):
        log("[INFO] Skipping 401K report section because pay period was not found.")
        if _failure_logger:
            _failure_logger.log_skip("401K Report")
        return

    download_button = ensure_download_button(page)
    try:
        expect(download_button).to_be_visible(timeout=120000)
        page.wait_for_function("button => !button.disabled", arg=download_button.element_handle(), timeout=120000)
        filename = safe_text(f"401K_Report_{end_date}.csv")
        with page.expect_download(timeout=60000) as csv_dl:
            download_button.click()
        save_and_upload_download(service, csv_dl.value, DOWNLOAD_DIR / filename, folder_id, filename, "text/csv")
        log(f"[OK] 401K report saved: {filename}")
    except Exception as exc:
        log(f"[WARN] 401K report download failed: {exc}")
        if _failure_logger:
            _failure_logger.log_failure(safe_text(f"401K_Report_{end_date}.csv"), reason=str(exc)[:300])


def get_report_item(reports: list[dict], key: str) -> dict:
    for report_item in reports:
        if key in report_item:
            return report_item
    return {}


def run_company_job(service, page, job: dict, root_folder_id: str) -> None:
    job_start_time = datetime.now()
    log(f"[START] Processing company job at {job_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    if not job.get("execute", False):
        log(f"[SKIP] Skipping company job because execute=false: {job.get('company_name')}")
        return

    company_name = job.get("company_name", "")
    start_date = job.get("start_date")
    end_date = job.get("end_date")
    if not company_name or not start_date or not end_date:
        log(f"[ERROR] Invalid job configuration: {job}")
        return

    _thread_state.company_name = company_name
    _thread_state.start_date = start_date
    _thread_state.end_date = end_date

    selection_start_time = datetime.now()
    log(f"[INFO] Starting company selection for '{company_name}' at {selection_start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    if not select_company(page, company_name):
        if _failure_logger:
            _failure_logger.log_failure("Company Selection", reason="Failed to select company in UI")
        return

    selection_end_time = datetime.now()
    selection_elapsed = (selection_end_time - selection_start_time).total_seconds()
    log(f"[OK] Company '{company_name}' selected at {selection_end_time.strftime('%Y-%m-%d %H:%M:%S')} (took {selection_elapsed:.2f} seconds)")

    today = datetime.today().strftime("%Y-%m-%d")
    period_folder = safe_text(f"{start_date}_to_{end_date}_{today}")
    company_folder_id = get_or_create_folder(service, root_folder_id, company_name)
    root_path = ["Prod_reports", company_name]
    log_drive_folder("Company folder", root_path, company_folder_id)

    # Prevailing Wage
    prevailing_item = get_report_item(job.get("reports", []), "prevailing_wage_report")
    if prevailing_item.get("prevailing_wage_report"):
        projects = prevailing_item.get("projects", [])
        if projects:
            pw_folder = get_or_create_folder(service, company_folder_id, "Prevailing Wage")
            period_folder_id = get_or_create_folder(service, pw_folder, period_folder)
            log_drive_folder("Download folder", root_path + ["Prevailing Wage", period_folder], period_folder_id)
            download_prevailing_wage_reports(service, page, company_name, projects, period_folder_id, start_date, end_date)
        else:
            log(f"[WARN] No prevailing wage projects configured for {company_name}")

    # Prevailing Wage Summary
    prevailing_summary_item = get_report_item(job.get("reports", []), "prevailingwage_summary_report")
    if prevailing_summary_item.get("prevailingwage_summary_report"):
        projects = prevailing_summary_item.get("projects", [])
        if projects:
            pw_summary_folder = get_or_create_folder(service, company_folder_id, "Prevailing wage Summary")
            period_folder_id = get_or_create_folder(service, pw_summary_folder, period_folder)
            log_drive_folder("Download folder", root_path + ["Prevailing wage Summary", period_folder], period_folder_id)
            download_prevailing_wage_summary_reports(service, page, company_name, projects, period_folder_id, start_date, end_date)
        else:
            log(f"[WARN] No prevailing wage summary projects configured for {company_name}")

    # Union Reports
    union_item = get_report_item(job.get("reports", []), "union_report")
    if union_item.get("union_report"):
        union_names = union_item.get("report_name", [])
        custom_report_flag = union_item.get("custom_report", False)
        union_folder = get_or_create_folder(service, company_folder_id, "Union")
        period_folder_id = get_or_create_folder(service, union_folder, period_folder)
        log_drive_folder("Download folder", root_path + ["Union", period_folder], period_folder_id)
        download_union_reports(service, page, company_name, union_names, period_folder_id, start_date, end_date, custom_report_flag)

    # Worker Compensation
    worker_item = get_report_item(job.get("reports", []), "worker_compensation_report")
    if worker_item.get("worker_compensation_report"):
        wc_folder = get_or_create_folder(service, company_folder_id, "Worker Compensation")
        period_folder_id = get_or_create_folder(service, wc_folder, period_folder)
        log_drive_folder("Download folder", root_path + ["Worker Compensation", period_folder], period_folder_id)
        download_worker_compensation_report(service, page, company_name, period_folder_id, start_date, end_date)

    # Payroll Register
    payroll_item = get_report_item(job.get("reports", []), "payroll_register_report")
    if not payroll_item:
        payroll_item = get_report_item(job.get("reports", []), "payroll_register")
    if payroll_item.get("payroll_register_report") or payroll_item.get("payroll_register"):
        pay_period_index = payroll_item.get("pay_period_index", 0)
        pr_folder = get_or_create_folder(service, company_folder_id, "Payroll Register")
        period_folder_id = get_or_create_folder(service, pr_folder, period_folder)
        log_drive_folder("Download folder", root_path + ["Payroll Register", period_folder], period_folder_id)
        download_payroll_register_report(service, page, company_name, period_folder_id, start_date, end_date, pay_period_index)

    # Job Costing
    job_costing_item = get_report_item(job.get("reports", []), "job_costing_report")
    if job_costing_item.get("job_costing_report"):
        jc_folder = get_or_create_folder(service, company_folder_id, "Job Costing")
        period_folder_id = get_or_create_folder(service, jc_folder, period_folder)
        log_drive_folder("Download folder", root_path + ["Job Costing", period_folder], period_folder_id)
        download_job_costing_report(service, page, company_name, period_folder_id, start_date, end_date)

    # 401K Report
    report_401k_item = get_report_item(job.get("reports", []), "401K_report")
    if report_401k_item.get("401K_report"):
        k401_folder = get_or_create_folder(service, company_folder_id, "401K")
        period_folder_id = get_or_create_folder(service, k401_folder, period_folder)
        log_drive_folder("Download folder", root_path + ["401K", period_folder], period_folder_id)
        download_401k_report(service, page, company_name, period_folder_id, start_date, end_date)

    # Payroll Journal
    payroll_journal_item = get_report_item(job.get("reports", []), "payroll_journal_report")
    if payroll_journal_item.get("payroll_journal_report"):
        pj_folder = get_or_create_folder(service, company_folder_id, "Payroll Journal")
        period_folder_id = get_or_create_folder(service, pj_folder, period_folder)
        log_drive_folder("Download folder", root_path + ["Payroll Journal", period_folder], period_folder_id)
        download_payroll_journal_report(service, page, company_name, period_folder_id, start_date, end_date)

    # Apprentice Ratio report
    apprentice_ratio_item = get_report_item(job.get("reports", []), "apprentice_ratio_report")
    if apprentice_ratio_item.get("apprentice_ratio_report"):
        app_ratio_folder = get_or_create_folder(service, company_folder_id, "Apprentice Ratio")
        period_folder_id = get_or_create_folder(service, app_ratio_folder, period_folder)
        log_drive_folder("Download folder", root_path + ["Apprentice Ratio", period_folder], period_folder_id)
        download_apprentice_ratio_reports(service, page, company_name, period_folder_id, start_date, end_date)

    # Summary of Wages
    summary_of_wages_item = get_report_item(job.get("reports", []), "summary_of_wages_report")
    if summary_of_wages_item.get("summary_of_wages_report"):
        sow_folder = get_or_create_folder(service, company_folder_id, "Summary of Wages")
        period_folder_id = get_or_create_folder(service, sow_folder, period_folder)
        log_drive_folder("Download folder", root_path + ["Summary of Wages", period_folder], period_folder_id)
        download_summary_of_wages_report(service, page, company_name, period_folder_id, start_date, end_date)

    # Child Support Remittance
    child_support_item = get_report_item(job.get("reports", []), "child_support_report")
    if child_support_item.get("child_support_report"):
        pay_period_index = child_support_item.get("pay_period_index", 0)
        cs_folder = get_or_create_folder(service, company_folder_id, "Child Support Remittance")
        period_folder_id = get_or_create_folder(service, cs_folder, period_folder)
        log_drive_folder("Download folder", root_path + ["Child Support Remittance", period_folder], period_folder_id)
        download_child_support_report(service, page, company_name, period_folder_id, start_date, end_date, pay_period_index)

    # Garnishment Report
    garnishment_item = get_report_item(job.get("reports", []), "garnishment_report")
    if garnishment_item.get("garnishment_report"):
        pay_period_index = garnishment_item.get("pay_period_index", 0)
        g_folder = get_or_create_folder(service, company_folder_id, "Garnishment")
        period_folder_id = get_or_create_folder(service, g_folder, period_folder)
        log_drive_folder("Download folder", root_path + ["Garnishment", period_folder], period_folder_id)
        download_garnishment_report(service, page, company_name, period_folder_id, start_date, end_date, pay_period_index)

    job_end_time = datetime.now()
    job_elapsed = (job_end_time - job_start_time).total_seconds()
    log(f"[DONE] Completed automation for company: {company_name} at {job_end_time.strftime('%Y-%m-%d %H:%M:%S')} (total time: {job_elapsed:.2f} seconds)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Automate report downloads and Google Drive uploads from JSON config.")
    parser.add_argument("config", nargs="?", default=str(DEFAULT_CONFIG_PATH), help="Path to JSON config file.")
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=3,
        help="Number of parallel browser workers (default: 3). Must match the number of Chrome instances running.",
    )
    parser.add_argument(
        "--ports",
        type=str,
        default="9222,9223,9224",
        help="Comma-separated CDP ports for each Chrome instance (default: 9222,9223,9224).",
    )
    args = parser.parse_args()

    ports = [int(p.strip()) for p in args.ports.split(",")]

    if len(ports) < args.workers:
        log(f"[ERROR] --workers={args.workers} but only {len(ports)} ports specified via --ports. "
            f"Add more ports or reduce --workers.")
        return

    ensure_dir(DOWNLOAD_DIR)

    global _failure_logger
    failure_log_path = BASE_DIR / f"failure_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    _failure_logger = FailureLogger(failure_log_path)
    log(f"[INFO] Failure log: {failure_log_path}")

    bootstrap_service = get_drive_service()
    prod_reports_id = get_or_create_folder(bootstrap_service, "root", "Prod_reports")
    log_drive_folder("Root reports folder", ["Prod_reports"], prod_reports_id)

    config = load_config(Path(args.config))
    executable_jobs = [job for job in config if job.get("execute", False)]

    if not executable_jobs:
        log("[INFO] No executable jobs found in config (all have execute=false).")
        return

    num_workers = min(args.workers, len(executable_jobs))
    log(f"\n[PARALLEL] {len(executable_jobs)} jobs  |  {num_workers} workers  |  ports={ports[:num_workers]}")
    log("=" * 60)

    # ── Distribute jobs into per-port lanes (round-robin) ──
    # Each port gets a dedicated thread. Jobs in a lane run one at a time
    # on that port — no new tab is opened until the previous company is done.
    job_lanes: list[list[dict]] = [[] for _ in range(num_workers)]
    for i, job in enumerate(executable_jobs):
        job_lanes[i % num_workers].append(job)

    def run_port_lane(port: int, jobs: list[dict]) -> None:
        """
        Process all jobs assigned to this port sequentially in one thread.

        WHY sequential: Playwright's sync API binds objects to the greenlet
        that created them. Each call to sync_playwright() inside this thread
        gives it its own greenlet + event-loop pair, so we create and destroy
        one playwright session per job.  Jobs never run concurrently on the
        same port, so only one tab is open per Chrome instance at any time.
        """
        _thread_state.port = port  # all log() calls in this thread get [port=X]

        for job in jobs:
            company = job.get("company_name", "?")
            log(f"[INFO] [{company}] Starting job on port {port}")

            with sync_playwright() as pw:
                try:
                    browser = pw.chromium.connect_over_cdp(f"http://localhost:{port}")
                except Exception as exc:
                    log(f"[ERROR] [{company}] Could not connect to Chrome on port {port}: {exc}")
                    log(f"[ERROR] Make sure Chrome is running with: chrome.exe --remote-debugging-port={port} --user-data-dir=<unique-dir>")
                    continue  # skip this job, try the next one on this port

                context = browser.contexts[0]

                # Discover the app base URL from the existing logged-in tab
                app_base_url = "https://admin.lumberfi.com"  # safe fallback
                for existing_page in context.pages:
                    raw_url = existing_page.url or ""
                    m = re.match(r"^(https?://[^/]+)", raw_url)
                    if m and not raw_url.startswith(("about:", "chrome-extension:", "chrome:")):
                        app_base_url = m.group(1)
                        break

                page = context.new_page()

                reports_url = f"{app_base_url}/reports/payroll"
                log(f"[INFO] [{company}] Opening app in new tab: {reports_url}")
                try:
                    page.goto(reports_url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass  # networkidle timeout is non-fatal; DOM content is enough
                page.wait_for_timeout(10000)  # extra grace period for JS to fully render

                try:
                    # Each worker gets its own Drive service — the underlying
                    # HTTP client is not guaranteed thread-safe when shared.
                    service = get_drive_service()
                    run_company_job(service, page, job, prod_reports_id)
                except Exception as exc:
                    log(f"[ERROR] [{company}] Job failed: {exc}")
                    traceback.print_exc()
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
                    # IMPORTANT: Do NOT call browser.close() — that would
                    # terminate the Chrome instance this port is connected to.

            log(f"[INFO] [{company}] Job complete on port {port}")

    # ── Launch one thread per port; each thread drains its job lane ──
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = [
            executor.submit(run_port_lane, ports[i], job_lanes[i])
            for i in range(num_workers)
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                log(f"[ERROR] Port lane raised an unhandled exception: {exc}")

    log("=" * 60)
    log("[DONE] All configured company jobs processed.")


if __name__ == "__main__":
    main()
