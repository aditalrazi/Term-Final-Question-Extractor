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
TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# ── Rendering ───────────────────────────────────────────────────────────────
DPI = 220              # raise for sharper output, lower for speed

# ── Sub-part splitting ──────────────────────────────────────────────────────
# When True, each question is split into one image per (a)/(b)/(c)/... part.
# When False, every question is saved as a single image (legacy behaviour).
SPLIT_SUB_PARTS = True

# ── Question-marker detection ──────────────────────────────────────────────
LEFT_MARGIN_MIN_X  = 140    # marker x must be >= this
LEFT_MARGIN_MAX_X  = 235    # marker x must be <= this
MIN_MARKER_HEIGHT  = 18     # px — ignores small dots and noise
MAX_MARKER_HEIGHT  = 60     # px — ignores big stuff (headings, etc.)
MIN_MARKER_WIDTH   = 10     # px — kills 1-2 px wide table-grid '|' lines
MIN_CONFIDENCE     = 20     # OCR confidence floor
TOP_FRACTION       = 0.045  # ignore the top 4.5% of each page (headers)
BOTTOM_FRACTION    = 0.955  # ignore the bottom 4.5% (footers / page nrs)

# ── Sub-part-marker detection ──────────────────────────────────────────────
SUBPART_MIN_X      = 180    # sub-parts sit slightly indented from questions
SUBPART_MAX_X      = 330
SUBPART_MIN_HEIGHT = 18
SUBPART_MAX_HEIGHT = 60
SUBPART_MIN_WIDTH  = 15     # "(a)" rendered at ~220 dpi is ~30-40 px wide
SUBPART_MIN_CONF   = 30
# Sub-parts on the SAME LINE as a question marker (often (a)) are accepted
# even if their y is a little above the question marker — this is the slack.
SAME_LINE_TOL      = 40

# ── Cropping ────────────────────────────────────────────────────────────────
PAGE_TOP_PAD       = 12      # px of whitespace kept above each cropped region
PAGE_BOTTOM_PAD    = 12      # px of whitespace kept below each cropped region
MAX_QUESTION_PAGES = 2       # cap each question to this many pages
                             #   (protects against missed-marker / paper-break
                             #   over-stretches).  Raise if your questions are
                             #   genuinely longer.

# ── Pages to ignore entirely (1-indexed) ────────────────────────────────────
SKIP_PAGES: list[int] = []

# ── Output ──────────────────────────────────────────────────────────────────
OUTPUT_FORMAT    = "PNG"     # "PNG" or "JPEG"
# Placeholders:
#   {seq}    = global detection index across all saved images
#   {page}   = page where the image starts (1-indexed)
#   {num}    = question number Tesseract thought it saw
#   {part}   = sub-part letter ('a', 'b', …) or '' for whole questions
FILENAME_PATTERN = "q_{seq:03d}_p{page:02d}_n{num}{part}.png"

# ─────────────────────────────────────────────────────────────────────────────


# Question-marker patterns
RE_STRICT = re.compile(r"^(\d{1,2})\s*[\.\,\:\;]$")     # "1.", "12.", "2:"
RE_LOOSE  = re.compile(r"^(\d{1,2})$")                  # bare "1", "12"
RE_ONE_FIX = re.compile(r"^[il|I!]\s*[\.\,\:\;]?$")     # i, l, |, …

# Sub-part patterns — explicit roman skip-list
ROMAN_SKIP = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}


def parse_marker(text: str) -> tuple[int, str] | None:
    """Return (question_number, tier) or None."""
    t = text.strip()
    if not t:
        return None
    if (m := RE_STRICT.match(t)):
        n = int(m.group(1))
        if 1 <= n <= 99:
            return n, "strict"
    if (m := RE_LOOSE.match(t)):
        n = int(m.group(1))
        if 1 <= n <= 99:
            return n, "loose"
    if RE_ONE_FIX.match(t):
        return 1, "fix1"
    return None


def parse_subpart(text: str) -> str | None:
    """Return the lowercase letter for a sub-part marker like "(a)", or None.
    Rejects Roman numerals like "(i)", "(ii)" so they stay inside their
    parent sub-part instead of starting a new image."""
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
    if c == "i":   # ambiguous with roman 'i'
        return None
    if "a" <= c <= "z":
        return c
    return None


def configure_tesseract() -> None:
    if TESSERACT_CMD:
        p = Path(TESSERACT_CMD)
        if p.exists():
            pytesseract.pytesseract.tesseract_cmd = str(p)


def render_page(page: "fitz.Page", dpi: int) -> Image.Image:
    pix = page.get_pixmap(dpi=dpi)
    return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")


