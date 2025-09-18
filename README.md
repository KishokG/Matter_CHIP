## Overview & Procedure to run the Test case Mapping File Reviewer

This project provides a Python script to **review the Matter test case mapping JSON files** against a reference list of test cases stored in a **Google Sheet(TC_List)**.  

### 🔍 It performs the following checks:

1. **Test Case List Validation**
   - Compares **Column D** in the Google Sheet with JSON test case IDs.
   - Reports:
     - Test cases present in the sheet but missing in JSON.
     - Extra/unwanted test cases in JSON not listed in the sheet.

2. **Certification Status Checks**
   - Detects empty `"CertificationStatus": ""` entries.
   - Ensures consistency:
     - If `"CertificationStatus": "Executable"`, the next line must be `"cert": "true"`.
     - If `"CertificationStatus": "Provisional"` or `"Blocked"`, the next line must be `"cert": "false"`.

3. **Google Sheet vs JSON (Column F Validation)**
   - **Column F (“Certification Status”)** in the sheet is compared against JSON:
     - Sheet = **Executable** → JSON must be `"CertificationStatus": "Executable"` and `"cert": "true"`.
     - Sheet = **Provisional** → JSON must be `"CertificationStatus": "Provisional"` and `"cert": "false"`.
     - Sheet = **Blocked** → JSON must be `"CertificationStatus": "Blocked"` and `"cert": "false"`.
   - Any mismatch between the sheet and JSON is logged with line number + test case ID.

4. **PICS Validation**
   - Inside `"PICS": [ ... ]` blocks, checks for forbidden characters:
     - `,  &  (  )  {  }  -`
   - Logs invalid entries with test case ID and line number.
     
---

## 🚀 Setup Instructions

1. **Clone the repository:**
   ```bash
   git clone https://github.com/KishokG/Matter_CHIP.git
   
2. **Navigate to the directory:**
   ```bash
   cd Matter_CHIP/TC_MappingFile_Review/

3. **Create a virtual environment (folder .venv inside project):**
   ```bash
   python3 -m venv .venv

4. **Activate it:**
   ```bash
   source .venv/bin/activate

5. **Install dependencies:**
   ```bash
   pip install -r requirements.txt

---

## ⚙️ Configuration

1. All configurable values are stored in **`config.yaml`**.  
2. **Before running the script, update the config file with the correct values for:**  
   - Google Sheets credentials file (`credentials_file`)  
   - Google Sheet URL (`sheet_url`)  
   - Worksheet/tab name (`worksheet_name`)  
   - Local JSON file path (`json_file`)  
   - Output log file (`output_file`)  

---

## 🔑 Google Service Account Setup

1. Go to **[Google Cloud Console](https://console.cloud.google.com/)**.  

2. **Enable the Google Sheets API**  
   - In Cloud Console, go to **APIs & Services → Library**  
   - Search for **Google Sheets API**  
   - Click **Google Sheets API → Enable**  

3. **Create a Service Account and download the credentials JSON file**  
   - In Cloud Console, go to **IAM & Admin → Service Accounts**  
   - Click **➕ CREATE SERVICE ACCOUNT**  
     - **Service account name**: e.g. `json-reviewer`  
     - **Service account ID** will be auto-filled  
   - Click **CREATE** (you can skip granting roles here; roles are for broader GCP access — not required just to access a spreadsheet if you share the sheet with the service account)  
   - After creation, click the service account entry → **Keys** tab → **Add Key → Create new key**  
   - Choose **JSON → Create** → a file like `credentials-XXXXX.json` will download  
   - **Save and rename** it to `credentials.json` (or keep the filename and update the path in `config.yaml`)  

4. **Update your project**  
   - Place the `credentials.json` file inside your project folder  
   - Open **`config.yaml`** and update the `credentials_file` path if needed  

5. **Share your Google Sheet with the Service Account**  
   - Open your Google Sheet in the browser  
   - Click **Share** (top-right)  
   - Open the downloaded `credentials.json` in an editor and copy the value of **`client_email`**  
     ```json
     "client_email": "json-reviewer@my-project.iam.gserviceaccount.com"
     ```  
   - Paste this email into the Sheet’s share dialog and grant at least **Viewer** access (or **Editor**)  

---

## ▶️ Running the Script

1. **Activate your virtual environment** (if not already active):  
   ```bash
   source .venv/bin/activate
2. **Run the script**
   ```bash
   python3 JSON_comparision.py
