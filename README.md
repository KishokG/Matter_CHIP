## Overview & Procedure to run the Test case Mapping File Reviewer

This project provides a Python script to **review the Matter test case mapping JSON files** against a reference list of test cases stored in a **Google Sheet(TC_List)**.  

#### It checks for:
- Missing or extra test cases
- Empty or invalid `CertificationStatus` fields
- Consistency between `CertificationStatus` and `cert` fields
- Invalid characters inside `PICS` entries

---

## 🚀 Setup Instructions

#### 1. Clone the repository: 
git clone https://github.com/KishokG/Matter_CHIP.git

#### 2. Navigate to the directory:
cd Matter_CHIP/TC_MappingFile_Review/

#### 3. Create a virtual environment (folder .venv inside project):
python3 -m venv .venv

#### 4. Activate it:
source .venv/bin/activate

#### 5. Install dependencies:
pip install -r requirements.txt

## ⚙️ Configuration

1. All configurable values are stored in **config.yaml**
2. Service Account Setup:
