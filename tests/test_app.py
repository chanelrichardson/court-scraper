"""
Tests for the eviction canvasser backend.

API-calling tests are skipped automatically in CI when ANTHROPIC_API_KEY
is not set — they run fine locally once you've added your key to .env.
"""

import os
import io
import json
import pytest

# Set a dummy key before importing app so the startup check doesn't fail in CI
if not os.environ.get("ANTHROPIC_API_KEY"):
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-key-for-ci-only"

from app import app, extract_cases_from_pdf, LT_PATTERN


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ── Health check ──────────────────────────────────────────────────────────────

def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert "time" in data


def test_dockets_list(client):
    resp = client.get("/api/dockets")
    assert resp.status_code == 200
    locations = resp.get_json()
    assert "wabash" in locations
    assert "hargrove" in locations


# ── Case number regex ─────────────────────────────────────────────────────────

def test_lt_pattern_matches_valid():
    valid = [
        "D05LT26102566038",
        "D05LT26000000001",
        "d05lt26999999999",   # lowercase
    ]
    for cn in valid:
        assert LT_PATTERN.search(cn), f"Should match: {cn}"


def test_lt_pattern_rejects_invalid():
    invalid = [
        "D05CR26102566038",   # criminal, not LT
        "D06LT26102566038",   # wrong district
        "12345678",           # random number
    ]
    for cn in invalid:
        assert not LT_PATTERN.search(cn), f"Should NOT match: {cn}"


# ── PDF extraction ────────────────────────────────────────────────────────────

def test_extract_from_minimal_pdf():
    """
    Build a tiny valid PDF containing a known LT case number and verify
    extraction finds it without hitting the real mdcourts.gov server.
    """
    import pdfplumber

    # Minimal PDF with a case number embedded in a text stream
    # (real dockets are much larger but the regex works the same way)
    minimal_pdf = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 85>>
stream
BT /F1 12 Tf 50 700 Td (D05LT26102566038  SMITH, JOHN  06/10/2026  9:00 AM) Tj ET
endstream
endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000274 00000 n 
0000000411 00000 n 
trailer<</Size 6/Root 1 0 R>>
startxref
490
%%EOF"""

    cases = extract_cases_from_pdf(minimal_pdf)
    case_numbers = [c["case_number"] for c in cases]
    assert "D05LT26102566038" in case_numbers, f"Got: {case_numbers}"


# ── API endpoints (no external calls) ─────────────────────────────────────────

def test_extract_pdf_bad_location(client):
    resp = client.post("/api/extract-pdf",
                       data=json.dumps({"location": "nonexistent"}),
                       content_type="application/json")
    assert resp.status_code == 400


def test_upload_pdf_no_file(client):
    resp = client.post("/api/upload-pdf")
    assert resp.status_code == 400


def test_lookup_cases_empty(client):
    resp = client.post("/api/lookup-cases",
                       data=json.dumps({"case_numbers": [], "generate_walk_sheets": False}),
                       content_type="application/json")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_processed"] == 0


def test_generate_walk_sheet_no_body(client):
    resp = client.post("/api/generate-walk-sheet",
                       data=json.dumps({}),
                       content_type="application/json")
    # Empty body returns 400
    assert resp.status_code == 400


# ── Live API tests (skipped in CI if key is a dummy) ─────────────────────────

REAL_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SKIP_LIVE = not REAL_KEY or REAL_KEY.startswith("sk-ant-test")


@pytest.mark.skipif(SKIP_LIVE, reason="No real ANTHROPIC_API_KEY — skipping live API test")
def test_generate_walk_sheet_live(client):
    """Calls Claude for real — only runs when a real key is present."""
    case = {
        "case_number": "D05LT26102566038",
        "tenants": [{"name": "Jane Doe", "address": "123 Main St, Baltimore MD 21201"}],
        "landlords": [{"name": "Test Landlord LLC"}],
        "case_type": "Failure to Pay Rent",
        "hearing_date": "2026-06-15",
        "jurisdiction": "Baltimore City, MD",
        "status": "Pending",
    }
    resp = client.post("/api/generate-walk-sheet",
                       data=json.dumps(case),
                       content_type="application/json")
    assert resp.status_code == 200
    ws = resp.get_json()
    assert "opening" in ws
    assert "rights" in ws
    assert isinstance(ws["rights"], list)
    assert len(ws["rights"]) > 0
