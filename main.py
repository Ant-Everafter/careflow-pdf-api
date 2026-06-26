from __future__ import annotations

import io
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pypdf import PdfReader, PdfWriter


APP_TITLE = "CareFlow PDF API"
MAX_FILE_SIZE_BYTES = int(os.getenv("MAX_PDF_SIZE_MB", "20")) * 1024 * 1024
API_KEY = os.getenv("PDF_EXTRACTION_API_KEY", "")

app = FastAPI(
    title=APP_TITLE,
    version="1.0.0",
    description=(
        "Extracts weekly support session data and fills reviewed support-plan "
        "updates into editable AcroForm PDFs."
    ),
)

# During early testing this permits any Lovable preview/domain. For production,
# set ALLOWED_ORIGINS to comma-separated domains, e.g.
# https://your-app.lovable.app,https://your-domain.com
allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "*")
allowed_origins = (
    ["*"]
    if allowed_origins_env.strip() == "*"
    else [x.strip() for x in allowed_origins_env.split(",") if x.strip()]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=allowed_origins != ["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "X-Filled-Rows"],
)


# ---------------------------------------------------------------------------
# Health and authentication helpers
# ---------------------------------------------------------------------------

@app.get("/")
def health_check() -> Dict[str, str]:
    return {
        "status": "ok",
        "message": "CareFlow PDF API is running",
        "version": "1.0.0",
    }


@app.get("/health")
def health() -> Dict[str, str]:
    return health_check()


def require_api_key(authorization: Optional[str]) -> None:
    """Validate an optional bearer key configured in Render."""
    if not API_KEY:
        return

    expected = f"Bearer {API_KEY}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


async def read_pdf_upload(file: UploadFile) -> bytes:
    filename = file.filename or "uploaded.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are allowed")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="The uploaded PDF is empty")

    if len(pdf_bytes) > MAX_FILE_SIZE_BYTES:
        limit_mb = MAX_FILE_SIZE_BYTES // (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"PDF exceeds the {limit_mb} MB file-size limit",
        )

    return pdf_bytes


def open_pdf_reader(pdf_bytes: bytes) -> PdfReader:
    """Open an AcroForm PDF, including PDFs encrypted with an empty password."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
        if reader.is_encrypted:
            result = reader.decrypt("")
            if result == 0:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "This PDF is password protected. Upload a copy that can be "
                        "opened without entering a password."
                    ),
                )
        return reader
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Could not open the PDF: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# PDF field extraction
# ---------------------------------------------------------------------------

def field_value_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(field_value_to_text(item) for item in value)
    return str(value).replace("\x00", "").strip()


def extract_raw_fields(reader: PdfReader) -> Dict[str, str]:
    fields = reader.get_fields() or {}
    raw_fields: Dict[str, str] = {}

    for field_name, field_data in fields.items():
        raw_fields[field_name] = field_value_to_text(field_data.get("/V", ""))

    return raw_fields


def first_non_empty(raw: Dict[str, str], keys: Iterable[str]) -> str:
    for key in keys:
        value = raw.get(key, "")
        if value and value.strip():
            return value.strip()
    return ""


def map_support_pdf_fields(raw: Dict[str, str]) -> Dict[str, Any]:
    client = {
        "name": first_non_empty(raw, ["Full Name", "Full Name2", "Client Name"]),
        "email": first_non_empty(
            raw, ["Client's Email", "eMail", "Client Email"]
        ),
        "address": first_non_empty(raw, ["Address"]),
        "room": first_non_empty(raw, ["Room No", "Room"]),
        "supportWorker": first_non_empty(
            raw,
            ["Support workers name", "Support workers name2", "Key / Support worker"],
        ),
        "supportWorkerNumber": first_non_empty(
            raw, ["Support workers contact number", "Support Worker No"]
        ),
    }

    weekly = {
        "supportDate": first_non_empty(
            raw,
            ["Date of weekly session", "Date of weekly session2", "Support Date"],
        ),
        "weekEnding": first_non_empty(
            raw, ["Date of weekly session", "Date of weekly session2"]
        ),
        "weeklySupportNo": first_non_empty(
            raw, ["Weekly session number", "Weekly Support No."]
        ),
        "moveInDate": first_non_empty(
            raw, ["Date resident moved into new accommodation", "Move in date"]
        ),
        "dueBy": first_non_empty(raw, ["Due by"]),
        "totalTime": first_non_empty(
            raw, ["TOTALINPROPERTY", "Total time spent"]
        ),
    }

    sessions: List[Dict[str, str]] = []
    # In this source PDF the outcome dropdowns are numbered 1,4,7,10,13.
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
            "supportArea": first_non_empty(raw, outcome_field_groups[i - 1]),
            "notes": first_non_empty(raw, [f"Text{i}"]),
        }
        if session["date"] or session["supportArea"] or session["notes"]:
            sessions.append(session)

    return {"client": client, "weekly": weekly, "sessions": sessions}


@app.post("/extract-pdf")
async def extract_pdf(
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    require_api_key(authorization)
    pdf_bytes = await read_pdf_upload(file)

    try:
        reader = open_pdf_reader(pdf_bytes)
        raw_fields = extract_raw_fields(reader)

        if not raw_fields:
            return {
                "client": {},
                "weekly": {},
                "sessions": [],
                "rawFields": {},
                "warning": "No AcroForm fields were found in this PDF.",
            }

        mapped = map_support_pdf_fields(raw_fields)
        return {**mapped, "rawFields": raw_fields}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"PDF extraction failed: {exc}",
        ) from exc


@app.post("/list-fields")
async def list_fields(
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Debug endpoint: list every AcroForm field, type, current value and options."""
    require_api_key(authorization)
    pdf_bytes = await read_pdf_upload(file)
    reader = open_pdf_reader(pdf_bytes)
    fields = reader.get_fields() or {}

    result: List[Dict[str, Any]] = []
    for name, field in fields.items():
        result.append(
            {
                "name": name,
                "type": field_value_to_text(field.get("/FT", "")),
                "value": field_value_to_text(field.get("/V", "")),
                "options": choice_options(field),
            }
        )

    return {"count": len(result), "fields": result}


