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

…it writes one image per sub-part:

```
q_001_p01_n1a.png    Q1 part (a)  — with the "1." heading visible
q_002_p01_n1b.png    Q1 part (b)
q_003_p01_n1c.png    Q1 part (c)
q_004_p01_n2a.png    Q2 part (a)  — with the "2." heading visible
q_005_p01_n2b.png    Q2 part (b)
...
```

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
q_{seq:03d}_p{page:02d}_n{num}{part}.png
```

- `seq`  — global counter across all saved images (`001`, `002`, …).
- `page` — 1-indexed source page where the image starts.
- `num`  — question number the OCR thought it saw (`1`, `5`, `12`, …). When OCR misses an intermediate question marker, the script infers a number from cycle detection (see below).
- `part` — sub-part letter (`a`, `b`, `c`, …) or an empty string if the question wasn't split.

To change the format, edit `FILENAME_PATTERN` in the CONFIG block — any
of those placeholders are optional.

---

## How edge cases are handled

| Situation | What the script does |
|---|---|
| `1.` is OCR-misread as `i`, `l`, `\|`, or `2.` as `2:` | Recognised as the original digit. |
| A sub-part marker `(a)` sits on the same line as the question marker `1.` | The first sub-part image includes both the `1.` heading and the `(a)` text. |
| Roman-numeral sub-sub-parts `(i)`, `(ii)`, `(iii)` | Skipped — they stay inside their parent sub-part. |
| Question spans multiple pages | Slices are stitched vertically. |
| The PDF contains several separate exam papers, each restarting at `1.` | When the next detected number is `≤` the current one, it's treated as a paper break and the current question ends at the bottom of its starting page. |
| OCR misses Q6 between Q5 and Q8 but still catches `(a)(b)(c)(a)(b)(c)(a)(b)(c)` | "Cycle detection" treats each letter restart as a new virtual question, so filenames become `n5a/b/c, n6a/b/c, n7a/b/c` instead of duplicates. |
| The last marker in the document | Ends at the bottom of its own page (not the end of the PDF). |

---

## Tuning knobs (CONFIG block)

If detection misses real markers or grabs false positives, these are the
levers in order of usefulness:

| Setting | What it controls | Try raising | Try lowering |
|---|---|---|---|
| `DPI` | Render resolution. | …if markers look blurry to Tesseract. | …if scripts run too slow. |
| `MIN_CONFIDENCE` | Min OCR confidence for a question marker. | …to drop false positives. | …to catch faint scans. |
| `SUBPART_MIN_CONF` | Same, for sub-parts. | | |
| `LEFT_MARGIN_MIN_X` / `_MAX_X` | x-pixel window where question markers must sit. | (shift right if your scan has wider left padding) | (shift left if markers are close to the edge) |
| `SUBPART_MIN_X` / `_MAX_X` | Same window for `(a)/(b)/(c)`. | | |
| `MIN_MARKER_WIDTH` | Minimum width in pixels — filters table-grid `\|` noise. | …if grid lines are still showing up. | …if a real `1.` is being rejected. |
| `MAX_QUESTION_PAGES` | Max pages one question may span. | …for long, multi-page essay questions. | (rarely needed) |
| `TOP_FRACTION` / `BOTTOM_FRACTION` | Vertical exclusion zones (headers / page numbers). | …if page numbers near the bottom are being read as markers. | |
| `SAME_LINE_TOL` | Pixels of slack so an `(a)` slightly above `1.` still counts as that question's sub-part. | …if first sub-parts are being dropped. | |
| `SKIP_PAGES` | 1-indexed pages to ignore entirely. | List your cover / blank / index pages here. | |

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
