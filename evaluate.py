#!/usr/bin/env python
"""
Very simple, non-semantic comparison between a generated opinion and the bank's
existing reference OUTPUT for the same company.

This is NOT a semantic evaluation - it only does line/keyword-based matching of
section names and risk grades, and reports what's missing on either side. It is
meant purely for a quick sanity check during MVP development.

The reference OUTPUT file is read ONLY here, for evaluation - never as input to
the generation pipeline (see main.py / document_reader.discover_company_documents,
which excludes it by default).

Usage:
    python evaluate.py --company "BEO TRADE COM SRL"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import document_reader

DATA_ROOT = Path(__file__).resolve().parent / "data" / "opinie_risc"
OUTPUT_ROOT = Path(__file__).resolve().parent / "generated"

GRADE_RE = re.compile(r"^Risc\s+(scazut|mediu|ridicat)$", re.IGNORECASE)

# Reference section header -> normalized category name used to match against our
# generated categories (see src/ko_engine.py category_labels).
SECTION_MAP = {
    "risc strategic": "risc strategic",
    "risc bancar / comportament de plata": "risc bancar / comportament de plata",
    "risc financiar": "risc financiar",
    "risc reputational": "risc reputational",
    "colaterizare": "colaterizare",
    # Present in the reference but not modeled as a standalone graded category
    # in this MVP's KO engine (see ko_engine.py notes) - reported as reference-only.
    "procese / bpi": None,
    "alte riscuri": None,
}


def find_output_file(company_dir: Path) -> Path | None:
    for p in company_dir.iterdir():
        if p.name.lower().startswith("output"):
            return p
    return None


def parse_reference_output(lines: list[str]) -> dict:
    """Heuristic line-based parser for the bank's HTML-as-.doc reference opinions."""
    overall = None
    for i, line in enumerate(lines):
        if line.strip().lower() == "opinie risc" and i + 1 < len(lines) and GRADE_RE.match(lines[i + 1]):
            overall = lines[i + 1]
            break

    sections: dict[str, dict] = {}
    current_key = None
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        header_key = line.rstrip(":").strip().lower()
        if header_key in SECTION_MAP:
            current_key = header_key
            sections[current_key] = {"grade": None, "mentions": []}
            i += 1
            continue
        if current_key:
            if line == "Grad Risc" and i + 1 < len(lines) and GRADE_RE.match(lines[i + 1]):
                sections[current_key]["grade"] = lines[i + 1]
                i += 2
                continue
            if line == "Mentiuni":
                i += 1
                # collect mention lines until the next known marker
                stop_markers = {"grad risc", "mentiuni"} | set(SECTION_MAP.keys())
                while i < len(lines) and lines[i].strip().lower().rstrip(":") not in stop_markers:
                    sections[current_key]["mentions"].append(lines[i])
                    i += 1
                continue
        i += 1

    return {"overall_risk": overall, "sections": sections}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Compare a generated opinion with the bank's reference OUTPUT (non-semantic).")
    parser.add_argument("--company", required=True)
    args = parser.parse_args()

    company_dir = document_reader.find_company_dir(DATA_ROOT, args.company)
    if company_dir is None:
        print(f"Compania '{args.company}' nu a fost gasita.", file=sys.stderr)
        return 1

    generated_path = OUTPUT_ROOT / company_dir.name / "opinion.json"
    if not generated_path.exists():
        print(f"Nu exista opinie generata pentru '{company_dir.name}'. Ruleaza intai main.py.", file=sys.stderr)
        return 1
    generated = json.loads(generated_path.read_text(encoding="utf-8"))

    output_file = find_output_file(company_dir)
    if output_file is None:
        print(f"Niciun fisier OUTPUT de referinta gasit pentru '{company_dir.name}'.", file=sys.stderr)
        return 1

    ref_doc = document_reader.read_html_doc(output_file)
    ref_lines = [l for l in ref_doc.text.split("\n") if l.strip()]
    reference = parse_reference_output(ref_lines)

    gen_opinion = generated["opinion"]
    gen_by_section = {r["type"].strip().lower(): r for r in gen_opinion["risks"]}

    print(f"=== Evaluare (non-semantica): {company_dir.name} ===\n")
    print(f"Fisier referinta: {output_file.name}\n")

    print("--- Risc general ---")
    print(f"  Referinta : {reference['overall_risk'] or '(neparsat)'}")
    print(f"  Generat   : {gen_opinion['overall_risk']}")
    match = (reference["overall_risk"] or "").strip().lower() == gen_opinion["overall_risk"].strip().lower()
    print(f"  Coincide  : {'DA' if match else 'NU'}\n")

    print("--- Sectiuni ---")
    ref_keys = set(reference["sections"].keys())
    gen_keys = set(gen_by_section.keys())
    common = sorted(ref_keys & gen_keys)
    ref_only = sorted(k for k in ref_keys - gen_keys if SECTION_MAP.get(k) is not None or True)
    gen_only = sorted(gen_keys - ref_keys)

    for key in common:
        ref_grade = (reference["sections"][key]["grade"] or "(neparsat)")
        gen_grade = gen_by_section[key]["grade"]
        same = ref_grade.strip().lower() == gen_grade.strip().lower()
        print(f"  [{'OK ' if same else 'DIF'}] {key}: referinta='{ref_grade}' vs generat='{gen_grade}'")

    if ref_only:
        print("\n  Sectiuni doar in referinta (nu exista in KO engine ca si categorie gradata in acest MVP):")
        for k in ref_only:
            print(f"    - {k} (grad referinta: {reference['sections'][k]['grade']})")

    if gen_only:
        print("\n  Sectiuni doar in output-ul generat:")
        for k in gen_only:
            print(f"    - {k}")

    print("\n--- Campuri lipsa in opinia generata ---")
    for m in gen_opinion["missing_fields"][:20]:
        print(f"  - {m}")
    if len(gen_opinion["missing_fields"]) > 20:
        print(f"  ... si inca {len(gen_opinion['missing_fields']) - 20} campuri")

    print(
        "\nNota: aceasta comparatie este strict sintactica (potrivire de nume sectiuni si "
        "text de grad). Nu evalueaza calitatea semantica a mentiunilor generate - acel pas "
        "ramane manual, in sarcina ofiterului de risc."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
