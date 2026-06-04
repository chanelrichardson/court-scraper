"""
Eviction Canvasser Tool – Backend
Workflow:
1. Download docket PDF from mdcourts.gov
2. Extract text, find LT case numbers (pattern D05LT...)
3. For each case number, hit the MJCS public case-details API
4. Feed structured data to Claude to generate walk sheets
5. Serve everything via Flask REST API
"""

import os, re, time, json, io, logging
from datetime import datetime
import requests
import pdfplumber
from flask import Flask, request, jsonify
from flask_cors import CORS
import anthropic

# Load .env when running locally; in production (Railway/Render/GitHub Actions)
# the real environment variables take precedence automatically.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed — that's fine in production

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

# In production set ALLOWED_ORIGINS="https://yourorg.github.io,https://yourapp.com"
_origins = os.environ.get("ALLOWED_ORIGINS", "*")
allowed_origins = [o.strip() for o in _origins.split(",")] if _origins != "*" else "*"
CORS(app, origins=allowed_origins)

# ── Constants ────────────────────────────────────────────────────────────────

# All four Baltimore City District Court docket PDFs (different courthouses)
DOCKET_URLS = {
    "wabash":   "https://www.mdcourts.gov/sites/default/files/import/district/directories/Dockets/baltimorecitywabashavedcdocket.pdf",
    "hargrove": "https://www.mdcourts.gov/sites/default/files/import/district/directories/Dockets/baltimorecityhargrovedcdocket.pdf",
    "eastside": "https://www.mdcourts.gov/sites/default/files/import/district/directories/Dockets/baltimorecityeastsidedcdocket.pdf",
    "hubbard":  "https://www.mdcourts.gov/sites/default/files/import/district/directories/Dockets/baltimorecitynorthcalvertdcdocket.pdf",
}

MJCS_API = "https://casesearch.courts.state.md.us/api-casedetails/v1/public/cases/{case_id}"

# Landlord-tenant case number patterns used by Baltimore City District Court
# Format: D05LT{YY}{digits}  e.g. D05LT26102566038
LT_PATTERN = re.compile(r'\bD05LT\d{11,14}\b', re.IGNORECASE)

# Also catch the short format that appears in some PDFs: 26-XXXXXX
SHORT_LT_PATTERN = re.compile(r'\b2[0-9]-\s*\d{6,}\b')

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TenantRightsCanvasser/1.0; civic-use)",
    "Accept": "application/json, */*",
}

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# Fail loudly at startup if the key is missing — better than a confusing 401 later
if not os.environ.get("ANTHROPIC_API_KEY"):
    raise RuntimeError(
        "ANTHROPIC_API_KEY is not set.\n"
        "  • Locally: copy .env.example → .env and add your key\n"
        "  • GitHub Actions / Railway / Render: add it as a repository/environment secret"
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def download_pdf(url: str) -> bytes | None:
    """Download a PDF, return raw bytes or None on failure."""
    try:
        r = requests.get(url, headers={**HEADERS, "Accept": "application/pdf"},
                         timeout=20, allow_redirects=True)
        r.raise_for_status()
        if len(r.content) < 500:
            log.warning(f"PDF too small ({len(r.content)} bytes) – likely blocked: {url}")
            return None
        return r.content
    except Exception as e:
        log.error(f"PDF download failed {url}: {e}")
        return None


def extract_cases_from_pdf(pdf_bytes: bytes) -> list[dict]:
    """
    Parse the docket PDF.
    The dockets are tabular: date | time | case# | party names | type
    We extract rows, identify LT (landlord-tenant) cases, and return structured dicts.
    """
    cases = []
    seen = set()

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        full_text = ""
        all_tables = []
        for page in pdf.pages:
            full_text += page.extract_text() or ""
            tables = page.extract_tables()
            for tbl in (tables or []):
                all_tables.extend(tbl)

        # Strategy 1: Parse tables (structured)
        current_date = None
        for row in all_tables:
            if not row:
                continue
            # Detect date rows (often a merged cell with just a date)
            row_text = " ".join(str(c or "") for c in row).strip()
            date_match = re.search(r'\b(\d{1,2}/\d{1,2}/\d{4})\b', row_text)
            if date_match and len([c for c in row if c]) <= 2:
                current_date = date_match.group(1)
                continue

            # Find case number in row
            case_num = None
            for cell in row:
                cell_str = str(cell or "").strip()
                lt = LT_PATTERN.search(cell_str)
                if lt:
                    case_num = lt.group().upper()
                    break
                # Short format fallback
                short = SHORT_LT_PATTERN.search(cell_str)
                if short:
                    # normalize to attempt API lookup format
                    raw = short.group().replace(" ", "").replace("-", "")
                    case_num = f"D05LT{raw}"
                    break

            if case_num and case_num not in seen:
                seen.add(case_num)
                # Extract names and other info from remaining cells
                cells = [str(c or "").strip() for c in row if c and str(c).strip()]
                case = {
                    "case_number": case_num,
                    "hearing_date": current_date or "",
                    "raw_row": cells,
                }
                # Heuristic: first long text cell after case# is probably party names
                for cell in cells:
                    if cell != case_num and len(cell) > 5 and not re.match(r'\d+:\d+', cell):
                        if "defendant" not in cell.lower() and "plaintiff" not in cell.lower():
                            case["raw_parties"] = cell
                            break
                cases.append(case)

        # Strategy 2: Regex over full text (catches anything tables missed)
        for m in LT_PATTERN.finditer(full_text):
            cn = m.group().upper()
            if cn not in seen:
                seen.add(cn)
                # Try to find a date nearby (within 200 chars before)
                snippet = full_text[max(0, m.start()-200):m.end()+200]
                d = re.search(r'\b(\d{1,2}/\d{1,2}/\d{4})\b', snippet)
                cases.append({
                    "case_number": cn,
                    "hearing_date": d.group(1) if d else "",
                    "raw_row": [],
                    "context_snippet": snippet[:300],
                })

    log.info(f"Extracted {len(cases)} LT cases from PDF")
    return cases


def fetch_case_details(case_id: str, delay: float = 1.2) -> dict | None:
    """
    Hit the MJCS public API for one case.
    Returns the JSON dict or None if blocked/missing.
    Respects a delay between calls to be a good citizen.
    """
    url = MJCS_API.format(case_id=case_id)
    try:
        time.sleep(delay)
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 429:
            log.warning(f"Rate limited on {case_id}, waiting 10s")
            time.sleep(10)
            r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 404:
            log.info(f"Case not found: {case_id}")
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"MJCS API failed for {case_id}: {e}")
        return None


def parse_mjcs_response(data: dict, docket_case: dict) -> dict:
    """
    Normalize the MJCS JSON into a clean case dict.
    The MJCS response structure has partyType, party name, address, hearingDate etc.
    """
    if not data:
        return {**docket_case, "api_status": "not_found"}

    # Extract parties
    parties = data.get("parties", []) or []
    defendants = [p for p in parties if "defendant" in str(p.get("partyType", "")).lower()
                  or "tenant" in str(p.get("partyType", "")).lower()]
    plaintiffs = [p for p in parties if "plaintiff" in str(p.get("partyType", "")).lower()
                  or "landlord" in str(p.get("partyType", "")).lower()]

    def fmt_party(p):
        name = " ".join(filter(None, [p.get("firstName",""), p.get("middleName",""), p.get("lastName","")])).strip()
        if not name:
            name = p.get("businessName") or p.get("name") or ""
        addr_parts = filter(None, [
            p.get("address1") or p.get("addressLine1"),
            p.get("address2") or p.get("addressLine2"),
            p.get("city"), p.get("state"), p.get("zip") or p.get("zipCode"),
        ])
        address = ", ".join(addr_parts)
        return {"name": name, "address": address}

    tenant_info  = [fmt_party(p) for p in defendants] if defendants else []
    landlord_info = [fmt_party(p) for p in plaintiffs] if plaintiffs else []

    # Hearing date
    hearings = data.get("hearings", []) or data.get("events", []) or []
    hearing_date = docket_case.get("hearing_date") or ""
    if hearings:
        future = [h for h in hearings if h.get("date", "") >= datetime.today().strftime("%Y-%m-%d")]
        if future:
            hearing_date = future[0].get("date", hearing_date)
        elif hearings:
            hearing_date = hearings[-1].get("date", hearing_date)

    case_type = data.get("caseType") or data.get("caseCategory") or "Failure to Pay Rent"
    status = data.get("status") or data.get("caseStatus") or ""

    return {
        "case_number": docket_case["case_number"],
        "case_type": case_type,
        "status": status,
        "hearing_date": hearing_date,
        "tenants": tenant_info,
        "landlords": landlord_info,
        "jurisdiction": "Baltimore City, MD",
        "court": "District Court of Maryland – Baltimore City",
        "api_status": "found",
        "raw_api": data,
    }


def generate_walk_sheet_claude(case: dict) -> dict:
    """
    Call Claude to generate structured walk sheet content.
    Designed to be cheap: ~400 input tokens, ~600 output tokens ≈ $0.003 per case on Sonnet.
    For 60 cases/run that's ~$0.18 — fits the $0.20 budget.
    """
    tenant_names = ", ".join(t["name"] for t in case.get("tenants", []) if t["name"]) or "Tenant on record"
    tenant_address = next((t["address"] for t in case.get("tenants", []) if t.get("address")), "Address in case file")
    landlord_name  = next((l["name"] for l in case.get("landlords", []) if l.get("name")), "See case file")
    is_dc = "DC" in case.get("jurisdiction", "")
    jx = "Washington DC" if is_dc else "Baltimore City / Maryland"

    prompt = f"""You are a tenant rights canvasser assistant. Generate a concise walk sheet for this Baltimore eviction case.

Case: {case['case_number']} | Type: {case.get('case_type','Failure to Pay Rent')} | Hearing: {case.get('hearing_date','TBD')}
Tenant(s): {tenant_names} | Address: {tenant_address} | Landlord: {landlord_name}
Status: {case.get('status','')}

Respond ONLY with compact JSON (no markdown, no explanation):
{{"opening":"2 sentence door-knock script","rights":[{{"r":"right name","d":"1 sentence plain English"}}],"actions":["action 1","action 2","action 3"],"resources":[{{"n":"name","c":"phone/web"}}],"tip":"1 canvasser tip","urgency":"high|medium|low"}}

Rules: rights must be specific to {jx} law. 4 rights max. 3 resources max. Keep each field under 120 chars."""

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text.strip()
        # strip any accidental markdown
        text = re.sub(r'^```(?:json)?|```$', '', text, flags=re.MULTILINE).strip()
        ws = json.loads(text)
        ws["generated_at"] = datetime.utcnow().isoformat()
        ws["case"] = case
        return ws
    except Exception as e:
        log.error(f"Claude generation failed for {case['case_number']}: {e}")
        return {
            "error": str(e),
            "case": case,
            "opening": "Hi, I'm a tenant rights canvasser. We noticed you have an upcoming court date and wanted to share information about your rights.",
            "rights": [{"r": "Right to appear", "d": "You have the right to appear and defend yourself in court."}],
            "actions": ["Attend your hearing on the scheduled date", "Call Maryland Legal Aid at 410-539-5340", "Call 211 for emergency rental assistance"],
            "resources": [{"n": "Maryland Legal Aid", "c": "410-539-5340"}, {"n": "211 Maryland", "c": "211"}],
            "tip": "Be calm and introduce yourself clearly. Offer the resource sheet.",
            "urgency": "high",
        }


# ── API Routes ────────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})


@app.route("/api/dockets")
def list_dockets():
    """Return available docket locations."""
    return jsonify(list(DOCKET_URLS.keys()))


@app.route("/api/extract-pdf", methods=["POST"])
def extract_pdf():
    """
    Download and parse a docket PDF.
    Body: {"location": "wabash"} or {"url": "https://..."}
    Returns list of raw case stubs (case numbers + hearing dates from PDF).
    Does NOT hit MJCS yet.
    """
    body = request.get_json() or {}
    loc = body.get("location", "wabash")
    url = body.get("url") or DOCKET_URLS.get(loc)
    if not url:
        return jsonify({"error": f"Unknown location: {loc}"}), 400

    pdf_bytes = download_pdf(url)
    if not pdf_bytes:
        return jsonify({"error": "Could not download PDF — mdcourts.gov may be blocking automated requests. Try uploading the PDF manually."}), 502

    cases = extract_cases_from_pdf(pdf_bytes)
    lt_cases = [c for c in cases]  # all are LT by pattern match
    return jsonify({
        "location": loc,
        "url": url,
        "total_found": len(lt_cases),
        "cases": lt_cases,
    })