def scan_page(img: Image.Image) -> tuple[list[tuple[int, int, str]],
                                          list[tuple[int, str]]]:
    """OCR a page once and split the tokens into:
        questions  — [(question_num_guess, y_top, tier), …]   sorted by y
        subparts   — [(y_top, letter), …]                     sorted by y
    """
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    top_limit = int(img.height * TOP_FRACTION)
    bot_limit = int(img.height * BOTTOM_FRACTION)

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

        # ── Try as a question marker ───────────────────────────────────────
        if (LEFT_MARGIN_MIN_X <= x <= LEFT_MARGIN_MAX_X
                and MIN_MARKER_HEIGHT <= h <= MAX_MARKER_HEIGHT
                and w >= MIN_MARKER_WIDTH
                and not (0 <= conf < MIN_CONFIDENCE)):
            parsed = parse_marker(text)
            if parsed is not None:
                num, tier = parsed
                q_out.append((num, y, tier))
                continue   # a single token is either a question OR a sub-part

        # ── Try as a sub-part marker ───────────────────────────────────────
        if (SUBPART_MIN_X <= x <= SUBPART_MAX_X
                and SUBPART_MIN_HEIGHT <= h <= SUBPART_MAX_HEIGHT
                and w >= SUBPART_MIN_WIDTH
                and not (0 <= conf < SUBPART_MIN_CONF)):
            letter = parse_subpart(text)
            if letter is not None:
                sp_out.append((y, letter))

    q_out.sort(key=lambda t: t[1])
    sp_out.sort(key=lambda t: t[0])

    # De-dupe question markers within 8 px of each other (keep strongest tier)
    tier_rank = {"strict": 0, "loose": 1, "fix1": 2}
    cleaned_q: list[tuple[int, int, str]] = []
    for m in q_out:
        if cleaned_q and abs(m[1] - cleaned_q[-1][1]) < 8:
            if tier_rank[m[2]] < tier_rank[cleaned_q[-1][2]]:
                cleaned_q[-1] = m
        else:
            cleaned_q.append(m)

    # De-dupe sub-parts the same way (same y → same letter)
    cleaned_sp: list[tuple[int, str]] = []
    for sp in sp_out:
        if cleaned_sp and abs(sp[0] - cleaned_sp[-1][0]) < 8:
            continue
        cleaned_sp.append(sp)

    return cleaned_q, cleaned_sp


def collect_markers(
    doc: "fitz.Document",
) -> tuple[list[tuple[int, int, int, str]],
           list[tuple[int, int, str]],
           list[Image.Image | None]]:
    """Walk every page; return:
        events    — [(page_idx, y_top, q_num, tier), …]   reading order
        subparts  — [(page_idx, y_top, letter), …]        reading order
        images    — rendered images per page (None for skipped pages)
    """
    events: list[tuple[int, int, int, str]] = []
    subparts: list[tuple[int, int, str]] = []
    images: list[Image.Image | None] = []

    for page_idx in range(len(doc)):
        if (page_idx + 1) in SKIP_PAGES:
            images.append(None)
            print(f"  page {page_idx + 1}: skipped")
            continue
        img = render_page(doc[page_idx], DPI)
        images.append(img)
        q_markers, sp_markers = scan_page(img)
        for num, y, tier in q_markers:
            events.append((page_idx, y, num, tier))
        for y, letter in sp_markers:
            subparts.append((page_idx, y, letter))
        if q_markers or sp_markers:
            qs = ", ".join(f"{n}({t[0]})" for n, _, t in q_markers) or "-"
            sps = "".join(letter for _, letter in sp_markers) or "-"
            print(f"  page {page_idx + 1}:  Q[{qs}]  sub[{sps}]")
    return events, subparts, images


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
    Each restart (letter <= previous letter) is treated as a new
    virtual question.  Returns [(virtual_num, [subs…]), …]."""
    if not q_subs:
        return []
    groups: list[tuple[int, list[tuple[int, int, str]]]] = [
        (start_num, [q_subs[0]])
    ]
    current_num = start_num
    for sp in q_subs[1:]:
        letter = sp[2]
        prev_letter = groups[-1][1][-1][2]
        if letter <= prev_letter:
            current_num += 1
            groups.append((current_num, [sp]))
        else:
            groups[-1][1].append(sp)
    return groups


def subparts_in_question(
    subparts: list[tuple[int, int, str]],
    q_start: tuple[int, int],
    q_end: tuple[int, int | None],
) -> list[tuple[int, int, str]]:
    """Filter `subparts` down to those inside this question's region."""
    s_page, s_y = q_start
    e_page, e_y = q_end

    out: list[tuple[int, int, str]] = []
    for sp in subparts:
        p, y, _letter = sp
        if p < s_page or p > e_page:
            continue
        if p == s_page and y < s_y - SAME_LINE_TOL:
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
    events, subparts, images = collect_markers(doc)
    if not events:
        sys.exit(
            "\nNo question markers found.  Try lowering MIN_CONFIDENCE,\n"
            "raising DPI, or widening LEFT_MARGIN_MIN_X / LEFT_MARGIN_MAX_X."
        )

    print(f"\n{len(events)} question marker(s), {len(subparts)} sub-part(s) detected\n")
    print("Cropping & saving...")
    save_kwargs: dict = {}
    if OUTPUT_FORMAT.upper() == "JPEG":
        save_kwargs = {"quality": 92, "optimize": True}

    seq = 0
    for q_idx, (q_page, q_y, q_num, q_tier) in enumerate(events):
        q_end = determine_question_end(events, q_idx, len(images))

        q_subs = subparts_in_question(subparts, (q_page, q_y), q_end) \
                 if SPLIT_SUB_PARTS else []

        if not q_subs:
            # No sub-parts (or splitting disabled) → save whole question
            seq += 1
            img = crop_region(images, (q_page, q_y), q_end)
            out_name = FILENAME_PATTERN.format(
                seq=seq, page=q_page + 1, num=q_num, part="")
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
            else:
                end = q_end

            img = crop_region(images, start, end)
            out_name = FILENAME_PATTERN.format(
                seq=seq, page=start[0] + 1, num=v_num, part=letter)
            img.save(out_dir / out_name, OUTPUT_FORMAT, **save_kwargs)
            print(f"  -> {out_name}   {img.width}x{img.height}   [{q_tier}/{letter}]")

    print(f"\nDone. {seq} image(s) written to:\n  {out_dir}")


if __name__ == "__main__":
    main()
