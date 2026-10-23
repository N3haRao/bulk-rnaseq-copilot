"""Reading the uploaded sample table in Python (for the setup screen).

The counts file is only read by the R code bank; the app needs just the
metadata columns and their values to offer sensible choices before step 01 runs.
"""

import csv
import gzip
import io
from pathlib import Path

TABLE_EXTENSIONS = {".csv", ".tsv", ".txt", ".xlsx", ".xls"}


def table_extension(filename: str) -> str:
    """Extension that decides how a file is read ('.csv', '.tsv.gz' -> '.tsv', ...)."""
    name = filename.lower()
    if name.endswith(".gz"):
        name = name[:-3]
    return Path(name).suffix


def read_table(path: Path) -> tuple[list[str], list[list[str]]]:
    """Header and rows of a .csv/.tsv/.txt (optionally .gz) or .xlsx file, all as text."""
    ext = table_extension(path.name)
    if ext in {".xlsx", ".xls"}:
        return _read_excel(path)
    opener = gzip.open if path.name.lower().endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8-sig", errors="replace", newline="") as f:
        text = f.read()
    sample = text[:20000]
    if ext == ".tsv" or ("\t" in sample.splitlines()[0] if sample else False):
        delim = "\t"
    else:
        try:
            delim = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
        except csv.Error:
            delim = ","
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delim) if any(c.strip() for c in r)]
    if not rows:
        return [], []
    header = [h.strip() for h in rows[0]]
    body = [[c.strip() for c in r] + [""] * (len(header) - len(r)) for r in rows[1:]]
    return header, [r[: len(header)] for r in body]


def _read_excel(path: Path) -> tuple[list[str], list[list[str]]]:
    try:
        import openpyxl
    except ImportError as e:  # pragma: no cover - dependency is in requirements
        raise ValueError("Reading Excel files needs the 'openpyxl' package; save the sheet as .csv instead.") from e
    if path.suffix.lower() == ".xls":
        raise ValueError("Old .xls files are not supported here; save the sheet as .xlsx or .csv.")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = [["" if v is None else str(v).strip() for v in r] for r in ws.iter_rows(values_only=True)]
    rows = [r for r in rows if any(r)]
    if not rows:
        return [], []
    return rows[0], rows[1:]


def count_file_preview(path: Path, max_lines: int = 6) -> dict:
    """First lines of the counts file: header and number of columns, without reading it all."""
    ext = table_extension(path.name)
    if ext in {".xlsx", ".xls"}:
        header, rows = _read_excel(path)
        return {"columns": header, "n_columns": len(header), "first_rows": rows[:max_lines - 1]}
    opener = gzip.open if path.name.lower().endswith(".gz") else open
    lines = []
    with opener(path, "rt", encoding="utf-8-sig", errors="replace", newline="") as f:
        for _ in range(max_lines):
            line = f.readline()
            if not line:
                break
            lines.append(line.rstrip("\r\n"))
    if not lines:
        return {"columns": [], "n_columns": 0, "first_rows": []}
    delim = "\t" if "\t" in lines[0] else ("," if "," in lines[0] else ";")
    parsed = list(csv.reader(lines, delimiter=delim))
    return {"columns": parsed[0], "n_columns": len(parsed[0]), "first_rows": parsed[1:]}
