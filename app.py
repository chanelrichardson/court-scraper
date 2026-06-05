"""
Eviction Canvasser Tool – Backend v3
- Accepts any Maryland District Court docket PDF (any case type, any district)
- Robust column detection that doesn't assume fixed layout
- Handles type codes bleeding into address column
- No AI, no MJCS API — all data from the PDF itself
"""

import os, re, io, json, logging
from datetime import datetime
import pdfplumber
from flask import Flask, request, jsonify
from flask_cors import CORS

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
_origins = os.environ.get("ALLOWED_ORIGINS", "*")
CORS(app, origins=[o.strip() for o in _origins.split(",")] if _origins != "*" else "*")

# ── Patterns ──────────────────────────────────────────────────────────────────

# Any Maryland District Court case number: D{district}{type}{year}{seq}
# District = 2 digits, type = 2 letters, year = 2 digits, seq = 6+ digits
CASE_PATTERN = re.compile(r'\bD\d{2}[A-Z]{2}\d{6,}\b', re.IGNORECASE)

# Case type codes used in MD dockets
CASE_TYPE_CODES = {
    'FPR':  'Failure to Pay Rent',
    'FFPR': 'Failure to Pay Rent',
    'BOL':  'Breach of Lease',
    'HOL':  'Holdover',
    'TEN':  'Tenancy',
    'DC':   'Summary Ejectment',
    'SUM':  'Summary Ejectment',
    'CV':   'Civil',
    'L/T':  'Landlord/Tenant',
    'LT':   'Landlord/Tenant',
    'TR':   'Traffic',
    'CR':   'Criminal',
    'PO':   'Peace Order',
}

# Type codes we consider eviction/landlord-tenant related
# (include all by default — canvassers can filter in the UI)
CIVIL_CODES = {'FPR','FFPR','BOL','HOL','TEN','DC','SUM','L/T','LT','CV'}