# ---------------------------------------------------------------------------
# Reviewed AI update parsing and support-plan merging
# ---------------------------------------------------------------------------

AREA_ALIASES = {
    "manage personal relationships": "Ho Manage personal relationships",
    "ho manage personal relationships": "Ho Manage personal relationships",
    "emotional support": "Emotional support",
    "maintain accommodation": "Maintain accommodation",
    "develop independent living skills": "Develop independent living skills",
    "abide by house rules": "Abide by house rules",
    "healthy diet plan": "Healthy diet plan",
    "engage in local community initiatives": "Engage in local community initiatives",
    "take part in charitable contributions": "Take part in charitable contributions",
    "access employment training educational": "Access Employment/training/educational",
    "access employment/training/educational": "Access Employment/training/educational",
    "access leisure social cultural faith activities": (
        "Access leisure/social/cultural/faith activitiesmelessness"
    ),
    "access leisure/social/cultural/faith activities": (
        "Access leisure/social/cultural/faith activitiesmelessness"
    ),
}


def normalize_spaces(value: Any) -> str:
    return re.sub(r"\s+", " ", field_value_to_text(value)).strip()


def normalize_key(value: Any) -> str:
    text = normalize_spaces(value).casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_area(value: Any) -> str:
    cleaned = normalize_spaces(value)
    alias = AREA_ALIASES.get(normalize_key(cleaned))
    return alias or cleaned


def canonical_status(value: Any) -> str:
    key = normalize_key(value)
    if not key:
        return ""
    if key in {"cont", "continue", "continued", "continuing", "ongoing"}:
        return "Cont..."
    if key in {"met", "complete", "completed", "achieved"}:
        return "Met"
    if key in {"pending", "not started", "awaiting"}:
        return "Pending"
    if "cont" in key:
        return "Cont..."
    return normalize_spaces(value)


def compact_date(value: Any) -> str:
    """Return support-plan dates as dd/mm/yy when a common date format is supplied."""
    text = normalize_spaces(value)
    if not text:
        return ""

    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%y"):
        try:
            return datetime.strptime(text, fmt).strftime("%d/%m/%y")
        except ValueError:
            pass
    return text


