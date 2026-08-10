"""
Extraction stage: turns RawDocuments into a CompanyRiskData object.

Two extraction paths are combined, per the architecture principle in the spec:

  * GPT-5-mini reads the narrative/PDF content and the rendered spreadsheet
    tables, and extracts discrete facts + evidence (never computes ratios,
    never applies KO thresholds).
  * A handful of purely numeric figures that are safe and unambiguous to read
    directly out of the standardized "Indicatori financiari" sheet (e.g. the
    solvency ratio) are pulled deterministically in Python and merged in,
    overriding any LLM guess for that specific field. This keeps arithmetic
    out of the LLM's hands, per the "Python calculeaza" principle.
"""
from __future__ import annotations

import datetime as _dt

from .azure_llm import AzureLLM
from .models import CompanyRiskData
from .utils import StepLogger

MAX_CHARS_PER_DOC = 20000
MAX_SHEET_ROWS = 250

SYSTEM_PROMPT = """\
Esti un asistent care extrage informatii FACTUALE din documentele unei companii \
pentru o opinie de risc bancara de tip Corporate. NU esti cel care decide riscul.

Reguli OBLIGATORII:
1. Extrage DOAR informatii care apar explicit in documentele furnizate.
2. Daca o informatie nu este clar prezenta, foloseste null. Nu ghici, nu \
   completa cu 0, nu interpreta "-" ca fiind 0 decat daca textul o spune explicit.
3. NU calcula tu insuti praguri, scoruri sau grade de risc (scazut/mediu/ridicat) - \
   asta este responsabilitatea unui motor Python separat.
4. Pentru fiecare informatie importanta (financiara, CRC, ANAF, popriri, CIP, \
   rating, colateral, procese, insolventa, AML), adauga o intrare in "evidence" \
   cu numele documentului sursa (camp "source_document", foloseste exact numele \
   fisierului asa cum apare in antetul sectiunii din text) si un fragment scurt \
   de context (camp "excerpt").
5. Daca gasesti un camp care in mod normal provine din sisteme interne banca \
   (IBS/Flow) si nu il gasesti in documente, adauga-l explicit in \
   "missing_information" cu o nota ca provine probabil din IBS/Flow.
6. Raspunde in limba romana pentru campurile descriptive (activity_description, \
   company_description etc).
7. O valoare 0 sau 0.00 scrisa EXPLICIT in document (de ex. "DATORII LA STAT: 0.00", \
   "Nr. incidente: 0") este o data REALA si trebuie extrasa ca 0, nu ca null - nu este \
   acelasi lucru cu un camp gol sau cu "-". Foloseste null DOAR cand informatia chiar \
   lipseste din document.
8. Pentru registration_date (data infiintarii/inmatricularii companiei) foloseste DOAR \
   mentiuni explicite de tipul "infiintata in ...", "data infiintarii", "inmatriculata la ...", \
   sau anul din "Nr. Reg. Com." (formatul romanesc este J[judet]/[numar]/[AN infiintare], \
   ex. "J4/1224/2022" -> anul 2022; daca cifrele sunt lipite fara "/", cauta un grup de 4 \
   cifre plauzibil ca an, ex. "J2016000848351" -> 2016). Daca gasesti doar anul (nu si \
   luna/ziua), foloseste 01.01 din acel an (ex. "2016-01-01") si mentioneaza in excerpt ca \
   e aproximat la inceputul anului. Daca ai doar luna si anul, foloseste ziua 01.
   ATENTIE: campul "Data ultimei inreg." din verificarile CRC este data ULTIMEI modificari \
   in registrul comertului (schimbare sediu, administrator etc.), NU data infiintarii - \
   NU il folosi niciodata pentru registration_date.
9. Documentul "verificari CRC" foloseste un format standardizat; cauta explicit aceste \
   etichete atunci cand completezi campurile de mai jos (sunt insotite de valori sub \
   forma de tabel, uneori pe randuri separate de eticheta):
   - "DATORII LA STAT, [COMPANIE ANALIZATA]" / "TOTAL" de sub acea sectiune -> anaf_debt_amount
   - "DATORII LA STAT, [GRUP]" -> anaf_debt_group_amount
   - ATENTIE: langa "DATORII LA STAT" poate exista si o mentiune separata in referat de \
     tipul "Decizia de esalonare din [data] pentru suma de [X] ron pe o perioada de [N] luni" \
     - aceasta e o suma DIFERITA (planul de reesalonare, de regula mai mare, acopera o \
     perioada viitoare), NU acelasi lucru cu totalul "DATORII LA STAT" (care e o poza la o \
     data fixa). Extrage in anaf_debt_amount valoarea din tabelul "DATORII LA STAT, \
     [COMPANIE ANALIZATA]" (nu suma din decizia de esalonare), dar mentioneaza si decizia \
     de esalonare (data, suma, perioada) in payment_history_notes, ca informatie separata.
   - "POPRIRI, [COMPANIE ANALIZATA]" (randuri goale/fara nume = nicio poprire; randuri cu \
     nume/suma = poprire prezenta) -> popriri_present, popriri_details
   - "POPRIRI, [GRUP]" (acelasi principiu, pt entitatile din grup) -> popriri_group_present, \
     popriri_group_details
   - "REZULTAT CIP [COMPANIE ANALIZATA]": daca "INCIDENTE PLATA MAJORE" SAU "INCIDENTE \
     PLATA MINORE" (in ultimii 2 ani sau mai vechi) au Nr. incidente/Total instrumente > 0 \
     pt solicitant -> cip_incident_present=true, cip_details=detaliile (data, tip, suma); \
     daca toate sunt 0 -> cip_incident_present=false.
   - "REZULTAT CIP [GRUP]": acelasi principiu, dar pentru entitatile din grup (verifica \
     TOATE sectiunile - majore/minore, in ultimii 2 ani/mai vechi) -> \
     cip_incident_group_present, cip_group_details (mentioneaza si numele entitatii din \
     grup la care apare incidentul).
   - "SCOR CRC" (nu "fara scor") -> crc_score; sectiunea grupului -> crc_score_group
   - "CAPACITATE DE RAMBURSARE, [COMPANIE ANALIZATA]": cauta valorile pentru "EBITDA \
     anualizata compania analizata" -> ebitda_current, "Rate totale companie analizata" \
     -> installments_total_current, "EbitdaGroup"/"Rate totale grup" -> ebitda_group / \
     installments_total_group (grupul poate exclude sau include PF - alege varianta grup \
     completa daca ambele apar).
   - "Rating:" (langa datele companiei) -> rating
10. Pentru legal_processes_present: true daca este mentionat ORICE dosar/proces (civil, \
    penal, comercial) pentru solicitant, asociati, administratori, fideiusori sau companii \
    din grup (de ex. sectiunea "Just", "Portal Just", "Nr proces selectat"); false daca \
    documentul afirma explicit ca nu a fost identificat niciun dosar; null daca nu e clar. \
    Descrie pe scurt in legal_processes despre ce e vorba (parti, obiect, stadiu, rezultat).
11. Pentru aml_risk_level_stated: cauta in documente (de regula in referat, sectiunea \
    despre "risc tranzactional"/"matrice AML"/conformitate) un nivel de risc DEJA declarat \
    de banca (cuvintele "scazut", "mediu" si/sau "ridicat" - uneori compus, ex. \
    "mediu-scazut"). Copiaza EXACT cuvantul/cuvintele gasite in acest camp (nu interpreta, \
    nu calcula) - acesta este rezultatul unei matrici AML deja aplicate, nu o cifra pe care \
    o determini tu. Pastreaza si textul complet original in aml_risk_statement.
"""


