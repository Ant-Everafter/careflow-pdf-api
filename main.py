from fastapi import FastAPI, UploadFile, File, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pypdf import PdfReader
import os
import io
from typing import Dict, Any, Optional

app = FastAPI(title="CareFlow PDF Extraction API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_KEY = os.getenv("PDF_EXTRACTION_API_KEY", "")

@app.get("/")
def health_check():
    return {"status": "ok", "message": "CareFlow PDF Extraction API is running"}

@app.post("/extract-pdf")
async def extract_pdf(file: UploadFile = File(...), authorization: Optional[str] = Header(default=None)):
    if API_KEY:
        expected = f"Bearer {API_KEY}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="Invalid or missing API key")

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are allowed")

    try:
        pdf_bytes = await file.read()
        reader = PdfReader(io.BytesIO(pdf_bytes))
        raw_fields = {}

        fields = reader.get_fields()
        if not fields:
            return {
                "client": {},
                "weekly": {},
                "sessions": [],
                "rawFields": {},
                "warning": "No AcroForm fields found in this PDF."
            }

        for field_name, field_data in fields.items():
            value = field_data.get("/V", "")
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value)
            else:
                value = str(value) if value is not None else ""
            raw_fields[field_name] = value

        mapped = map_support_pdf_fields(raw_fields)
        return {**mapped, "rawFields": raw_fields}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF extraction failed: {str(e)}")


def first_non_empty(raw: Dict[str, str], keys):
    for key in keys:
        value = raw.get(key, "")
        if value and value.strip():
            return value.strip()
    return ""


def map_support_pdf_fields(raw: Dict[str, str]) -> Dict[str, Any]:
    client = {
        "name": first_non_empty(raw, ["Full Name", "Full Name2", "Client Name"]),
        "email": first_non_empty(raw, ["Client's Email", "eMail", "Client Email"]),
        "address": first_non_empty(raw, ["Address"]),
        "room": first_non_empty(raw, ["Room No", "Room"]),
        "supportWorker": first_non_empty(raw, ["Support workers name", "Support workers name2", "Key / Support worker"]),
        "supportWorkerNumber": first_non_empty(raw, ["Support workers contact number", "Support Worker No"])
    }

    weekly = {
        "supportDate": first_non_empty(raw, ["Date of weekly session", "Date of weekly session2", "Support Date"]),
        "weekEnding": first_non_empty(raw, ["Date of weekly session", "Date of weekly session2"]),
        "weeklySupportNo": first_non_empty(raw, ["Weekly session number", "Weekly Support No."]),
        "moveInDate": first_non_empty(raw, ["Date resident moved into new accommodation", "Move in date"]),
        "dueBy": first_non_empty(raw, ["Due by"]),
        "totalTime": first_non_empty(raw, ["TOTALINPROPERTY", "Total time spent"])
    }

    sessions = []
    outcome_field_groups = [
        ["DropdownOutcomes.1"],
        ["DropdownOutcomes.4", "DropdownOutcomes.2"],
        ["DropdownOutcomes.7", "DropdownOutcomes.3"],
        ["DropdownOutcomes.10", "DropdownOutcomes.4"],
        ["DropdownOutcomes.13", "DropdownOutcomes.5"],
    ]

    for i in range(1, 6):
        session = {
            "date": first_non_empty(raw, [f"DATERow{i}"]),
            "deliveryMethod": first_non_empty(raw, [f"SUPPORT-DELIVERED{i}"]),
            "supportArea": first_non_empty(raw, outcome_field_groups[i-1]),
            "notes": first_non_empty(raw, [f"Text{i}"])
        }
        if session["date"] or session["supportArea"] or session["notes"]:
            sessions.append(session)

    return {"client": client, "weekly": weekly, "sessions": sessions}
