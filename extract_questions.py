"""
PDF Question Extractor
======================
Extracts individual exam-question SUB-PARTS from a scanned PDF (one
image file per sub-part, e.g. 1(a), 1(b), 1(c), …).

How it works
------------
1.  Render every PDF page to a high-DPI bitmap with PyMuPDF.
2.  Run Tesseract OCR on each page to find:
      * question markers — short tokens like "1.", "2.", "5." at the
        page's left margin.  OCR mis-reads such as ``i``/``l``/``|``
        for ``1`` and ``2:`` for ``2.`` are handled.
      * sub-part markers — tokens like "(a)", "(b)", "(c)" sitting
        slightly indented from the question markers.  Roman-numeral
        sub-sub-parts "(i)", "(ii)", "(iii)" are skipped.
3.  Sort everything in reading order (page, y).  For each question:
      * if it has no sub-parts → save the whole question as one image,
      * else → save one image per sub-part.  The first sub-part keeps
        the question heading ("1.") visible at the top so the image is
        self-describing.
4.  Multi-page sub-parts are stitched automatically.

Edit the CONFIG block below, then run:    python extract_questions.py
"""

import argparse
import io
import re
import sys
from pathlib import Path

import fitz  # PyMuPDF
import pytesseract
from PIL import Image

# Use UTF-8 for console output (Windows console defaults to cp1252).
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG  —  edit these before running
# ─────────────────────────────────────────────────────────────────────────────

# Input PDF (the scanned question paper).
PDF_PATH = r""

# Where the cropped images will be written.
# >>> THIS IS THE LINE YOU'LL TYPICALLY WANT TO CHANGE <<<
OUTPUT_DIR = r""

# Full path to the tesseract executable. Set to None to rely on PATH.
TESSERACT_CMD = r""

# ── Rendering ───────────────────────────────────────────────────────────────
DPI = 220              # raise for sharper output, lower for speed

# ── Sub-part splitting ──────────────────────────────────────────────────────
# When True, each question is split into one image per (a)/(b)/(c)/... part.
# When False, every question is saved as a single image (legacy behaviour).
SPLIT_SUB_PARTS = True

# All geometric thresholds are FRACTIONS of the rendered page's width/
# height, computed fresh on every page.  This matters because a single PDF
# can mix scans of very different pixel sizes (e.g. 1500-px-wide pages and
# 3300-px-wide pages) and a fixed-pixel threshold that fits one will miss
# the other entirely.

# ── Question-marker detection ──────────────────────────────────────────────
LEFT_MARGIN_MIN_FRAC   = 0.05    # marker x must be >= this fraction of width
LEFT_MARGIN_MAX_FRAC   = 0.14    # marker x must be <= this fraction of width
MIN_MARKER_HEIGHT_FRAC = 0.0055  # ignores small dots and noise
MAX_MARKER_HEIGHT_FRAC = 0.025   # ignores headings / huge characters
MIN_MARKER_WIDTH_FRAC  = 0.005   # kills 1-2 px wide table-grid '|' lines
MIN_CONFIDENCE       = 20        # OCR-confidence floor (strict-tier markers)
LOOSE_MIN_CONFIDENCE = 60        # higher floor for bare digits w/o
                                 #   punctuation — they're far more often
                                 #   equations/numbers in body text than real
                                 #   question numbers.
TOP_FRACTION         = 0.045     # ignore the top 4.5% of each page (headers)
BOTTOM_FRACTION      = 0.955     # ignore the bottom 4.5% (footers/page nrs)
MAX_QUESTION_NUMBER  = 15        # reject "Q-marker" parsed as a bigger number
                                 #   (real exams almost never go past Q10).