@app.route("/api/upload-pdf", methods=["POST"])
def upload_pdf():
    """
    Accept a manually uploaded docket PDF (multipart/form-data, field 'file').
    Extracts case stubs without needing to hit mdcourts.gov.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    pdf_bytes = f.read()
    if len(pdf_bytes) < 100:
        return jsonify({"error": "File too small"}), 400

    cases = extract_cases_from_pdf(pdf_bytes)
    return jsonify({
        "filename": f.filename,
        "total_found": len(cases),
        "cases": cases,
    })


@app.route("/api/lookup-cases", methods=["POST"])
def lookup_cases():
    """
    Given a list of case numbers, fetch details from MJCS and generate walk sheets.
    Body: {"case_numbers": ["D05LT26102566038", ...], "generate_walk_sheets": true}
    Respects rate limits. Returns enriched case data.
    """
    body = request.get_json() or {}
    case_numbers = body.get("case_numbers", [])
    generate_ws  = body.get("generate_walk_sheets", True)
    max_cases    = min(len(case_numbers), 60)  # hard cap for budget

    results = []
    for i, cn in enumerate(case_numbers[:max_cases]):
        log.info(f"[{i+1}/{max_cases}] Fetching {cn}")
        raw = fetch_case_details(cn)
        # Build a stub for the docket entry
        docket_stub = {"case_number": cn, "hearing_date": ""}
        case = parse_mjcs_response(raw, docket_stub)

        if generate_ws:
            case["walk_sheet"] = generate_walk_sheet_claude(case)

        results.append(case)

    return jsonify({
        "total_requested": len(case_numbers),
        "total_processed": len(results),
        "cases": results,
    })


@app.route("/api/full-run", methods=["POST"])
def full_run():
    """
    One-shot: download PDF → extract case numbers → MJCS lookup → walk sheets.
    Body: {"location": "wabash", "limit": 20}
    """
    body = request.get_json() or {}
    loc   = body.get("location", "wabash")
    limit = min(int(body.get("limit", 20)), 60)
    url   = body.get("url") or DOCKET_URLS.get(loc)

    if not url:
        return jsonify({"error": f"Unknown location: {loc}"}), 400

    # Step 1: PDF
    log.info(f"Full run: downloading {url}")
    pdf_bytes = download_pdf(url)
    if not pdf_bytes:
        return jsonify({
            "error": "PDF download blocked by mdcourts.gov. Please download the PDF manually and use /api/upload-pdf instead.",
            "manual_url": url,
            "workaround": "Download the PDF from mdcourts.gov in your browser, then upload it using the Upload PDF button."
        }), 502

    # Step 2: Extract
    docket_cases = extract_cases_from_pdf(pdf_bytes)
    if not docket_cases:
        return jsonify({"error": "No LT case numbers found in PDF", "pdf_size": len(pdf_bytes)}), 404

    # Step 3: MJCS lookup + walk sheets
    results = []
    for i, dc in enumerate(docket_cases[:limit]):
        cn = dc["case_number"]
        log.info(f"[{i+1}/{min(len(docket_cases), limit)}] Fetching {cn}")
        raw  = fetch_case_details(cn)
        case = parse_mjcs_response(raw, dc)
        case["walk_sheet"] = generate_walk_sheet_claude(case)
        results.append(case)

    return jsonify({
        "location": loc,
        "docket_url": url,
        "total_in_pdf": len(docket_cases),
        "total_processed": len(results),
        "cases": results,
        "estimated_cost_usd": round(len(results) * 0.003, 3),
    })


@app.route("/api/generate-walk-sheet", methods=["POST"])
def single_walk_sheet():
    """Generate a walk sheet for a manually entered case (no PDF/MJCS needed)."""
    case = request.get_json() or {}
    if not case:
        return jsonify({"error": "No case data"}), 400
    ws = generate_walk_sheet_claude(case)
    return jsonify(ws)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(debug=True, host="0.0.0.0", port=port, threaded=True)