def parse_updates_json(updates_text: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    try:
        payload = json.loads(updates_text)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"The updates field is not valid JSON: {exc.msg}",
        ) from exc

    metadata: Dict[str, Any] = payload if isinstance(payload, dict) else {}

    if isinstance(payload, list):
        source_items = payload
    elif isinstance(payload, dict):
        source_items = (
            payload.get("updates")
            or payload.get("outcomes")
            or payload.get("generatedPage2")
            or payload.get("generated_page2")
            or []
        )
    else:
        source_items = []

    if isinstance(source_items, dict):
        source_items = (
            source_items.get("updates")
            or source_items.get("outcomes")
            or []
        )

    if not isinstance(source_items, list):
        raise HTTPException(
            status_code=400,
            detail="updates JSON must contain an 'updates' or 'outcomes' array",
        )

    parsed: List[Dict[str, Any]] = []
    for position, item in enumerate(source_items, start=1):
        if not isinstance(item, dict):
            continue

        area = first_item_value(
            item,
            ["supportArea", "outcomeName", "outcome", "area", "name"],
        )
        date = first_item_value(item, ["date", "updateDate", "sessionDate"])
        update_text = first_item_value(
            item,
            ["generatedText", "update", "updateText", "text", "notes"],
        )
        status = first_item_value(item, ["status", "outcomeStatus"]) or "Cont..."
        index = item.get("index", item.get("row", item.get("slot")))

        try:
            explicit_index = int(index) if index is not None and str(index).strip() else None
        except (TypeError, ValueError):
            explicit_index = None

        # Some app schemas use zero-based indexes.
        if explicit_index == 0:
            explicit_index = 1

        if not area and not update_text and not date:
            continue

        parsed.append(
            {
                "sourcePosition": position,
                "index": explicit_index,
                "supportArea": canonical_area(area),
                "date": compact_date(date),
                "update": normalize_spaces(update_text),
                "status": canonical_status(status),
            }
        )

    if not parsed:
        raise HTTPException(
            status_code=400,
            detail="No usable reviewed support updates were found in the updates JSON",
        )

    return parsed, metadata