# ── Sub-part-marker detection ──────────────────────────────────────────────
SUBPART_MIN_FRAC       = 0.11    # sub-parts sit slightly indented from
SUBPART_MAX_FRAC       = 0.25    #   question numbers
SUBPART_MIN_WIDTH_FRAC = 0.008   # "(a)" is wider than a single character
SUBPART_MIN_CONF       = 30
# Allowed sub-part letters.  Restricted to (a)-(h): real exam questions
# never go past (g) or (h), and letters past (h) are almost always math
# variables — (s) from "G(s)", (k) from gains, (n) from indices, etc.
SUBPART_LETTERS    = "abcdefgh"
# Minimum vertical gap between consecutive sub-parts of the same question,
# as a fraction of page height.  Anything closer is treated as OCR
# duplicating a marker on the same line of content.
MIN_SUBPART_GAP_FRAC   = 0.020
# Sub-parts on the SAME LINE as a question marker (often (a)) are accepted
# even if their y is a little above the question marker — this is the
# slack, as a fraction of page height.
SAME_LINE_TOL_FRAC     = 0.013

# ── Cropping ────────────────────────────────────────────────────────────────
PAGE_TOP_PAD          = 12   # px of whitespace kept above each cropped region
PAGE_BOTTOM_PAD       = 12   # px of whitespace kept below each cropped region
MAX_QUESTION_PAGES    = 2    # cap each question to this many pages
                             #   (protects against missed-marker / paper-break
                             #   over-stretches).  Raise if your questions are
                             #   genuinely longer.

# Sub-part cropping that crosses into a new page is risky — the new page
# might start with a section header / paper break instead of a real
# continuation.  Only cross when the next sub-part sits within this
# fraction of the new page's top.  Set to 1.0 to always allow crossing.
SUBPART_PAGE_CROSS_LIMIT_FRAC = 0.115

# ── Pages to ignore entirely (1-indexed) ────────────────────────────────────
SKIP_PAGES: list[int] = []

# ── Output ──────────────────────────────────────────────────────────────────
OUTPUT_FORMAT    = "PNG"     # "PNG" or "JPEG"
# Placeholders:
#   {seq}    = global detection index across all saved images
#   {year}   = academic-year tag from the paper header ("2022-23"), or
#              "unknown" if no header has been seen yet
#   {page}   = page where the image starts (1-indexed)
#   {num}    = question number Tesseract thought it saw
#   {part}   = sub-part letter ('a', 'b', …) or '' for whole questions
FILENAME_PATTERN = "q_{seq:03d}_y{year}_p{page:02d}_n{num}{part}.png"

# Pattern that recognises an academic-year string ("2022-2023", "2022-23",
# "2022 - 2023" …) in the page header.  Used to tag every saved image.
YEAR_RE = re.compile(r"(20\d{2})\s*[-–—/]\s*(20\d{2}|\d{2})")
# Fraction of page height searched for the year string (top of page).
YEAR_SEARCH_TOP_FRAC = 0.15
UNKNOWN_YEAR_TAG = "unknown"

# ─────────────────────────────────────────────────────────────────────────────


# Question-marker patterns
RE_STRICT  = re.compile(r"^(\d{1,2})\s*[\.\,\:\;]$")     # "1.", "12.", "2:"
RE_LOOSE   = re.compile(r"^(\d{1,2})$")                  # bare "1", "12"
RE_ONE_FIX = re.compile(r"^[il|I!]\s*[\.\,\:\;]?$")      # i, l, |, …
# Combined "Q + first sub-part" patterns: Tesseract often glues the marker
# and the first sub-part into a single token, especially when there's no
# space between them on the page.  Examples accepted: "6.(a)", "5(a)",
# "7. (b)".  We REQUIRE a paren before the letter so we don't accept
# random body tokens like "1a)" or "12b)".
RE_COMBO   = re.compile(
    r"^(\d{1,2})\s*[\.\,\:\;]?\s*\(\s*([a-z])\s*\)\s*$"
)

# Sub-part patterns — explicit roman skip-list
ROMAN_SKIP = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}


