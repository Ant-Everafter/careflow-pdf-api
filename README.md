# CareFlow PDF Extraction API

FastAPI service for Lovable.

## Render Settings

Build Command:
pip install -r requirements.txt

Start Command:
uvicorn main:app --host 0.0.0.0 --port $PORT

## Endpoint

POST /extract-pdf

Form-data:
file = uploaded PDF

Returns JSON:
client, weekly, sessions, rawFields