def render_sheet_as_text(sheet_name: str, rows: list[list], max_rows: int = MAX_SHEET_ROWS) -> str:
    lines = [f"[Sheet: {sheet_name}]"]
    for row in rows[:max_rows]:
        cells = []
        for v in row:
            if v is None:
                continue
            if isinstance(v, float):
                cells.append(f"{v:,.2f}")
            else:
                cells.append(str(v))
        if cells:
            lines.append(" | ".join(cells))
    if len(rows) > max_rows:
        lines.append(f"... ({len(rows) - max_rows} more rows truncated)")
    return "\n".join(lines)


def build_document_corpus(documents) -> str:
    parts = []
    for doc in documents:
        parts.append(f"\n===== DOCUMENT: {doc.filename} (tip: {doc.doc_type}) =====")
        if doc.text:
            text = doc.text
            if len(text) > MAX_CHARS_PER_DOC:
                text = text[:MAX_CHARS_PER_DOC] + "\n... (trunchiat)"
            parts.append(text)
        if doc.sheets:
            for sheet_name, rows in doc.sheets.items():
                parts.append(render_sheet_as_text(sheet_name, rows))
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Deterministic figures read directly from the standardized financial sheet.
# --------------------------------------------------------------------------- #
def _find_header_dates(rows: list[list]) -> dict[int, str]:
    """Returns {column_index: iso_date_string} for the nearest row above containing dates."""
    for row in rows:
        dated_cols = {i: v for i, v in enumerate(row) if isinstance(v, _dt.datetime)}
        if len(dated_cols) >= 2:
            return {i: v.date().isoformat() for i, v in dated_cols.items()}
    return {}


