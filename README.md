# PDF Question Extractor

A small Python tool that takes a scanned exam-paper PDF and saves every
question — split by sub-parts `(a)`, `(b)`, `(c)`, … — as its own PNG
image.

The script lives at `extract_questions.py` in this folder.

---

## What it does

For an input PDF that looks like this on one page:

```
1.   (a) HDLC is an international crane manufacturer ...
         [table]
     (b) Discuss Hersey and Blanchard's Situational ...
     (c) What is the span of management control? ...

2.   (a) How can the choice of facility layout ...
```

…it writes one image per sub-part, tagged with the academic year from
the paper header:

```
q_001_y2022-23_p01_n1a.png    Q1 part (a)  — with the "1." heading visible
q_002_y2022-23_p01_n1b.png    Q1 part (b)
q_003_y2022-23_p01_n1c.png    Q1 part (c)
q_004_y2022-23_p02_n2a.png    Q2 part (a)  — with the "2." heading visible
q_005_y2022-23_p02_n2b.png    Q2 part (b)
...
```

The script reads the year string ("B. Sc. Engineering Examinations
2022-2023") off the top of each paper's first page and carries it
forward to every page until a new paper header appears.

Multi-page sub-parts are stitched into one tall image automatically.

---

## Requirements

| | |
|---|---|
| Python | 3.10 or newer |
| Tesseract OCR | Installed system-wide |
| Python packages | `pymupdf`, `pillow`, `pytesseract` |

### One-time install

```powershell
# Tesseract binary (Windows, via winget)
winget install --id UB-Mannheim.TesseractOCR --silent

# Python libraries
python -m pip install pymupdf pillow pytesseract
```

Tesseract installs to `C:\Program Files\Tesseract-OCR\tesseract.exe` by
default, which is what the script expects. If yours lives somewhere else,
change `TESSERACT_CMD` in the script's CONFIG block.

---

## Quick start

1. Open `extract_questions.py` in any editor.
2. Edit two lines near the top of the **CONFIG** block:

   ```python
   PDF_PATH   = r"C:\path\to\your\input.pdf"
   OUTPUT_DIR = r"C:\path\to\output\folder"
   ```

3. Run it:

   ```powershell
   python extract_questions.py
   ```

The output folder is created automatically if it doesn't exist. Existing
files with the same name are overwritten.

---

## Command-line usage

You can also pass paths on the command line instead of editing the file:

```powershell
python extract_questions.py "input.pdf" "output_folder"
```

| Flag | Default | Meaning |
|---|---|---|
| `--dpi N` | 220 | Render resolution. Higher = sharper / slower / bigger files. |
| `--max-pages N` | 2 | Cap each question to this many pages (protects against OCR misses that would otherwise stretch one crop across many pages). |
| `--skip P [P ...]` | (none) | 1-indexed page numbers to ignore entirely. Useful for cover pages, instructions, blank pages. |
| `--no-split` | (off) | Save each whole question as one image instead of splitting by `(a)/(b)/(c)` sub-parts. |
| `--help` | | Show usage. |

Examples:

```powershell
# Use a custom DPI and skip the first two pages
python extract_questions.py --dpi 300 --skip 1 2

# Get whole-question images instead of sub-part splits
python extract_questions.py --no-split

# Allow longer questions (up to 3 pages each)
python extract_questions.py --max-pages 3
```

---

## Output filenames

The default pattern is:

```
q_{seq:03d}_y{year}_p{page:02d}_n{num}{part}.png
```

- `seq`  — global counter across all saved images (`001`, `002`, …).
- `year` — academic year tag pulled from the paper header (`2022-23`, `2018-19`, …) or `unknown` if no header has been seen yet. Carries forward from the most recently detected year.
- `page` — 1-indexed source page where the image starts.
- `num`  — question number the OCR thought it saw (`1`, `5`, `12`, …). When OCR misses an intermediate question marker, the script infers a number from cycle detection (see below).
- `part` — sub-part letter (`a`, `b`, `c`, …) or an empty string if the question wasn't split.

To change the format, edit `FILENAME_PATTERN` in the CONFIG block — any
of those placeholders are optional. Setting `FILENAME_PATTERN = "q_{seq:03d}_p{page:02d}_n{num}{part}.png"` drops the year if you don't want it.

---

## How edge cases are handled

| Situation | What the script does |
|---|---|
| The PDF mixes pages scanned at very different pixel sizes (e.g. 1500-wide and 3300-wide pages) | All geometric thresholds are expressed as fractions of the rendered page's width/height, computed fresh per page. |
| OCR glues `6.(a)` into one token | A "combo" parser recognises `N.(L)` / `N(L)` patterns and emits both a question event and a sub-part event for that position. |
| Paper header says "Examinations 2013-24" (OCR error) | Year span is validated — academic years must span 0 or 1 calendar year. Implausible spans are rejected so the previous valid year carries forward. |
| `1.` is OCR-misread as `i`, `l`, `\|`, or `2.` as `2:` | Recognised as the original digit. |
| A sub-part marker `(a)` sits on the same line as the question marker `1.` | The first sub-part image includes both the `1.` heading and the `(a)` text. |
| Roman-numeral sub-sub-parts `(i)`, `(ii)`, `(iii)` | Skipped — they stay inside their parent sub-part. |
| Equation variables like `(s)` in `G(s)`, `(k)`, `(n)` | Skipped — only letters `(a)`–`(h)` count as sub-parts. |
| A page-body number like `35` is OCR'd at the left margin | Rejected — `MAX_QUESTION_NUMBER` caps the realistic range and `LOOSE_MIN_CONFIDENCE` filters bare-digit false positives. |
| The same `(a)` is OCR'd twice within one line of question content | The duplicate is dropped — sub-parts within `MIN_SUBPART_GAP` pixels of the previous one are noise. |
| Question spans multiple pages | Slices are stitched vertically. |
| The PDF contains several separate exam papers, each restarting at `1.` | When the next detected number is `≤` the current one, it's treated as a paper break and the current question ends at the bottom of its starting page. |
| OCR misses Q6 between Q5 and Q8 but still catches `(a)(b)(c)(a)(b)(c)(a)(b)(c)` | "Cycle detection" treats each `(a)` as a new virtual question, so filenames become `n5a/b/c, n6a/b/c, n7a/b/c` instead of duplicates. |
| Sub-part crop would cross a page break into a new exam-paper section header | The crop is capped at the bottom of the starting page (controlled by `SUBPART_PAGE_CROSS_LIMIT`). |
| The last marker in the document | Ends at the bottom of its own page (not the end of the PDF). |

---

## Tuning knobs (CONFIG block)

If detection misses real markers or grabs false positives, these are the
levers in order of usefulness:

> **Note:** Every geometric threshold is now expressed as a **fraction of
> the page's pixel dimensions**, not as an absolute pixel count. That's
> what lets the same defaults work whether a page renders to 1500 px or
> 3300 px wide.

| Setting | What it controls | Try raising | Try lowering |
|---|---|---|---|
| `DPI` | Render resolution. | …if markers look blurry to Tesseract. | …if the script runs too slow. |
| `MIN_CONFIDENCE` | Min OCR confidence for a question marker. | …to drop false positives. | …to catch faint scans. |
| `SUBPART_MIN_CONF` | Same, for sub-parts. | | |
| `LEFT_MARGIN_MIN_FRAC` / `_MAX_FRAC` | x-window for question markers, as a fraction of page width. Defaults `0.05`–`0.14`. | (shift right if scan has wider left padding) | (shift left if markers sit closer to the edge) |
| `SUBPART_MIN_FRAC` / `_MAX_FRAC` | Same window for `(a)/(b)/(c)`. Defaults `0.11`–`0.25`. | …for diagram-heavy pages where sub-parts are indented further. | |
| `MAX_QUESTION_NUMBER` | Largest "Q-number" the OCR is allowed to claim. Default 15. | …if your paper has >15 questions. | …to be stricter about false positives like reading `35` as Q35. |
| `LOOSE_MIN_CONFIDENCE` | Confidence floor for bare-digit (no period) Q-markers. | …if too many false positives. | …if a legit `8` without period is being dropped. |
| `SUBPART_LETTERS` | Allowed sub-part letters. Default `"abcdefgh"` — letters past `(h)` are almost always math variables (`(s)` in `G(s)`, `(k)`, `(n)`, …). | | |
| `MIN_SUBPART_GAP_FRAC` | Min vertical gap between consecutive sub-parts, as a fraction of page height. Default `0.020`. Stops OCR from picking up the same `(a)` twice on one line. | …if duplicate sub-parts still slip through. | …if a real adjacent pair is too close (uncommon). |
| `MIN_MARKER_WIDTH_FRAC` | Minimum marker width as a fraction of page width. Kills 1-2 px grid lines. | …if grid lines still show up. | …if a real `1.` is being rejected. |
| `MAX_QUESTION_PAGES` | Max pages one question may span. | …for long, multi-page essay questions. | |
| `SUBPART_PAGE_CROSS_LIMIT_FRAC` | When a sub-part crop would cross a page break, only cross if the next sub-part sits within this fraction of the new page's top. Default `0.115`. Stops crops from bleeding into the next paper's section header. | …if legitimate cross-page sub-parts are being clipped. | …if page headers are still appearing in crops. |
| `TOP_FRACTION` / `BOTTOM_FRACTION` | Vertical exclusion zones (headers / page numbers). | …if footers are being read as markers. | |
| `SAME_LINE_TOL_FRAC` | Slack so an `(a)` slightly above `1.` still counts as that question's sub-part. | …if first sub-parts are being dropped. | |
| `YEAR_SEARCH_TOP_FRAC` | Fraction of the page top searched for the year string. Default `0.15`. | …if year strings sit lower on your covers. | |
| `UNKNOWN_YEAR_TAG` | Filename tag used before any year has been seen. Default `"unknown"`. | | |
| `SKIP_PAGES` | 1-indexed pages to ignore entirely. | List cover / blank / index pages here. | |

---

## Troubleshooting

**"PDF not found"**
Check the `PDF_PATH` you set — use raw strings (`r"..."`) so backslashes
aren't interpreted as escape sequences.

**"No question markers found"**
Your PDF's markers probably sit outside the default `x` window. Open the
PDF, eyeball the x-position of the digits at the left margin, and widen
`LEFT_MARGIN_MIN_X` / `LEFT_MARGIN_MAX_X` to cover them. If the PDF is
faint, also lower `MIN_CONFIDENCE` (e.g. to `10`).

**Output image is many pages tall / a single image swallows several questions**
A real question marker was missed by OCR. Either lower `MIN_CONFIDENCE`
or lower `MAX_QUESTION_PAGES` to cap the over-stretch.

**A table-grid line is being saved as a question**
Raise `MIN_MARKER_WIDTH` (the default `10` already kills most of these;
try `14`).

**Sub-parts are not being split**
Make sure `SPLIT_SUB_PARTS = True` and that `--no-split` is **not** on
the command line. If sub-parts in your PDF use a different style (e.g.
`a)` without parens, or `A.` in capitals), edit `parse_subpart()` in the
script.

**Console shows mojibake like `→`**
Run with `python -X utf8 extract_questions.py` or set
`$env:PYTHONIOENCODING="utf-8"` before running (the script already
reconfigures stdout on Python 3.7+, but some launchers ignore that).

---

## File layout

```
babas/
├─ extract_questions.py   # the script
├─ README.md              # this file
└─ questions/             # output (created on first run)
   ├─ q_001_p01_n1a.png
   ├─ q_002_p01_n1b.png
   └─ ...
```
