# AI-CLAN Notification

AI-CLAN exposes one common notification pipeline for three business flows:

- `performance`: weakest KII to a language-matched semantic video recommendation
- `engagement`: CLAN response-rate notification using the 60% rule
- `sentiment`: one Q&A-based workplace performance notification

## Local setup

1. Create and activate a virtual environment:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

2. Install requirements:

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

3. Create `.env` from `.env.example`:

```powershell
Copy-Item .env.example .env
```

4. Add the real `OPENAI_API_KEY`, `DATABASE_URL`, and notification provider URL
to `.env`. Never commit `.env`.

5. Start the API from this directory:

```powershell
uvicorn app.main:app --reload
```

Startup validates the database URL and LLM key. The active video recommender
uses the Sentence Transformers `all-MiniLM-L6-v2` model. Although embedding
provider and model settings are available, the active embedding function does
not currently use them.

## Run a notification

6. Request the next notification after the API starts:

```powershell
Invoke-RestMethod -Method Post `
	-Uri http://127.0.0.1:8000/notification/send `
	-ContentType "application/json" `
	-Body '{"user_id":953}'
```

7. Verify the response has `success` set to `true` and inspect the returned
`notification`. Its `reference_id` identifies the related item when applicable.

## Test

Run the test suite with:

```powershell
pytest
```

Most unit tests mock database and external-service calls. However,
`tests/test_performance.py` and
`tests/test_recommendation.py::test_user_language_id_maps_to_language_table`
query the database configured by `DATABASE_URL` directly. Run those tests
only with an isolated test database. Real end-to-end sends require a safe
test notification endpoint.

Read a user's performance result with the `user_id` query parameter:

```http
GET /notification/performance?user_id=953
```

The read-only flow endpoints are:

- `GET /notification/performance`
- `GET /notification/engagement`
- `GET /notification/sentiment`

Each endpoint accepts `user_id` as a query parameter. `POST /notification/send`
accepts a JSON body containing `user_id`; the notification type is selected
from the user's notification history.