def find_latest_ratio(sheets: dict[str, list[list]], label_keywords: list[str]) -> dict | None:
    """
    Searches all sheets for a row whose first two cells match any of label_keywords
    (case-insensitive substring), then returns the rightmost numeric value on that
    row together with its period date (read from the nearest date header row above),
    the sheet name and the row label - for evidence/traceability.
    """
    for sheet_name, rows in sheets.items():
        date_header = _find_header_dates(rows)
        for row in rows:
            label_cells = [str(c) for c in row[:2] if isinstance(c, str)]
            label = " ".join(label_cells).lower()
            if not label:
                continue
            if any(kw.lower() in label for kw in label_keywords):
                for col_idx in range(len(row) - 1, -1, -1):
                    v = row[col_idx]
                    if isinstance(v, (int, float)):
                        return {
                            "value": float(v),
                            "sheet": sheet_name,
                            "row_label": label_cells[-1] if label_cells else label,
                            "period_date": date_header.get(col_idx),
                        }
    return None


def compute_deterministic_financials(documents) -> dict:
    """Currently extracts: solvency ratio (Equity Ratio = Equity/Total Assets)."""
    result: dict = {}
    for doc in documents:
        if doc.doc_type != "financial_xlsm" or not doc.sheets:
            continue
        solvency = find_latest_ratio(doc.sheets, ["equity ratio"])
        if solvency:
            result["solvency_pct"] = solvency
        break  # only the solicitant's own financial workbook, not the group one
    return result


def extract_company_data(
    company_name: str,
    documents: list,
    llm: AzureLLM | None,
    logger: StepLogger | None = None,
) -> tuple[CompanyRiskData, dict]:
    """Returns (extracted_data, deterministic_financials)."""
    deterministic = compute_deterministic_financials(documents)

    if llm is None:
        data = CompanyRiskData(
            company_name=company_name,
            missing_information=["Extragere GPT dezactivata (--no-llm): toate campurile extrase din text sunt goale."],
        )
        return data, deterministic

    corpus = build_document_corpus(documents)
    user_prompt = (
        f"Compania analizata: {company_name}\n\n"
        f"Documente disponibile (referat, verificari CRC, analiza financiara):\n{corpus}"
    )
    if logger:
        logger.info(f"Sending ~{len(user_prompt):,} chars of document text to GPT-5-mini for extraction.")
    data = llm.extract_structured(SYSTEM_PROMPT, user_prompt, CompanyRiskData)
    return data, deterministic