# Date patterns found in docket headers
DATE_PATTERNS = [
    re.compile(r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+(\w+\s+\d{1,2},?\s+\d{4})', re.IGNORECASE),
    re.compile(r'(\d{1,2}/\d{1,2}/\d{4})'),
    re.compile(r'Week\s+of\s+(\w+\s+\d{1,2},?\s+\d{4})', re.IGNORECASE),
]

# Looks like a time (09:00, 9:00 AM, etc.)
TIME_PAT = re.compile(r'^\d{1,2}:\d{2}(\s*[AP]M)?$', re.IGNORECASE)

# ── Static rights content ─────────────────────────────────────────────────────

RIGHTS = {
    'Failure to Pay Rent': [
        {'r': 'Right of Redemption',      'd': 'Pay ALL rent owed + court fees before or at the hearing to stop the eviction entirely. This can happen at the courthouse on hearing day. (Md. Real Property § 8-401)'},
        {'r': 'Right to Appear & Defend', 'd': 'Attend your hearing. Raise defenses: disputed amount, habitability issues, proof of payment, or landlord retaliation.'},
        {'r': '4-Day Buffer After Judgment','d': 'Even after a ruling against you, the landlord must wait 4 business days before scheduling the physical eviction.'},
        {'r': 'Emergency Rental Assistance','d': 'Call 211 now. A pending ERAP application can sometimes delay the hearing. Do not wait.'},
    ],
    'Breach of Lease': [
        {'r': 'Right to Cure',             'd': 'For some violations you may be able to fix the problem before the hearing. Ask the judge if a cure period applies.'},
        {'r': 'Right to Appear & Dispute', 'd': 'Attend your hearing. Dispute the landlord\'s characterization and present any evidence.'},
        {'r': 'Retaliation Defense',       'd': 'If you recently complained to housing authorities or organized with neighbors, this eviction may be illegal retaliation. (§ 8-208.1)'},
        {'r': '4-Day Buffer After Judgment','d': 'Landlord must wait 4 business days after judgment before scheduling the physical eviction.'},
    ],
    'Holdover': [
        {'r': 'Notice Requirements',       'd': 'Landlord must give written notice (usually 30 days for month-to-month) before filing a holdover case. Failure is a defense.'},
        {'r': 'Lease May Still Apply',     'd': 'A holdover tenant often retains the same rights as during the original lease. Check whether the lease term actually ended.'},
        {'r': 'Right to Appear & Dispute', 'd': 'Attend your hearing. Bring your lease, renewal communications, and rent payment records.'},
        {'r': '4-Day Buffer After Judgment','d': 'Landlord must wait 4 business days after judgment before scheduling the physical eviction.'},
    ],
    'Summary Ejectment': [
        {'r': 'Right to Appear',           'd': 'Always attend your hearing. Summary ejectment is faster than other eviction types — do not miss it.'},
        {'r': 'Right to Counsel',          'd': 'You have the right to be represented by an attorney. Call Maryland Legal Aid (410-539-5340) immediately.'},
        {'r': '4-Day Buffer After Judgment','d': 'Landlord must wait 4 business days after judgment before scheduling the physical eviction.'},
        {'r': 'Habitability Defense',      'd': 'Poor conditions (mold, no heat, pests) are a defense. Document everything with photos and 311 complaint records.'},
    ],
    'default': [
        {'r': 'Right to Appear',           'd': 'Always attend your hearing. Not showing up almost guarantees a judgment against you.'},
        {'r': 'Right to Defend',           'd': 'Raise defenses including habitability issues, landlord retaliation, or procedural errors.'},
        {'r': 'Rent Escrow',               'd': 'If your landlord is failing to maintain the property, you may pay rent into a court escrow account. (§ 8-211)'},
        {'r': '4-Day Buffer After Judgment','d': 'Landlord must wait 4 business days after judgment before scheduling the physical eviction.'},
    ],
}

RESOURCES = [
    {'n': 'Maryland Legal Aid',     'c': '410-539-5340',      'note': 'Free legal help — call immediately'},
    {'n': '211 Maryland',           'c': 'Dial 211',          'note': 'Emergency rental assistance referrals'},
    {'n': 'MD Court Help Center',   'c': '410-260-1392',      'note': 'Self-help for court proceedings'},
    {'n': 'Public Justice Center',  'c': '410-625-9409',      'note': 'At Wabash courthouse weekday mornings'},
    {'n': 'Baltimore City DSS',     'c': '443-378-4999',      'note': 'Eviction prevention funds'},
    {'n': 'DHCD Rental Assistance', 'c': 'dhcd.maryland.gov', 'note': 'State rental assistance program'},
]

OPENINGS = {
    'Failure to Pay Rent': "Hi, my name is {canvasser} and I'm a volunteer tenant rights canvasser. We noticed you have a court date coming up on {date} about unpaid rent, and wanted to make sure you know your rights and that there may be help available.",
    'Breach of Lease':     "Hi, my name is {canvasser} and I'm a volunteer tenant rights canvasser. We saw you have a court date on {date} for a lease issue, and wanted to share information about your rights before that hearing.",
    'Holdover':            "Hi, my name is {canvasser} and I'm a volunteer tenant rights canvasser. We noticed you have a holdover hearing on {date} and wanted to make sure you know what options and rights you have.",
    'default':             "Hi, my name is {canvasser} and I'm a volunteer tenant rights canvasser. We noticed you have an upcoming court date on {date} and wanted to share information about your rights as a tenant.",
}

TIPS = {
    'Failure to Pay Rent': "Ask if they've applied for 211/ERAP assistance — a pending application is powerful. Ask if any rent is disputed or if there are habitability issues.",
    'Breach of Lease':     "Ask what the alleged violation is. If they recently complained to 311 or housing, note that — retaliation is a strong defense.",
    'Holdover':            "Ask whether they received proper written notice and when. Check for a written lease or renewal. Improper notice is a strong legal defense.",
    'default':             "Be calm and non-judgmental. Encourage them to call Maryland Legal Aid immediately. Offer to help them write down questions for the judge.",
}

ACTIONS = {
    'Failure to Pay Rent': [
        'Call Maryland Legal Aid NOW at 410-539-5340 — do not wait until hearing day',
        'Call 211 to ask about emergency rental assistance — a pending application can help your case',
        'If you can pay the full amount owed + court fees before the hearing, do it — this stops the eviction entirely',
        'Attend your hearing — bring your lease, all rent receipts, and any written communications with your landlord',
    ],
    'Breach of Lease': [
        'Call Maryland Legal Aid NOW at 410-539-5340',
        'Write down exactly what the landlord claims you violated — gather any evidence that disputes it',
        'If you recently complained to 311 or housing authorities, collect those records — retaliation is a defense',
        'Attend your hearing — bring your lease and any communications with your landlord',
    ],
    'Holdover': [
        'Call Maryland Legal Aid NOW at 410-539-5340',
        'Find your lease and any renewal letters or emails — bring them to the hearing',
        'Check the date you received written notice from the landlord — improper notice is a defense',
        'Attend your hearing and tell the judge if you did not receive proper written notice',
    ],
    'default': [
        'Call Maryland Legal Aid NOW at 410-539-5340',
        'Call 211 to ask about emergency rental assistance',
        'Attend your hearing — bring your lease, rent receipts, and any relevant communications',
        'Ask the court clerk about free legal help available at the courthouse',
    ],
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def find_dates_in_text(text: str) -> list[tuple[int, str]]:
    """Return list of (position, date_string) for all date headers found in text."""
    found = []
    for pat in DATE_PATTERNS:
        for m in pat.finditer(text):
            found.append((m.start(), m.group(1)))
    found.sort(key=lambda x: x[0])
    return found


def fmt_name(raw: str) -> str:
    """LAST, FIRST MIDDLE → First Middle Last. Handles multiple names on separate lines."""
    if not raw:
        return ''
    names = []
    for part in str(raw).split('\n'):
        part = part.strip()
        if not part:
            continue
        if ',' in part:
            bits = [b.strip() for b in part.split(',', 1)]
            # bits[0] = last, bits[1] = first [middle]
            formatted = f"{bits[1].title()} {bits[0].title()}"
            names.append(formatted.strip())
        else:
            names.append(part.title())
    return ' & '.join(names)


def fmt_address(raw: str) -> str:
    """Clean and title-case an address that may have ZIP/state on a second line."""
    if not raw:
        return ''
    # Strip any trailing type codes that bled in
    cleaned = re.sub(r'\b[A-Z]{2,5}\b$', '', str(raw)).strip()
    parts = [p.strip().title() for p in cleaned.split('\n') if p.strip()]
    return ', '.join(parts)


def extract_type_code(cell: str) -> tuple[str, str]:
    """
    Pull a case type code out of a cell that may have other content.
    Returns (type_code, cell_without_type).
    """
    cell = str(cell or '')
    # Look for known codes at end of string (they often bleed in from adjacent column)
    for code in sorted(CASE_TYPE_CODES.keys(), key=len, reverse=True):
        pattern = re.compile(re.escape(code) + r'\s*$', re.IGNORECASE)
        if pattern.search(cell):
            cleaned = pattern.sub('', cell).strip()
            return code.upper(), cleaned
    # Also check standalone in its own cell
    stripped = cell.strip().upper()
    if stripped in CASE_TYPE_CODES:
        return stripped, ''
    return '', cell


def infer_case_type_from_case_number(case_num: str) -> str:
    """Use the 2-letter type code embedded in the case number as a fallback."""
    m = re.match(r'D\d{2}([A-Z]{2})\d+', case_num, re.IGNORECASE)
    if m:
        code = m.group(1).upper()
        if code == 'LT':
            return 'Landlord/Tenant'
        if code == 'DC':
            return 'Summary Ejectment'
        if code == 'CV':
            return 'Civil'
        if code == 'CR':
            return 'Criminal'
        if code == 'TR':
            return 'Traffic'
    return 'Landlord/Tenant'


def urgency(date_str: str) -> str:
    if not date_str:
        return 'medium'
    try:
        from dateutil import parser as dp
        d = dp.parse(date_str, default=datetime.today())
        days = (d.date() - datetime.today().date()).days
        return 'high' if days <= 3 else 'medium' if days <= 7 else 'low'
    except Exception:
        return 'medium'


def make_walk_sheet(case_type: str, hearing_date: str) -> dict:
    rights   = RIGHTS.get(case_type, RIGHTS['default'])
    actions  = ACTIONS.get(case_type, ACTIONS['default'])
    opening  = OPENINGS.get(case_type, OPENINGS['default'])
    tip      = TIPS.get(case_type, TIPS['default'])
    return {
        'opening_template': opening,
        'rights':           rights,
        'actions':          actions,
        'resources':        RESOURCES,
        'canvasser_tip':    tip,
        'urgency':          urgency(hearing_date),
        'generated_at':     datetime.utcnow().isoformat(),
    }


# ── PDF Parser ────────────────────────────────────────────────────────────────

def extract_cases_from_pdf(pdf_bytes: bytes) -> list[dict]:
    """
    Parse any Maryland District Court docket PDF.
    Strategy:
      1. For each page, find all tables
      2. Detect column positions by scanning for the case number column
      3. Extract all other fields relative to the case number column position
      4. Also sweep raw text as a fallback for rows tables missed
    """
    cases = []
    seen  = set()

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_num, page in enumerate(pdf.pages):
            full_text = page.extract_text() or ''
            tables    = page.extract_tables() or []

            # Find date headers in full page text
            date_markers = find_dates_in_text(full_text)
            current_date = date_markers[0][1] if date_markers else ''

            log.debug(f"Page {page_num+1}: {len(tables)} tables, date={current_date}")

            for table in tables:
                if not table:
                    continue

                # Detect if first row is a header by checking for keyword columns
                first_row = [str(c or '').lower().strip() for c in (table[0] or [])]
                is_header = any(kw in ' '.join(first_row) for kw in
                                ['case', 'plaintiff', 'defendant', 'time', 'type', 'charge'])

                # Find which column index holds the case number by scanning rows
                case_col = None
                for row in table:
                    for ci, cell in enumerate(row or []):
                        if CASE_PATTERN.search(str(cell or '')):
                            case_col = ci
                            break
                    if case_col is not None:
                        break

                if case_col is None:
                    # No case numbers in this table — skip
                    continue

                # Determine column roles relative to case_col
                # Typical layout: [time, case#, plaintiff, defendant, address, type]
                # case_col tells us where we are; derive the rest
                ncols = max(len(r) for r in table if r)
                col_time      = case_col - 1 if case_col > 0 else None
                col_plaintiff = case_col + 1 if case_col + 1 < ncols else None
                col_defendant = case_col + 2 if case_col + 2 < ncols else None
                col_address   = case_col + 3 if case_col + 3 < ncols else None
                col_type      = case_col + 4 if case_col + 4 < ncols else None

                # If header row told us column names, use those instead
                if is_header:
                    for ci, h in enumerate(first_row):
                        if 'time' in h:                        col_time      = ci
                        elif 'case' in h:                      pass  # already know
                        elif 'plain' in h or 'landlord' in h:  col_plaintiff = ci
                        elif 'defend' in h or 'tenant' in h:   col_defendant = ci
                        elif 'addr' in h:                      col_address   = ci
                        elif 'type' in h or 'charge' in h:     col_type      = ci

                start_row = 1 if is_header else 0

                for row in table[start_row:]:
                    if not row:
                        continue

                    row_text = ' '.join(str(c or '') for c in row).strip()

                    # Date-only row — update current date
                    for pat in DATE_PATTERNS:
                        dm = pat.search(row_text)
                        if dm and sum(1 for c in row if str(c or '').strip()) <= 2:
                            current_date = dm.group(1)
                            break

                    # Find case number
                    case_match = CASE_PATTERN.search(str(row[case_col] or ''))
                    if not case_match:
                        # Maybe it shifted — scan all cells
                        for cell in row:
                            case_match = CASE_PATTERN.search(str(cell or ''))
                            if case_match:
                                break
                    if not case_match:
                        continue

                    case_num = case_match.group().upper()
                    if case_num in seen:
                        continue
                    seen.add(case_num)

                    def sg(idx):
                        if idx is None or idx < 0 or idx >= len(row):
                            return ''
                        return str(row[idx] or '').strip()

                    raw_time      = sg(col_time)
                    raw_plaintiff = sg(col_plaintiff)
                    raw_defendant = sg(col_defendant)
                    raw_address   = sg(col_address)
                    raw_type_cell = sg(col_type)

                    # Type code may be in its own column or bleeding into address
                    type_code, _ = extract_type_code(raw_type_cell)
                    if not type_code:
                        type_code, raw_address = extract_type_code(raw_address)

                    # Map type code to readable name; fall back to case number embedded code
                    if type_code:
                        case_type = CASE_TYPE_CODES.get(type_code, type_code)
                    else:
                        case_type = infer_case_type_from_case_number(case_num)

                    cases.append({
                        'case_number':  case_num,
                        'hearing_date': current_date,
                        'hearing_time': raw_time if TIME_PAT.match(raw_time) else '',
                        'tenant':       fmt_name(raw_defendant),
                        'landlord':     fmt_name(raw_plaintiff),
                        'address':      fmt_address(raw_address),
                        'case_type':    case_type,
                        'type_code':    type_code,
                        'jurisdiction': 'Baltimore City, MD',
                        'court':        'District Court of Maryland – Baltimore City',
                        'walk_sheet':   make_walk_sheet(case_type, current_date),
                    })

            # Fallback: sweep raw text for any case numbers tables missed
            for m in CASE_PATTERN.finditer(full_text):
                cn = m.group().upper()
                if cn in seen:
                    continue
                seen.add(cn)
                # Try to find a date in the nearby text
                snippet = full_text[max(0, m.start()-300):m.end()+300]
                nearby_date = current_date
                for pat in DATE_PATTERNS:
                    dm = pat.search(snippet)
                    if dm:
                        nearby_date = dm.group(1)
                        break
                ct = infer_case_type_from_case_number(cn)
                cases.append({
                    'case_number':  cn,
                    'hearing_date': nearby_date,
                    'hearing_time': '',
                    'tenant':       '',
                    'landlord':     '',
                    'address':      '',
                    'case_type':    ct,
                    'type_code':    '',
                    'jurisdiction': 'Baltimore City, MD',
                    'court':        'District Court of Maryland – Baltimore City',
                    'walk_sheet':   make_walk_sheet(ct, nearby_date),
                    'source':       'text_fallback',
                })

    log.info(f"Extracted {len(cases)} cases from PDF")
    return cases


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/api/health')
def health():
    return jsonify({'status': 'ok', 'time': datetime.utcnow().isoformat(), 'version': '3.0'})


@app.route('/api/upload-pdf', methods=['POST'])
def upload_pdf():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    f = request.files['file']
    pdf_bytes = f.read()

    if len(pdf_bytes) < 200:
        return jsonify({'error': 'File too small — is this a valid PDF?'}), 400

    try:
        cases = extract_cases_from_pdf(pdf_bytes)
    except Exception as e:
        log.exception('PDF parse error')
        return jsonify({'error': f'Failed to parse PDF: {e}'}), 500

    if not cases:
        return jsonify({
            'error': 'No court case numbers found in this PDF.',
            'hint': (
                'The parser looks for Maryland case numbers like D05LT26XXXXXXXXX, D05DC26XXXXXX, etc. '
                'Make sure this is a Baltimore City District Court docket PDF. '
                'If the PDF uses a different format, please share a sample case number so we can update the pattern.'
            ),
        }), 404

    today = datetime.today().strftime('%Y-%m-%d')

    # Group by case type for summary
    by_type = {}
    for c in cases:
        by_type[c['case_type']] = by_type.get(c['case_type'], 0) + 1

    return jsonify({
        'filename':    f.filename,
        'total_cases': len(cases),
        'by_type':     by_type,
        'cost_usd':    0.00,
        'cases':       cases,
    })


@app.route('/api/add-case', methods=['POST'])
def add_case():
    body = request.get_json() or {}
    if not body.get('tenant') or not body.get('address') or not body.get('hearing_date'):
        return jsonify({'error': 'tenant, address, and hearing_date are required'}), 400

    case_type = body.get('case_type', 'Failure to Pay Rent')
    return jsonify({
        'case_number':  body.get('case_number', f'MANUAL-{int(datetime.utcnow().timestamp())}'),
        'hearing_date': body.get('hearing_date', ''),
        'hearing_time': body.get('hearing_time', ''),
        'tenant':       body.get('tenant', ''),
        'landlord':     body.get('landlord', ''),
        'address':      body.get('address', ''),
        'case_type':    case_type,
        'jurisdiction': body.get('jurisdiction', 'Baltimore City, MD'),
        'court':        'District Court of Maryland – Baltimore City',
        'walk_sheet':   make_walk_sheet(case_type, body.get('hearing_date', '')),
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5050))
    log.info(f'Eviction Canvasser v3 on port {port} — $0.00/run')
    app.run(debug=True, host='0.0.0.0', port=port, threaded=True)
