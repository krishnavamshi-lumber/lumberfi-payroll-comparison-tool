# PDF and CSV Comparison Tool

A web-based tool for comparing PDFs and CSVs using side-by-side diff highlighting.

## Features

- Upload two PDFs: one as "truth" and one as "compare" to generate a diff PDF with highlights.
- Upload two CSVs: one as "truth" and one as "compare" to generate a diff Excel file with color coding.
- Optional: Compare only PDFs, only CSVs, or both.
- Download the generated diff files.
- Compare files directly from Google Drive using a service account JSON, and save the generated diffs back into a `difference` folder.

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

2. Run the app:
   ```
   python app.py
   ```

3. Open your browser to `http://127.0.0.1:5000/`

## Usage

- Fill in the form with the files you want to compare.
- Provide project name and report type for PDF comparison.
- Click "Compare" to process.
- Download the results from the results page.

## Dependencies

- Flask: Web framework
- PyMuPDF: PDF processing
- pandas: CSV processing
- openpyxl: Excel generation
- google-api-python-client: Google Drive access
- google-auth: Google service account authentication