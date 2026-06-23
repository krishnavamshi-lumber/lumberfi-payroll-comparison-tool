# LumberFi Payroll Report Downloader & Comparison Tool

A tool to bulk-download payroll reports from the LumberFi web app via Chrome remote debugging, then compare them against expected values.

## Prerequisites

- Google Chrome installed at `C:\Program Files\Google\Chrome\Application\chrome.exe`
- Python 3.x with dependencies installed:
  ```
  pip install -r requirements.txt
  ```

---

## Step 1 — Launch Chrome Instances with Remote Debugging

Open a PowerShell terminal and run the following to start one Chrome instance per worker. Each instance needs a unique debugging port and user data directory:

```powershell
$chromePath = "C:\Program Files\Google\Chrome\Application\chrome.exe"

Start-Process $chromePath -ArgumentList '--remote-debugging-port=9222 --user-data-dir="C:\temp\chrome-profile-1"'
Start-Process $chromePath -ArgumentList '--remote-debugging-port=9223 --user-data-dir="C:\temp\chrome-profile-2"'
Start-Process $chromePath -ArgumentList '--remote-debugging-port=9224 --user-data-dir="C:\temp\chrome-profile-3"'
```

> Add or remove `Start-Process` lines depending on how many parallel workers you want.

---

## Step 2 — Log In to Production

In **each** of the Chrome windows that opened, navigate to the LumberFi production app and log in with your credentials. All windows must be authenticated before proceeding.

---

## Step 3 — Download Reports

Run the downloader, passing the JSON config file for the report type and the number of Chrome instances you opened:

```powershell
python report_downloader.py <json_file> --workers <number_of_chrome_instances>
```

**Example** (3 workers, downloading the payroll journal):

```powershell
python report_downloader.py payroll_journal.json --workers 3
```

Repeat this command for each JSON config file you need to process. Available report configs:

| File | Report Type |
|------|-------------|
| `payroll_journal.json` | Payroll Journal |
| `payrollregister.json` | Payroll Register |
| `summary_of_wages.json` | Summary of Wages |
| `jobcosting.json` | Job Costing |
| `prevailing_wage.json` | Prevailing Wage |
| `prevailingwage_summary.json` | Prevailing Wage Summary |
| `union_report.json` | Union Report |
| `sievert_union.json` | Sievert Union |
| `worker_comp.json` | Worker Comp |
| `garnishment_report.json` | Garnishment Report |
| `child_support.json` | Child Support |
| `401k_report.json` | 401k Report |
| `apprentice_ratio.json` | Apprentice Ratio |

---

## Step 4 — Run the Comparison

Once all reports have been downloaded, run the comparison script:

```powershell
python comparison_2.py
```

This compares the downloaded reports against the expected values and outputs the differences.
