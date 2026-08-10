#!/usr/bin/env python
"""
CLI entry point for the risk opinion MVP.

Usage:
    python main.py --list-companies
    python main.py --company "BEO TRADE COM SRL"
    python main.py --company "BEO TRADE COM SRL" --no-llm
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import document_reader, extractor, ko_engine, opinion_generator
from src.azure_llm import AzureLLM, LLMNotConfigured
from src.utils import StepLogger

DATA_ROOT = Path(__file__).resolve().parent / "data" / "opinie_risc"
OUTPUT_ROOT = Path(__file__).resolve().parent / "generated"


def cmd_list_companies() -> int:
    companies = document_reader.list_companies(DATA_ROOT)
    if not companies:
        print(f"Niciun folder de companie gasit in {DATA_ROOT}")
        return 1
    print("Companii disponibile:")
    for c in companies:
        print(f"  - {c}")
    return 0


def cmd_generate(company: str, use_llm: bool) -> int:
    logger = StepLogger(total=5)

    company_dir = document_reader.find_company_dir(DATA_ROOT, company)
    if company_dir is None:
        print(f"Compania '{company}' nu a fost gasita in {DATA_ROOT}.", file=sys.stderr)
        print("Foloseste --list-companies pentru lista disponibila.", file=sys.stderr)
        return 1

    llm: AzureLLM | None = None
    if use_llm:
        try:
            llm = AzureLLM()
        except LLMNotConfigured as e:
            print(f"EROARE configurare Azure OpenAI: {e}", file=sys.stderr)
            return 1

    logger.step("Reading documents...")
    documents = document_reader.discover_company_documents(company_dir, include_output=False)
    for d in documents:
        logger.info(f"{d.filename} ({d.doc_type}) - {d.char_count} chars/cells" + (f" [!] {'; '.join(d.warnings)}" if d.warnings else ""))
    if not documents:
        logger.warn("Niciun document INPUT gasit pentru aceasta companie.")

    logger.step("Extracting structured data with GPT-5-mini..." if use_llm else "Extracting structured data (--no-llm: skipped)...")
    data, deterministic = extractor.extract_company_data(company_dir.name, documents, llm, logger)
    logger.info(f"Camp solvabilitate calculat determinist: {deterministic.get('solvency_pct')}")

    logger.step("Applying KO rules...")
    ko = ko_engine.apply_ko_rules(data, deterministic)
    for cat in ko.categories:
        logger.info(f"{cat.category}: {cat.status.value} grad={cat.risk_level} completitudine={cat.completeness:.0%}")
    logger.info(f"Risc general: {ko.overall_risk_level} (completitudine {ko.overall_completeness:.0%})")

    logger.step("Generating risk opinion...")
    opinion = opinion_generator.generate_opinion(company_dir.name, data, ko, llm, logger)

    logger.step("Saving output...")
    out_dir = opinion_generator.save_outputs(company_dir.name, data, ko, opinion, OUTPUT_ROOT)

    print(f"\nOpinie generata cu succes pentru: {company_dir.name}")
    print(f"  -> {out_dir / 'opinion.json'}")
    print(f"  -> {out_dir / 'opinion.md'}")
    print(f"  -> {out_dir / 'opinion.html'}")
    print(f"  -> {out_dir / 'opinion.docx'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Generator automat de opinii de risc Corporate (MVP).")
    parser.add_argument("--company", help="Numele companiei (folder in data/opinie_risc).")
    parser.add_argument("--list-companies", action="store_true", help="Listeaza companiile disponibile.")
    parser.add_argument("--no-llm", action="store_true", help="Ruleaza fara apeluri catre Azure OpenAI (testare parsere/KO).")
    args = parser.parse_args()

    if args.list_companies:
        return cmd_list_companies()

    if not args.company:
        parser.print_help()
        return 1

    return cmd_generate(args.company, use_llm=not args.no_llm)


if __name__ == "__main__":
    raise SystemExit(main())