def first_item_value(item: Dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = item.get(key)
        if value is not None and normalize_spaces(value):
            return normalize_spaces(value)
    return ""


def read_existing_rows(raw_fields: Dict[str, str], max_rows: int = 20) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for row_number in range(1, max_rows + 1):
        rows.append(
            {
                "row": str(row_number),
                "supportArea": canonical_area(raw_fields.get(f"Dropdown.List{row_number}", "")),
                "date": compact_date(raw_fields.get(f"Date{row_number}_af_date", "")),
                "update": normalize_spaces(raw_fields.get(f"Dropdown-update{row_number}", "")),
                "status": canonical_status(
                    raw_fields.get(f"Dropdown outcomes met{row_number}", "")
                )
                if normalize_spaces(raw_fields.get(f"Dropdown outcomes met{row_number}", ""))
                else "",
            }
        )
    return rows


def merge_updates_into_rows(
    existing_rows: List[Dict[str, str]],
    reviewed_updates: List[Dict[str, Any]],
    use_explicit_indexes: bool = False,
) -> Tuple[List[Dict[str, str]], List[int]]:
    """
    Preserve existing support areas. If an incoming area already exists, update that
    row. Otherwise place it in the first empty row. This matches the form's design:
    one ongoing row per support outcome, carried forward week to week.
    """
    rows = [dict(row) for row in existing_rows]
    touched: List[int] = []

    def first_empty_row() -> Optional[int]:
        for idx, row in enumerate(rows):
            if not normalize_spaces(row.get("supportArea", "")):
                return idx
        return None

    for incoming in reviewed_updates:
        area = canonical_area(incoming.get("supportArea", ""))
        target_idx: Optional[int] = None

        explicit_index = incoming.get("index")
        if use_explicit_indexes and isinstance(explicit_index, int):
            if 1 <= explicit_index <= len(rows):
                target_idx = explicit_index - 1

        if target_idx is None and area:
            area_key = normalize_key(area)
            for idx, row in enumerate(rows):
                if normalize_key(row.get("supportArea", "")) == area_key:
                    target_idx = idx
                    break

        if target_idx is None:
            target_idx = first_empty_row()

        if target_idx is None:
            raise HTTPException(
                status_code=422,
                detail="The support-plan table has no empty rows remaining",
            )

        current = rows[target_idx]
        if area:
            current["supportArea"] = area
        if incoming.get("date"):
            current["date"] = compact_date(incoming["date"])
        if incoming.get("update"):
            current["update"] = normalize_spaces(incoming["update"])
        current["status"] = canonical_status(incoming.get("status", "Cont..."))

        row_number = target_idx + 1
        if row_number not in touched:
            touched.append(row_number)

    return rows, touched


# ---------------------------------------------------------------------------
# AcroForm option matching, summary creation and form filling
# ---------------------------------------------------------------------------

def choice_options(field: Dict[str, Any]) -> List[str]:
    options = field.get("/Opt", []) or []
    result: List[str] = []
    for option in options:
        if isinstance(option, (list, tuple)) and option:
            # PDF choice options may be [export_value, display_value].
            value = option[0]
        else:
            value = option
        result.append(field_value_to_text(value))
    return result


def option_for_field(
    fields: Dict[str, Any], field_name: str, desired: str, kind: str
) -> str:
    """Match a friendly value to the exact export value accepted by a dropdown."""
    desired = canonical_area(desired) if kind == "area" else canonical_status(desired)
    field = fields.get(field_name, {})
    options = choice_options(field)

    if not options:
        return desired

    desired_key = normalize_key(desired)
    for option in options:
        candidate = canonical_area(option) if kind == "area" else canonical_status(option)
        if normalize_key(candidate) == desired_key:
            return option

    # Some rows dynamically remove options already used elsewhere. If the field is
    # editable (combo box), pypdf/Acrobat can still retain the supplied value.
    return desired


def create_support_summary(
    rows: List[Dict[str, str]],
    week_ending: str,
    old_summary: str = "",
) -> str:
    heading_date = normalize_spaces(week_ending)
    lines = [f"SUPPORT PLAN OUTCOMES UPDATE TO WEEK ENDING: {heading_date}"]

    populated = [row for row in rows if normalize_spaces(row.get("supportArea", ""))]
    for row in populated:
        area = normalize_spaces(row.get("supportArea", ""))
        date = compact_date(row.get("date", ""))
        update = normalize_spaces(row.get("update", ""))
        status = canonical_status(row.get("status", "Cont..."))

        detail_parts = [part for part in [date, update] if part]
        details = ": ".join(detail_parts)
        if details:
            lines.append(f"{area}: {details} [{status}]")
        else:
            lines.append(f"{area}: [{status}]")

    # Preserve the existing risks section if the form already contains one.
    risk_marker = "RISKS UPDATE REVIEWED"
    old_upper = old_summary.upper()
    marker_pos = old_upper.find(risk_marker)
    if marker_pos >= 0:
        risk_section = old_summary[marker_pos:].strip()
    else:
        risk_section = (
            "RISKS UPDATE REVIEWED (if any...):\n"
            ": : [ ]\n: : [ ]\n: : [ ]\n: : [ ]\n: : [ ]"
        )

    return "\n".join(lines) + "\n\n" + risk_section


def build_field_updates(
    fields: Dict[str, Any],
    rows: List[Dict[str, str]],
    old_rows: List[Dict[str, str]],
    week_ending: str,
    old_summary: str,
) -> Dict[str, str]:
    values: Dict[str, str] = {}

    for index, row in enumerate(rows, start=1):
        old = old_rows[index - 1]
        changed = any(
            normalize_spaces(row.get(key, "")) != normalize_spaces(old.get(key, ""))
            for key in ("supportArea", "date", "update", "status")
        )
        if not changed:
            continue

        area_name = f"Dropdown.List{index}"
        status_name = f"Dropdown outcomes met{index}"
        update_name = f"Dropdown-update{index}"
        date_name = f"Date{index}_af_date"

        if area_name in fields:
            values[area_name] = option_for_field(
                fields, area_name, row.get("supportArea", ""), "area"
            )
        if status_name in fields:
            values[status_name] = option_for_field(
                fields, status_name, row.get("status", "Cont..."), "status"
            )
        if update_name in fields:
            values[update_name] = normalize_spaces(row.get("update", ""))
        if date_name in fields:
            values[date_name] = compact_date(row.get("date", ""))

    if "SUPPORT-PLAN-OUTCOMES" in fields:
        values["SUPPORT-PLAN-OUTCOMES"] = create_support_summary(
            rows=rows,
            week_ending=week_ending,
            old_summary=old_summary,
        )

    return values


def page_field_names(page: Any) -> set[str]:
    """Return the AcroForm field names whose widgets are present on this page."""
    names: set[str] = set()
    for annotation_ref in page.get("/Annots", []) or []:
        annotation = annotation_ref.get_object()
        name = annotation.get("/T")
        if not name and annotation.get("/Parent"):
            parent = annotation["/Parent"].get_object()
            name = parent.get("/T")
        if name:
            names.add(str(name))
    return names


def apply_field_updates(reader: PdfReader, field_updates: Dict[str, str]) -> bytes:
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)

    # Keep the form editable and request appearance regeneration in PDF viewers.
    writer.set_need_appearances_writer(True)

    # Only send each page the fields whose widgets actually live on that page.
    # This is important for choice fields: repeatedly sending the same dropdown to
    # unrelated pages can cause some PDF engines to reset its selected value.
    for page in writer.pages:
        names_on_page = page_field_names(page)
        page_updates = {
            name: value
            for name, value in field_updates.items()
            if name in names_on_page
        }
        if not page_updates:
            continue
        writer.update_page_form_field_values(
            page,
            page_updates,
            auto_regenerate=True,
            flatten=False,
        )

    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def safe_download_filename(original_filename: str) -> str:
    stem = Path(original_filename or "support.pdf").stem
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")
    return f"{safe_stem or 'support'}_UPDATED_EDITABLE.pdf"


