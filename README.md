# CareFlow PDF API

## Render settings

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

## Endpoints

- `GET /` - health check
- `POST /extract-pdf` - extract client/session fields
- `POST /list-fields` - debug/list all form fields
- `POST /fill-pdf` - merge reviewed updates into Page 2 and return an editable PDF

## `/fill-pdf` request

Send `multipart/form-data`:

- `file`: original PDF
- `updates`: JSON string

Example:

```json
{
  "weekEnding": "22/03/2026",
  "updates": [
    {
      "date": "23/03/2026",
      "supportArea": "Develop independent living skills",
      "generatedText": "Supported the tenant with building consistent daily routines.",
      "status": "Cont..."
    }
  ]
}
```

The endpoint preserves existing support outcomes, updates matching outcomes, adds new outcomes to the first empty row, fills the summary field, and does not flatten the PDF.