def parse_marker(text: str) -> tuple[int, str] | None:
    """Return (question_number, tier) or None."""
    t = text.strip()
    if not t:
        return None
    if (m := RE_STRICT.match(t)):
        n = int(m.group(1))
        if 1 <= n <= MAX_QUESTION_NUMBER:
            return n, "strict"
    if (m := RE_LOOSE.match(t)):
        n = int(m.group(1))
        if 1 <= n <= MAX_QUESTION_NUMBER:
            return n, "loose"
    if RE_ONE_FIX.match(t):
        return 1, "fix1"
    return None


def parse_combo(text: str) -> tuple[int, str] | None:
    """Match tokens like ``6.(a)`` or ``5(a)`` — these turn up when OCR
    glues a question marker and its first sub-part together.  Returns
    ``(question_number, sub_part_letter)`` so the caller can emit both
    a question event and a sub-part event."""
    t = text.strip()
    if not t:
        return None
    m = RE_COMBO.match(t)
    if not m:
        return None
    n = int(m.group(1))
    letter = m.group(2).lower()
    if not (1 <= n <= MAX_QUESTION_NUMBER):
        return None
    if letter not in SUBPART_LETTERS:
        return None
    return n, letter


def parse_subpart(text: str) -> str | None:
    """Return the lowercase letter for a sub-part marker like "(a)", or None.

    Restricts to letters in ``SUBPART_LETTERS`` so that equation symbols
    like ``(s)`` in ``G(s)``, ``(k)`` in gains, ``(n)`` in indices etc.
    aren't mistaken for sub-parts.  Roman numerals like ``(i)``, ``(ii)``
    are also rejected so they stay inside their parent sub-part instead
    of starting a new image."""
    t = text.strip()
    if not t:
        return None
    # Must contain at least one parenthesis — keeps bare body-text letters out.
    if "(" not in t and ")" not in t:
        return None
    inner = t.strip("()").strip()
    if not inner:
        return None
    # Reject explicit roman numerals
    if inner.lower() in ROMAN_SKIP:
        return None
    if len(inner) != 1:
        return None
    c = inner.lower()
    if c not in SUBPART_LETTERS:
        return None
    return c


def configure_tesseract() -> None:
    if TESSERACT_CMD:
        p = Path(TESSERACT_CMD)
        if p.exists():
            pytesseract.pytesseract.tesseract_cmd = str(p)


def render_page(page: "fitz.Page", dpi: int) -> Image.Image:
    pix = page.get_pixmap(dpi=dpi)
    return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")


