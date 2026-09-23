# Test Case Sheet Validator

Reads the two tabs described in the brief, checks every rule (test-case-ID
consistency, empty cells, argument spacing, D-vs-F argument matching, the
PICS/config flags, and the cross-check against the master tab), and writes
a single self-contained HTML report you can open in any browser.

## Files

| File | What it does |
|---|---|
| `config.py` | Every setting you're likely to touch: sheet ID, tab names, column layout, rule exceptions. |
| `sheets_client.py` | Talks to the Google Sheets API. |
| `sheet_utils.py` | Small shared helpers (column-letter math, safe cell access). |
| `validators.py` | The actual rules from the brief - one function per rule. |
| `generate_report.py` | Main script: fetch both tabs, run the rules, render the report. |
| `report_template.html` | The report's HTML/CSS/JS. Edit this to change how it looks. |
| `demo_with_sample_data.py` | Builds a fake sheet in memory and renders the same report - no Google account or credentials needed. Good first step. |

## 1. Try it with no setup

```bash
python demo_with_sample_data.py
```

This opens/writes `demo_report.html` next to the script, built from a small
made-up sheet with a handful of planted mistakes, so you can see exactly
what the report looks like and how each rule reads before touching your
real spreadsheet.

## 2. Connect it to your real Google Sheet

1. **Create a service account** in the Google Cloud console (APIs & Services
   → Credentials → Create Credentials → Service account), then create a JSON
   key for it and download it. Save it next to these scripts as
   `service_account.json` (or point `--service-account-file` at wherever you
   put it).
2. **Enable the Google Sheets API** (and Google Drive API) for that project.
3. **Share your spreadsheet** with the service account's email address
   (looks like `something@your-project.iam.gserviceaccount.com`) - Viewer
   access is enough, since the script only reads.
4. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
5. **Set your spreadsheet ID** in `config.py` (`SPREADSHEET_ID`), or pass it
   on the command line:
   ```bash
   python generate_report.py --spreadsheet-id 1AbCDeFGhijkLMNoPQRstuVWxyz
   ```
6. Open the `validation_report.html` it writes (it also tries to open your
   browser automatically; pass `--no-open` to skip that).

## Adjusting the rules

Everything that's a judgment call rather than a hard rule from the brief is
called out in `config.py` with a comment, in particular:

- `ARGS_EXEMPT_FROM_D_F_CROSS_CHECK` / `STANDARD_EXECUTION_ARGS_EXEMPT_FROM_D_F_CROSS_CHECK`
  - which arguments are allowed to appear in column F without also being in
    column D (PICS, plus the standard commissioning/connection flags that
    are always in F regardless of what's in D). Empty these sets for a
    fully literal reading of "any arg in D must be in F and vice versa."
- `NO_ARGS_NOTE` - the exact phrase column D uses to mean "no arguments."
- `PICS_FLAG_ALIASES_IN_G` / `CONFIG_FLAG_ALIASES_IN_G` - accepted spellings
  for the PICS and config flags in column G.
- Tab names and header/data row numbers for both tabs.

## Notes

- Rows where every cell is blank are skipped entirely (treated as spacer
  rows, not data).
- Severity: cell-content problems (empty cells, ID mismatches, argument
  mismatches, missing flags) are reported as **errors**; the "missing a
  space after an argument" check is a **warning** so it doesn't drown out
  the more serious findings.