# ---------------------------------------------------------------------------
# Final editable PDF generation endpoint
# ---------------------------------------------------------------------------

@app.post("/fill-pdf")
async def fill_pdf(
    file: UploadFile = File(...),
    updates: str = Form(...),
    authorization: Optional[str] = Header(default=None),
) -> Response:
    """
    Receive multipart/form-data:
      - file: the original editable support PDF
      - updates: JSON string containing reviewed AI updates

    Accepted JSON examples:

    {"updates": [{"date":"23/03/2026", "supportArea":"Maintain accommodation",
                  "generatedText":"...", "status":"Cont..."}]}

    {"outcomes": [{"index":1, "outcomeName":"Maintain accommodation",
                   "date":"23/03/2026", "update":"...", "status":"Cont..."}]}

    Existing support areas in the PDF are preserved. Incoming updates replace the
    latest date/text/status for the same support area; brand-new areas use the first
    empty support-plan row. The returned PDF is not flattened and stays editable.
    """
    require_api_key(authorization)
    pdf_bytes = await read_pdf_upload(file)
    reviewed_updates, payload_metadata = parse_updates_json(updates)

    try:
        reader = open_pdf_reader(pdf_bytes)
        fields = reader.get_fields() or {}
        if not fields:
            raise HTTPException(
                status_code=422,
                detail="No editable AcroForm fields were found in this PDF",
            )

        raw_fields = extract_raw_fields(reader)
        existing_rows = read_existing_rows(raw_fields)

        use_explicit_indexes = bool(
            payload_metadata.get("useExplicitIndexes")
            or payload_metadata.get("use_explicit_indexes")
            or payload_metadata.get("mode") == "explicit-index"
        )

        merged_rows, touched_rows = merge_updates_into_rows(
            existing_rows,
            reviewed_updates,
            use_explicit_indexes=use_explicit_indexes,
        )

        week_ending = (
            first_item_value(
                payload_metadata,
                ["weekEnding", "supportDate", "week_ending", "support_date"],
            )
            or first_non_empty(
                raw_fields,
                ["Date of weekly session", "Date of weekly session2", "Due by"],
            )
        )

        old_summary = raw_fields.get("SUPPORT-PLAN-OUTCOMES", "")
        field_updates = build_field_updates(
            fields=fields,
            rows=merged_rows,
            old_rows=existing_rows,
            week_ending=week_ending,
            old_summary=old_summary,
        )

        if not field_updates:
            raise HTTPException(
                status_code=422,
                detail="No matching Page 2 fields could be updated",
            )

        completed_pdf = apply_field_updates(reader, field_updates)
        filename = safe_download_filename(file.filename or "support.pdf")

        return Response(
            content=completed_pdf,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Filled-Rows": ",".join(str(row) for row in touched_rows),
                "Cache-Control": "no-store",
            },
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"PDF filling failed: {exc}",
        ) from exc