def detect_year(data: dict, page_height: int) -> str | None:
    """Look in the top YEAR_SEARCH_TOP_FRAC of the page for an academic-year
    string like "2022-2023" or "2022-23".  Returns a normalised "YYYY-YY"
    string or None.  Rejects mathematically-impossible spans (e.g. an OCR
    misread of "2013-14" as "2013-24")."""
    top_zone = int(page_height * YEAR_SEARCH_TOP_FRAC)
    parts: list[tuple[int, int, str]] = []
    for i in range(len(data["text"])):
        t = data["text"][i].strip()
        if not t or data["top"][i] > top_zone:
            continue
        try:
            conf = int(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1
        if conf >= 0 and conf < 25:
            continue
        parts.append((data["top"][i], data["left"][i], t))
    parts.sort()
    joined = " ".join(t for _, _, t in parts)

    # Find every plausible candidate, keep the first that validates.
    for m in YEAR_RE.finditer(joined):
        y1 = int(m.group(1))
        raw_y2 = m.group(2)
        if len(raw_y2) == 2:
            y2_full = (y1 // 100) * 100 + int(raw_y2)
            if y2_full < y1:        # rolled past century boundary (rare)
                y2_full += 100
        else:
            y2_full = int(raw_y2)
        # Academic year spans 0 or 1 calendar years; anything else is OCR noise.
        if y2_full - y1 not in (0, 1):
            continue
        return f"{y1}-{y2_full % 100:02d}"
    return None


def scan_page(
    img: Image.Image,
) -> tuple[list[tuple[int, int, str]], list[tuple[int, str]], str | None]:
    """OCR a page once and return:
        questions  — [(question_num_guess, y_top, tier), …]   sorted by y
        subparts   — [(y_top, letter), …]                     sorted by y
        year       — "YYYY-YY" string if a paper header is found, else None

    All geometric thresholds are derived from the page's actual width and
    height so the function adapts to scans of any pixel size.
    """
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    w_px, h_px = img.width, img.height

    # Per-page pixel thresholds
    lm_min  = int(w_px * LEFT_MARGIN_MIN_FRAC)
    lm_max  = int(w_px * LEFT_MARGIN_MAX_FRAC)
    sp_min  = int(w_px * SUBPART_MIN_FRAC)
    sp_max  = int(w_px * SUBPART_MAX_FRAC)
    min_h   = max(8, int(h_px * MIN_MARKER_HEIGHT_FRAC))
    max_h   = int(h_px * MAX_MARKER_HEIGHT_FRAC)
    min_w   = max(4, int(w_px * MIN_MARKER_WIDTH_FRAC))
    sp_w    = max(6, int(w_px * SUBPART_MIN_WIDTH_FRAC))
    gap     = int(h_px * MIN_SUBPART_GAP_FRAC)
    top_limit = int(h_px * TOP_FRACTION)
    bot_limit = int(h_px * BOTTOM_FRACTION)

    q_out: list[tuple[int, int, str]] = []
    sp_out: list[tuple[int, str]] = []

    for i in range(len(data["text"])):
        text = data["text"][i].strip()
        if not text:
            continue
        x = data["left"][i]
        y = data["top"][i]
        w = data["width"][i]
        h = data["height"][i]
        try:
            conf = int(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1

        # Common filters
        if y < top_limit or y > bot_limit:
            continue

        # ── Try as a combined "Q + first sub-part" token ───────────────────
        # OCR routinely glues "6.(a)" into a single token.  Restricted to
        # the question-marker x range so we don't mistake sub-parts of
        # other questions for new questions.
        if (lm_min <= x <= lm_max
                and min_h <= h <= max_h
                and not (0 <= conf < MIN_CONFIDENCE)):
            combo = parse_combo(text)
            if combo is not None:
                num, letter = combo
                q_out.append((num, y, "combo"))
                sp_out.append((y, letter))
                continue

        # ── Try as a question marker ───────────────────────────────────────
        if (lm_min <= x <= lm_max
                and min_h <= h <= max_h
                and w >= min_w
                and not (0 <= conf < MIN_CONFIDENCE)):
            parsed = parse_marker(text)
            if parsed is not None:
                num, tier = parsed
                if tier == "loose" and 0 <= conf < LOOSE_MIN_CONFIDENCE:
                    pass   # fall through; try as sub-part below
                else:
                    q_out.append((num, y, tier))
                    continue

        # ── Try as a sub-part marker ───────────────────────────────────────
        if (sp_min <= x <= sp_max
                and min_h <= h <= max_h
                and w >= sp_w
                and not (0 <= conf < SUBPART_MIN_CONF)):
            letter = parse_subpart(text)
            if letter is not None:
                sp_out.append((y, letter))

    q_out.sort(key=lambda t: t[1])
    sp_out.sort(key=lambda t: t[0])

    # De-dupe question markers within 8 px of each other (keep strongest tier)
    tier_rank = {"strict": 0, "combo": 0, "loose": 1, "fix1": 2}
    cleaned_q: list[tuple[int, int, str]] = []
    for m in q_out:
        if cleaned_q and abs(m[1] - cleaned_q[-1][1]) < 8:
            if tier_rank[m[2]] < tier_rank[cleaned_q[-1][2]]:
                cleaned_q[-1] = m
        else:
            cleaned_q.append(m)

    # De-dupe sub-parts: drop any closer than `gap` to the previous one.
    cleaned_sp: list[tuple[int, str]] = []
    for sp in sp_out:
        if cleaned_sp and sp[0] - cleaned_sp[-1][0] < gap:
            continue
        cleaned_sp.append(sp)

    year = detect_year(data, h_px)
    return cleaned_q, cleaned_sp, year


def collect_markers(
    doc: "fitz.Document",
) -> tuple[list[tuple[int, int, int, str]],
           list[tuple[int, int, str]],
           list[Image.Image | None],
           list[str | None]]:
    """Walk every page; return:
        events     — [(page_idx, y_top, q_num, tier), …]   reading order
        subparts   — [(page_idx, y_top, letter), …]        reading order
        images     — rendered images per page (None for skipped pages)
        page_years — year string (e.g. "2022-23") for each page; carries
                     forward from the latest header seen until a new one
                     appears.
    """
    events: list[tuple[int, int, int, str]] = []
    subparts: list[tuple[int, int, str]] = []
    images: list[Image.Image | None] = []
    page_years: list[str | None] = []
    current_year: str | None = None

    for page_idx in range(len(doc)):
        if (page_idx + 1) in SKIP_PAGES:
            images.append(None)
            page_years.append(current_year)
            print(f"  page {page_idx + 1}: skipped")
            continue
        img = render_page(doc[page_idx], DPI)
        images.append(img)
        q_markers, sp_markers, year = scan_page(img)
        if year:
            current_year = year
        page_years.append(current_year)

        for num, y, tier in q_markers:
            events.append((page_idx, y, num, tier))
        for y, letter in sp_markers:
            subparts.append((page_idx, y, letter))
        if q_markers or sp_markers or year:
            qs  = ", ".join(f"{n}({t[0]})" for n, _, t in q_markers) or "-"
            sps = "".join(letter for _, letter in sp_markers) or "-"
            year_tag = f"  [year {year}]" if year else ""
            print(f"  page {page_idx + 1}:  Q[{qs}]  sub[{sps}]{year_tag}")
    return events, subparts, images, page_years


def determine_question_end(
    events: list[tuple[int, int, int, str]],
    idx: int,
    n_pages: int,
) -> tuple[int, int | None]:
    """Decide where question `idx` ends.

    Returns (end_page, end_y).  ``end_y is None`` means "go to the
    bottom of ``end_page``".
    """
    s_page, _s_y, s_num, _s_tier = events[idx]
    cap_page = min(s_page + MAX_QUESTION_PAGES - 1, n_pages - 1)

    if idx + 1 >= len(events):
        return (s_page, None)

    n_page, n_y, n_num, _n_tier = events[idx + 1]
    if n_num <= s_num:                  # paper restart
        return (s_page, None)
    if n_page > cap_page:               # next marker too far away
        return (cap_page, None)
    return (n_page, n_y)


def split_into_cycles(
    q_subs: list[tuple[int, int, str]],
    start_num: int,
) -> list[tuple[int, list[tuple[int, int, str]]]]:
    """Group a question's sub-parts into "virtual question" cycles.

    When OCR misses an intermediate question marker (say Q6 between
    Q5 and Q8) but still finds that question's (a)/(b)/(c) markers,
    we see something like ``a b c a b c a b c`` inside Q5's region.
    Each ``(a)`` after the first one starts a new virtual question.

    Rules:
      * The first sub-part in the question seeds a new cycle (whatever
        its letter — sometimes OCR misses the leading ``(a)``).
      * Inside an active cycle, a letter is appended only if it's
        strictly greater than the previous letter.
      * A letter equal to ``'a'`` always starts a fresh cycle.
      * Anything else (out-of-order or a repeat) is dropped as OCR
        noise rather than spawning a bogus split.

    Returns [(virtual_num, [subs…]), …].
    """
    if not q_subs:
        return []
    groups: list[tuple[int, list[tuple[int, int, str]]]] = [
        (start_num, [q_subs[0]])
    ]
    current_num = start_num
    for sp in q_subs[1:]:
        letter = sp[2]
        prev_letter = groups[-1][1][-1][2]
        if letter == "a":
            current_num += 1
            groups.append((current_num, [sp]))
        elif letter > prev_letter:
            groups[-1][1].append(sp)
        # else: out-of-order or repeat — drop as OCR noise
    return groups


def subparts_in_question(
    subparts: list[tuple[int, int, str]],
    q_start: tuple[int, int],
    q_end: tuple[int, int | None],
    page_height: int,
) -> list[tuple[int, int, str]]:
    """Filter `subparts` down to those inside this question's region."""
    s_page, s_y = q_start
    e_page, e_y = q_end
    same_line_tol = int(page_height * SAME_LINE_TOL_FRAC)

    out: list[tuple[int, int, str]] = []
    for sp in subparts:
        p, y, _letter = sp
        if p < s_page or p > e_page:
            continue
        if p == s_page and y < s_y - same_line_tol:
            continue            # sub-part is above the question marker
        if p == e_page and e_y is not None and y >= e_y - 4:
            continue            # sub-part belongs to the next question
        out.append(sp)
    return out


def crop_region(
    images: list[Image.Image | None],
    start: tuple[int, int],
    end: tuple[int, int | None],
) -> Image.Image:
    """Crop and vertically stitch from `start` up to (but not including) `end`."""
    s_page, s_y = start
    e_page, e_y = end

    segments: list[Image.Image] = []
    for p in range(s_page, e_page + 1):
        img = images[p]
        if img is None:
            continue
        top = max(0, s_y - PAGE_TOP_PAD) if p == s_page else 0
        if p == e_page and e_y is not None:
            bot = max(top + 1, e_y - PAGE_BOTTOM_PAD)
        else:
            bot = img.height
        segments.append(img.crop((0, top, img.width, bot)))

    if len(segments) == 1:
        return segments[0]
    width = max(s.width for s in segments)
    height = sum(s.height for s in segments)
    out = Image.new("RGB", (width, height), "white")
    y = 0
    for s in segments:
        out.paste(s, ((width - s.width) // 2, y))
        y += s.height
    return out


def _parse_cli() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Extract exam-question sub-parts (or whole questions) "
                    "from a scanned PDF into one image file each.",
    )
    p.add_argument("pdf", nargs="?", default=PDF_PATH,
                   help=f"Input PDF.  Default: {PDF_PATH!r}")
    p.add_argument("out", nargs="?", default=OUTPUT_DIR,
                   help=f"Output folder.  Default: {OUTPUT_DIR!r}")
    p.add_argument("--dpi", type=int, default=DPI,
                   help=f"Render DPI.  Default: {DPI}")
    p.add_argument("--max-pages", type=int, default=MAX_QUESTION_PAGES,
                   help="Cap each question to this many pages.  "
                        f"Default: {MAX_QUESTION_PAGES}")
    p.add_argument("--skip", type=int, nargs="*", default=SKIP_PAGES,
                   help="1-indexed page numbers to ignore (e.g. --skip 1 2)")
    p.add_argument("--no-split", action="store_true",
                   help="Save each whole question as one image (don't split "
                        "by (a)/(b)/(c) sub-parts).")
    return p.parse_args()


def main() -> None:
    args = _parse_cli()

    # Apply CLI overrides
    global DPI, MAX_QUESTION_PAGES, SKIP_PAGES, SPLIT_SUB_PARTS
    DPI = args.dpi
    MAX_QUESTION_PAGES = args.max_pages
    SKIP_PAGES = list(args.skip)
    if args.no_split:
        SPLIT_SUB_PARTS = False

    configure_tesseract()

    pdf_path = Path(args.pdf)
    out_dir = Path(args.out)
    if not pdf_path.exists():
        sys.exit(f"PDF not found: {pdf_path}")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Opening {pdf_path.name}")
    doc = fitz.open(pdf_path)
    print(f"  {len(doc)} pages, rendering at {DPI} DPI")
    print(f"  split sub-parts: {SPLIT_SUB_PARTS}\n")

    print("Scanning pages for markers...")
    events, subparts, images, page_years = collect_markers(doc)
    if not events:
        sys.exit(
            "\nNo question markers found.  Try lowering MIN_CONFIDENCE,\n"
            "raising DPI, or widening the LEFT_MARGIN_*_FRAC range."
        )

    print(f"\n{len(events)} question marker(s), {len(subparts)} sub-part(s) detected")
    detected_years = sorted({y for y in page_years if y})
    print(f"  detected year tags: {detected_years or 'none'}\n")
    print("Cropping & saving...")
    save_kwargs: dict = {}
    if OUTPUT_FORMAT.upper() == "JPEG":
        save_kwargs = {"quality": 92, "optimize": True}

    seq = 0
    for q_idx, (q_page, q_y, q_num, q_tier) in enumerate(events):
        q_end = determine_question_end(events, q_idx, len(images))
        year_tag = page_years[q_page] or UNKNOWN_YEAR_TAG
        page_h = images[q_page].height if images[q_page] is not None else 3085
        cross_limit_px = int(page_h * SUBPART_PAGE_CROSS_LIMIT_FRAC)

        q_subs = (subparts_in_question(subparts, (q_page, q_y), q_end, page_h)
                  if SPLIT_SUB_PARTS else [])

        if not q_subs:
            # No sub-parts (or splitting disabled) → save whole question
            seq += 1
            img = crop_region(images, (q_page, q_y), q_end)
            out_name = FILENAME_PATTERN.format(
                seq=seq, year=year_tag, page=q_page + 1, num=q_num, part="")
            img.save(out_dir / out_name, OUTPUT_FORMAT, **save_kwargs)
            print(f"  -> {out_name}   {img.width}x{img.height}   [{q_tier}]")
            continue

        # Detect (a)(b)(c) cycles → split into virtual sub-questions when OCR
        # missed an intermediate question number marker.
        cycles = split_into_cycles(q_subs, q_num)

        # Flatten cycles into save instructions so we can compute end-points
        # that cross cycle boundaries cleanly.
        flat: list[tuple[int, int, int, str, bool]] = []
        # tuple: (page, y, virtual_num, letter, is_first_sub_of_real_question)
        for c_idx, (v_num, v_subs) in enumerate(cycles):
            for sp_idx, (sp_page, sp_y, letter) in enumerate(v_subs):
                is_first = (c_idx == 0 and sp_idx == 0)
                flat.append((sp_page, sp_y, v_num, letter, is_first))

        for i, (sp_page, sp_y, v_num, letter, is_first) in enumerate(flat):
            seq += 1

            # Start: for the very first sub-part of the (real) question,
            # also include the question heading ("1.") which lives on the
            # same line, by taking min(q_y, sp_y).
            if is_first and sp_page == q_page:
                start = (q_page, min(q_y, sp_y))
            else:
                start = (sp_page, sp_y)

            # End: next sub-part (whether in same cycle or next), else q_end.
            if i + 1 < len(flat):
                nxt = flat[i + 1]
                end = (nxt[0], nxt[1])
                # If the next sub-part lives on a later page and sits well
                # below the top of that page, there's probably a section
                # break / header between them.  Cap the crop at the end
                # of the current page instead of bleeding into the new
                # page's header.
                if nxt[0] > start[0] and nxt[1] > cross_limit_px:
                    end = (start[0], None)
            else:
                end = q_end

            img = crop_region(images, start, end)
            sp_year = page_years[start[0]] or UNKNOWN_YEAR_TAG
            out_name = FILENAME_PATTERN.format(
                seq=seq, year=sp_year, page=start[0] + 1,
                num=v_num, part=letter)
            img.save(out_dir / out_name, OUTPUT_FORMAT, **save_kwargs)
            print(f"  -> {out_name}   {img.width}x{img.height}   [{q_tier}/{letter}]")

    print(f"\nDone. {seq} image(s) written to:\n  {out_dir}")


if __name__ == "__main__":
    main()
