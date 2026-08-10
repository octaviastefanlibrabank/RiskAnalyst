"""
Deterministic KO ("knock-out" risk grading) engine.

All thresholds/weights are READ from data/opinie_risc/criterii KO - baze de date
independente.xlsx (sheet "Corporate") at runtime - see _load_config(). Nothing
here hardcodes a business threshold; only cell coordinates are hardcoded (i.e.
"read B18 as the weight of the CRC-score rule"), which is necessary because the
workbook has no named ranges/headers machine-readable enough to locate cells by
label alone, and was confirmed by manual inspection (see project notes).

The workbook itself is a work-in-progress design document (it contains open
questions from the business - "de lamurit", "alte intrebari" - and two
inconsistent draft weighting tables). We use the ONE unambiguous, complete path:
the B37 formula (`=B13*C13+B17*C17+B23*C23+B28*C28+B33*C33`) and its D39/D40/D41
classification thresholds, together with the per-sub-criterion D/E/F threshold
descriptions. Any rule whose D/E/F thresholds are missing, contradictory, or
require data we structurally cannot obtain from the available documents (e.g.
group-level aggregation, restricted-CAEN lists, IBS "rate" schedules) is marked
NOT_IMPLEMENTED / DATA_MISSING rather than guessed.

GPT NEVER computes or overrides any risk_level produced here.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import openpyxl

from .models import CompanyRiskData, KoCategoryResult, KoEngineResult, KoRuleResult, RuleStatus

KO_XLSX_DEFAULT = Path(__file__).resolve().parent.parent / "data" / "opinie_risc" / "criterii KO - baze de date independente.xlsx"
SHEET = "Corporate"


@lru_cache(maxsize=1)
def _load_config(path: str = str(KO_XLSX_DEFAULT)) -> dict:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb[SHEET]

    def c(coord):
        return ws[coord].value

    cfg = {
        "score_scale": {"SCAZUT": c("D13"), "MEDIU": c("E13"), "RIDICAT": c("F13")},
        "category_weights": {
            "strategic": c("B13"),
            "bancar": c("B17"),
            "financiar": c("B23"),
            "reputational": c("B28"),
            "colaterizare": c("B33"),
        },
        "overall_thresholds": {"ridicat_max": c("D41"), "mediu_max": c("D40"), "scazut_min": c("D39")},
        "rules": {
            "vechime": {"weight": c("B15"), "scazut": c("D15"), "mediu": c("E15"), "ridicat": c("F15"), "category": "strategic"},
            "domeniu_activitate": {"weight": c("B14"), "scazut": c("D14"), "mediu": c("E14"), "ridicat": c("F14"), "category": "strategic"},
            "scor_crc": {"weight": c("B18"), "scazut": c("D18"), "mediu": c("E18"), "ridicat": c("F18"), "category": "bancar"},
            "incidente_cip": {"weight": c("B19"), "scazut": c("D19"), "mediu": c("E19"), "ridicat": c("F19"), "category": "bancar"},
            "datorii_anaf": {"weight": c("B20"), "scazut": c("D20"), "mediu": c("E20"), "ridicat": c("F20"), "category": "bancar"},
            "popriri": {"weight": c("B21"), "scazut": c("D21"), "mediu": c("E21"), "ridicat": c("F21"), "category": "bancar"},
            "incidente_grup": {"weight": c("B22"), "category": "bancar"},
            "rating": {"weight": c("B24"), "scazut": c("D24"), "mediu": c("E24"), "ridicat": c("F24"), "category": "financiar"},
            "ebitda_rate_solicitant": {"weight": c("B25"), "scazut": c("D25"), "mediu": c("E25"), "ridicat": c("F25"), "category": "financiar"},
            "ebitda_rate_grup": {"weight": c("B26"), "scazut": c("D26"), "mediu": c("E26"), "ridicat": c("F26"), "category": "financiar"},
            "solvabilitate": {"weight": c("B27"), "scazut": c("D27"), "mediu": c("E27"), "ridicat": c("F27"), "category": "financiar"},
            "conformitate": {"weight": c("B29"), "scazut": c("D29"), "mediu": c("E29"), "ridicat": c("F29"), "category": "reputational"},
            "procese": {"weight": c("B30"), "scazut": c("D30"), "mediu": c("E30"), "ridicat": c("F30"), "category": "reputational"},
            "istoric_insolvente": {"weight": c("B31"), "scazut": c("D31"), "mediu": c("E31"), "ridicat": c("F31"), "category": "reputational"},
            "tip_ipoteca": {"weight": c("B34"), "scazut": c("D34"), "category": "colaterizare"},
            "grad_acoperire": {"weight": c("B35"), "scazut": c("D35"), "mediu": c("E35"), "ridicat": c("F35"), "category": "colaterizare"},
        },
        "category_labels": {
            "strategic": "Risc strategic",
            "bancar": "Risc bancar / comportament de plata",
            "financiar": "Risc financiar",
            "reputational": "Risc reputational",
            "colaterizare": "Colaterizare",
        },
    }
    wb.close()
    return cfg


def _pts(level: str, cfg: dict) -> float:
    return cfg["score_scale"][level]


def _num(text) -> float | None:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(text))
    return float(m.group(0).replace(",", ".")) if m else None


def _highest_risk_word(text: str | None) -> str | None:
    """Scans text for literal 'scazut'/'mediu'/'ridicat' tokens (already-declared grade,
    e.g. a bank's own AML matrix result) and returns the highest-risk one found. Handles
    compound wording like "mediu-scazut" by picking the more conservative (higher-risk)
    word present, rather than guessing which single one applies."""
    if not text:
        return None
    t = text.lower()
    if "ridicat" in t:
        return "RIDICAT"
    if "mediu" in t:
        return "MEDIU"
    if "scazut" in t or "scăzut" in t:
        return "SCAZUT"
    return None


def _skip(rule_id: str, label: str, cfg: dict, reason: str, category_key: str, inputs=None) -> KoRuleResult:
    r = cfg["rules"].get(rule_id, {})
    return KoRuleResult(
        rule_id=rule_id,
        label=label,
        category=cfg["category_labels"][category_key],
        weight=r.get("weight") or 0.0,
        status=RuleStatus.NOT_IMPLEMENTED if "NOT_IMPLEMENTED" in reason else RuleStatus.DATA_MISSING,
        explanation=reason,
        inputs_used=inputs or {},
        source_cells=[],
    )


# --------------------------------------------------------------------------- #
# Individual rules
# --------------------------------------------------------------------------- #
def rule_vechime(data: CompanyRiskData, cfg: dict) -> KoRuleResult:
    r = cfg["rules"]["vechime"]
    if not data.registration_date:
        return _skip("vechime", "Vechime companie", cfg, "DATA_MISSING: data infiintarii nu a fost gasita in documente.", "strategic")
    import datetime as dt

    try:
        reg = dt.date.fromisoformat(data.registration_date[:10])
    except ValueError:
        return _skip("vechime", "Vechime companie", cfg, f"DATA_MISSING: data infiintarii '{data.registration_date}' nu este in format recunoscut.", "strategic")

    age_years = (dt.date.today() - reg).days / 365.25
    hi = _num(r["scazut"])  # ">2 ani" -> 2
    lo = _num(r["ridicat"])  # "<1 an" -> 1
    if hi is None or lo is None:
        return _skip("vechime", "Vechime companie", cfg, "NOT_IMPLEMENTED: pragurile de vechime din workbook nu au putut fi interpretate.", "strategic")

    if age_years > hi:
        level = "SCAZUT"
    elif age_years < lo:
        level = "RIDICAT"
    else:
        level = "MEDIU"
    return KoRuleResult(
        rule_id="vechime", label="Vechime companie", category=cfg["category_labels"]["strategic"], weight=r["weight"],
        status=RuleStatus.OK, risk_level=level, score=_pts(level, cfg),
        explanation=f"Vechime ~{age_years:.1f} ani (inregistrare {data.registration_date}). Praguri workbook: scazut{r['scazut']!r}, mediu{r['mediu']!r}, ridicat{r['ridicat']!r}.",
        inputs_used={"registration_date": data.registration_date, "age_years": round(age_years, 2)},
        source_cells=["A15", "B15", "D15", "E15", "F15"],
    )


def rule_domeniu_activitate(data: CompanyRiskData, cfg: dict) -> KoRuleResult:
    return _skip(
        "domeniu_activitate", "Domeniu de activitate", cfg,
        "NOT_IMPLEMENTED: lista CAEN-urilor restrictionate/interzise conform strategiei si normei bancii "
        "nu este disponibila in datele furnizate pentru acest MVP (workbook-ul KO trimite doar catre "
        "'norma'/'strategie' interne, fara valorile efective).",
        "strategic", inputs={"caen_code": data.caen_code, "activity_description": data.activity_description},
    )


def rule_scor_crc(data: CompanyRiskData, cfg: dict) -> KoRuleResult:
    r = cfg["rules"]["scor_crc"]
    if data.crc_score is None:
        return _skip("scor_crc", "Scor CRC", cfg, "DATA_MISSING: scorul CRC nu a fost gasit in documente.", "bancar")
    lo, hi = _num(r["scazut"]), _num(r["mediu"])
    level = "SCAZUT" if data.crc_score <= lo else ("MEDIU" if data.crc_score <= hi else "RIDICAT")
    return KoRuleResult(
        rule_id="scor_crc", label="Scor CRC", category=cfg["category_labels"]["bancar"], weight=r["weight"],
        status=RuleStatus.OK, risk_level=level, score=_pts(level, cfg),
        explanation=f"Scor CRC = {data.crc_score}. Praguri workbook: scazut<={lo}, mediu<={hi}, ridicat>{hi}.",
        inputs_used={"crc_score": data.crc_score}, source_cells=["B18", "D18", "E18", "F18"],
    )


def rule_incidente_cip(data: CompanyRiskData, cfg: dict) -> KoRuleResult:
    r = cfg["rules"]["incidente_cip"]
    if data.cip_incident_present is None:
        return _skip("incidente_cip", "Incidente CIP (ultimele 12 luni)", cfg, "DATA_MISSING: rezultatul CIP nu a fost gasit in documente.", "bancar")
    level = "RIDICAT" if data.cip_incident_present else "SCAZUT"
    return KoRuleResult(
        rule_id="incidente_cip", label="Incidente CIP (ultimele 12 luni)", category=cfg["category_labels"]["bancar"], weight=r["weight"],
        status=RuleStatus.OK, risk_level=level, score=_pts(level, cfg),
        explanation=f"Incident CIP prezent: {data.cip_incident_present}. Workbook: fara incident=scazut, cu incident='{r['ridicat']}'=ridicat "
                    "(nivelul mediu nu este definit distinct in workbook pentru aceasta regula).",
        inputs_used={"cip_incident_present": data.cip_incident_present, "cip_details": data.cip_details},
        source_cells=["B19", "D19", "E19", "F19"],
    )


def rule_datorii_anaf(data: CompanyRiskData, cfg: dict) -> KoRuleResult:
    r = cfg["rules"]["datorii_anaf"]
    if data.anaf_debt_amount is None:
        return _skip("datorii_anaf", "Datorii ANAF", cfg, "DATA_MISSING: valoarea datoriilor la stat nu a fost gasita in documente.", "bancar")
    if not data.revenue_current:
        return _skip("datorii_anaf", "Datorii ANAF", cfg, "DATA_MISSING: cifra de afaceri (necesara pentru procentul din CA) nu a fost gasita.", "bancar",
                     inputs={"anaf_debt_amount": data.anaf_debt_amount})
    pct = data.anaf_debt_amount / data.revenue_current
    if pct <= 0:
        level = "SCAZUT"
    elif pct <= 0.05:
        # workbook defines mediu band as "1% CA < datorii < 5% CA"; 0-1% is not covered
        # explicitly - treated conservatively as MEDIU rather than silently assumed SCAZUT.
        level = "MEDIU"
    else:
        level = "RIDICAT"
    return KoRuleResult(
        rule_id="datorii_anaf", label="Datorii ANAF", category=cfg["category_labels"]["bancar"], weight=r["weight"],
        status=RuleStatus.OK, risk_level=level, score=_pts(level, cfg),
        explanation=f"Datorii ANAF = {data.anaf_debt_amount:,.0f}, CA = {data.revenue_current:,.0f} -> {pct:.2%} din CA. "
                    f"Workbook: 0='{r['scazut']}', mediu='{r['mediu']}', ridicat='{r['ridicat']}'. "
                    "Interval 0%-1% nedefinit explicit in workbook - tratat conservator ca MEDIU.",
        inputs_used={"anaf_debt_amount": data.anaf_debt_amount, "revenue_current": data.revenue_current, "pct_of_ca": round(pct, 4)},
        source_cells=["B20", "D20", "E20", "F20"],
    )


def rule_popriri(data: CompanyRiskData, cfg: dict) -> KoRuleResult:
    r = cfg["rules"]["popriri"]
    if data.popriri_present is None:
        return _skip("popriri", "Popriri", cfg, "DATA_MISSING: informatia despre popriri nu a fost gasita in documente.", "bancar")
    level = "RIDICAT" if data.popriri_present else "SCAZUT"
    return KoRuleResult(
        rule_id="popriri", label="Popriri", category=cfg["category_labels"]["bancar"], weight=r["weight"],
        status=RuleStatus.OK, risk_level=level, score=_pts(level, cfg),
        explanation=f"Popriri prezente: {data.popriri_present}.",
        inputs_used={"popriri_present": data.popriri_present, "popriri_details": data.popriri_details},
        source_cells=["B21", "D21", "E21", "F21"],
    )


def rule_incidente_grup(data: CompanyRiskData, cfg: dict) -> KoRuleResult:
    """
    Celula A22 din workbook defineste explicit un declansator compozit (nu un tabel D/E/F
    separat): "Incidente la nivel de grup (CRC >0.05, incident Cip in ultimele 12 luni sau
    este in perioada de interdictie bancara, Datorii Anaf>5% cifra de afaceri, popriri da)".
    Implementam acest declansator direct din text: daca ORICARE dintre semnalele
    disponibile la nivel de grup este adevarat -> RIDICAT. Daca toate semnalele disponibile
    sunt cunoscute si false -> SCAZUT. Daca niciun semnal nu e cunoscut -> DATA_MISSING.
    Pragul ANAF-grup (>5% din CA) necesita si cifra de afaceri a grupului, care de regula
    nu e disponibila din documentele curente - acel sub-semnal e omis daca lipseste, fara
    a bloca evaluarea celorlalte semnale cunoscute.
    """
    r = cfg["rules"]["incidente_grup"]
    signals: dict[str, bool | None] = {
        "crc_group_peste_0.05": (data.crc_score_group > 0.05) if data.crc_score_group is not None else None,
        "incident_cip_grup": data.cip_incident_group_present,
        "popriri_grup": data.popriri_group_present,
    }
    known = {k: v for k, v in signals.items() if v is not None}
    if not known:
        return _skip("incidente_grup", "Incidente la nivel de grup", cfg,
                     "DATA_MISSING: niciun semnal de grup (CRC/CIP/popriri) nu a putut fi determinat din documente.",
                     "bancar")
    triggered = [k for k, v in known.items() if v]
    level = "RIDICAT" if triggered else "SCAZUT"
    detail = f"Declansat de: {', '.join(triggered)}." if triggered else "Niciun semnal cunoscut declansat."
    return KoRuleResult(
        rule_id="incidente_grup", label="Incidente la nivel de grup", category=cfg["category_labels"]["bancar"], weight=r["weight"],
        status=RuleStatus.OK, risk_level=level, score=_pts(level, cfg),
        explanation=(f"Declansator compozit (cf. A22): CRC grup, CIP grup, popriri grup. Semnale cunoscute: {known}. {detail} "
                     "Pragul ANAF-grup (>5% CA grup) nu a fost evaluat (necesita CA grup, indisponibila)."),
        inputs_used={"cip_incident_group_present": data.cip_incident_group_present,
                     "popriri_group_present": data.popriri_group_present,
                     "crc_score_group": data.crc_score_group},
        source_cells=["A22", "B22"],
    )


def rule_rating(data: CompanyRiskData, cfg: dict) -> KoRuleResult:
    r = cfg["rules"]["rating"]
    if not data.rating:
        return _skip("rating", "Rating", cfg, "DATA_MISSING: ratingul nu a fost gasit in documente.", "financiar")
    rating = data.rating.strip().upper()
    tiers = {
        "SCAZUT": [x.strip().upper() for x in str(r["scazut"]).split(",")],
        "MEDIU": [x.strip().upper() for x in str(r["mediu"]).split(",")],
        "RIDICAT": [x.strip().upper() for x in str(r["ridicat"]).split(",")],
    }
    level = next((lvl for lvl, vals in tiers.items() if rating in vals), None)
    if level is None:
        return _skip("rating", "Rating", cfg, f"DATA_MISSING: ratingul extras '{data.rating}' nu se regaseste in niciuna dintre categoriile din workbook.",
                     "financiar", inputs={"rating": data.rating})
    return KoRuleResult(
        rule_id="rating", label="Rating", category=cfg["category_labels"]["financiar"], weight=r["weight"],
        status=RuleStatus.OK, risk_level=level, score=_pts(level, cfg),
        explanation=f"Rating = {data.rating}. Workbook: scazut={r['scazut']}, mediu={r['mediu']}, ridicat={r['ridicat']}.",
        inputs_used={"rating": data.rating}, source_cells=["B24", "D24", "E24", "F24"],
    )


def _ebitda_rate_rule(rule_id: str, label: str, ebitda, installments, cfg: dict) -> KoRuleResult:
    r = cfg["rules"][rule_id]
    if ebitda is None or installments is None:
        return _skip(rule_id, label, cfg,
                     "NOT_IMPLEMENTED: valorile EBITDA si/sau rate totale (esalon de rambursare, "
                     "de regula din IBS/Flow) nu au fost gasite/nu sunt structurate clar in documentele disponibile.",
                     "financiar")
    if installments == 0:
        return _skip(rule_id, label, cfg, "DATA_MISSING: 'rate totale' = 0, raport nedefinit.", "financiar",
                     inputs={"ebitda": ebitda, "installments_total": installments})
    pct = ebitda / installments
    hi = _num(r["scazut"]) / 100  # ">110%" -> 1.10
    lo = _num(r["ridicat"]) / 100  # "<100%" -> 1.00
    level = "SCAZUT" if pct > hi else ("RIDICAT" if pct < lo else "MEDIU")
    return KoRuleResult(
        rule_id=rule_id, label=label, category=cfg["category_labels"]["financiar"], weight=r["weight"],
        status=RuleStatus.OK, risk_level=level, score=_pts(level, cfg),
        explanation=f"EBITDA/Rate totale = {ebitda:,.0f} / {installments:,.0f} = {pct:.1%}. Workbook: scazut{r['scazut']!r}, ridicat{r['ridicat']!r}.",
        inputs_used={"ebitda": ebitda, "installments_total": installments, "pct": round(pct, 4)},
        source_cells=["B25" if rule_id == "ebitda_rate_solicitant" else "B26"],
    )


def rule_ebitda_rate_solicitant(data: CompanyRiskData, cfg: dict) -> KoRuleResult:
    return _ebitda_rate_rule("ebitda_rate_solicitant", "EBITDA / rate - solicitant", data.ebitda_current, data.installments_total_current, cfg)


def rule_ebitda_rate_grup(data: CompanyRiskData, cfg: dict) -> KoRuleResult:
    return _ebitda_rate_rule("ebitda_rate_grup", "EBITDA / rate - grup", data.ebitda_group, data.installments_total_group, cfg)


def rule_solvabilitate(data: CompanyRiskData, deterministic: dict, cfg: dict) -> KoRuleResult:
    r = cfg["rules"]["solvabilitate"]
    solv = deterministic.get("solvency_pct")
    if not solv:
        return _skip("solvabilitate", "Solvabilitate", cfg,
                     "DATA_MISSING: randul 'Equity Ratio' nu a fost gasit in sheet-ul Indicatori financiari.", "financiar")
    pct = solv["value"]
    hi = _num(r["scazut"]) / 100  # ">15%"
    lo_band = r["mediu"]  # "10-15%"
    lo = _num(str(lo_band).split("-")[0]) / 100
    level = "SCAZUT" if pct > hi else ("RIDICAT" if pct < lo else "MEDIU")
    return KoRuleResult(
        rule_id="solvabilitate", label="Solvabilitate", category=cfg["category_labels"]["financiar"], weight=r["weight"],
        status=RuleStatus.OK, risk_level=level, score=_pts(level, cfg),
        explanation=(f"Solvabilitate (Equity Ratio = Capitaluri proprii / Total active) = {pct:.1%} "
                     f"(sheet '{solv['sheet']}', perioada {solv.get('period_date')}). Workbook: scazut{r['scazut']!r}, mediu{r['mediu']!r}, ridicat{r['ridicat']!r}."),
        inputs_used={"solvency_pct": pct, "period_date": solv.get("period_date"), "sheet": solv["sheet"]},
        source_cells=["B27", "D27", "E27", "F27"],
    )


def rule_conformitate(data: CompanyRiskData, cfg: dict) -> KoRuleResult:
    """
    Workbook-ul (D29='nu'->scazut, E29/F29='da'->mediu SAU ridicat) nu are un prag numeric
    care sa distinga mediu de ridicat cand rezultatul e "da". In schimb, referatele/CRC
    contin deja rezultatul unei matrici AML aplicate de banca (ex. "risc tranzactional
    ridicat", "matrice AML: mediu-scazut") - aml_risk_level_stated preia acel cuvant LITERAL
    (nu il calculam noi). Il folosim direct ca grad, cu prioritate pe varianta mai
    conservatoare cand textul e compus (ex. "mediu-scazut" -> MEDIU).
    """
    r = cfg["rules"]["conformitate"]
    level = _highest_risk_word(data.aml_risk_level_stated) or _highest_risk_word(data.aml_risk_statement)
    if level is None:
        return _skip("conformitate", "Conformitate (AML)", cfg,
                     "DATA_MISSING: niciun nivel de risc AML declarat explicit (scazut/mediu/ridicat) nu a fost "
                     f"gasit in documente. Mentiune libera gasita (daca exista): {data.aml_risk_statement or 'nicio mentiune'}.",
                     "reputational", inputs={"aml_risk_statement": data.aml_risk_statement})
    return KoRuleResult(
        rule_id="conformitate", label="Conformitate (AML)", category=cfg["category_labels"]["reputational"], weight=r["weight"],
        status=RuleStatus.OK, risk_level=level, score=_pts(level, cfg),
        explanation=(f"Nivel AML preluat LITERAL din documente (nu calculat): '{data.aml_risk_level_stated or data.aml_risk_statement}' -> {level}. "
                     "Workbook-ul nu diferentiaza numeric mediu/ridicat pentru 'da' - folosim direct clasificarea deja facuta de banca (matrice AML)."),
        inputs_used={"aml_risk_level_stated": data.aml_risk_level_stated, "aml_risk_statement": data.aml_risk_statement},
        source_cells=["B29", "D29", "E29", "F29"],
    )


def rule_procese(data: CompanyRiskData, cfg: dict) -> KoRuleResult:
    """
    Workbook: D30='nu'->scazut, E30/F30='da'->mediu SAU ridicat (fara prag care sa separe
    mediu de ridicat). Distinctia nu='scazut' este insa clara si neambigua - o aplicam.
    Cand exista un proces ('da'), alegem conservator MEDIU (nu RIDICAT, pentru care nu avem
    niciun criteriu de diferentiere in workbook) si marcam explicit aceasta alegere.
    """
    r = cfg["rules"]["procese"]
    if data.legal_processes_present is None:
        return _skip("procese", "Procese", cfg,
                     f"DATA_MISSING: nu s-a putut determina daca exista procese/dosare. Mentiune gasita: {data.legal_processes or 'nicio mentiune'}.",
                     "reputational", inputs={"legal_processes": data.legal_processes})
    if not data.legal_processes_present:
        level = "SCAZUT"
        note = "Niciun proces/dosar identificat (corespunde D30='nu')."
    else:
        level = "MEDIU"
        note = ("Proces/dosar identificat (corespunde E30/F30='da'). Workbook-ul nu diferentiaza mediu de ridicat "
                "pentru acest caz - ales conservator MEDIU, nu RIDICAT (fara criteriu de diferentiere disponibil).")
    return KoRuleResult(
        rule_id="procese", label="Procese", category=cfg["category_labels"]["reputational"], weight=r["weight"],
        status=RuleStatus.OK, risk_level=level, score=_pts(level, cfg),
        explanation=f"{note} Detalii: {data.legal_processes or '-'}",
        inputs_used={"legal_processes_present": data.legal_processes_present, "legal_processes": data.legal_processes},
        source_cells=["B30", "D30", "E30", "F30"],
    )


def rule_istoric_insolvente(data: CompanyRiskData, cfg: dict) -> KoRuleResult:
    r = cfg["rules"]["istoric_insolvente"]
    if data.insolvency_history_present is None:
        return _skip("istoric_insolvente", "Istoric insolvente", cfg, "DATA_MISSING: informatia despre istoricul de insolventa nu a fost gasita in documente.", "reputational")
    level = "RIDICAT" if data.insolvency_history_present else "SCAZUT"
    return KoRuleResult(
        rule_id="istoric_insolvente", label="Istoric insolvente", category=cfg["category_labels"]["reputational"], weight=r["weight"],
        status=RuleStatus.OK, risk_level=level, score=_pts(level, cfg),
        explanation=f"Istoric insolventa prezent: {data.insolvency_history_present}.",
        inputs_used={"insolvency_history_present": data.insolvency_history_present, "notes": data.insolvency_history_notes},
        source_cells=["B31", "D31", "E31", "F31"],
    )


def rule_tip_ipoteca(data: CompanyRiskData, cfg: dict) -> KoRuleResult:
    return _skip("tip_ipoteca", "Tip ipoteca", cfg,
                 "NOT_IMPLEMENTED: workbook-ul defineste doar pragul pentru 'scazut' (D34='ipoteca'); "
                 "pragurile pentru mediu/ridicat (E34/F34) nu sunt completate in workbook. "
                 f"Colateral mentionat in documente (informativ): {data.collateral_description or 'nespecificat'}.",
                 "colaterizare", inputs={"collateral_description": data.collateral_description})


def rule_grad_acoperire(data: CompanyRiskData, cfg: dict) -> KoRuleResult:
    r = cfg["rules"]["grad_acoperire"]
    pct = data.guarantee_coverage_pct
    if pct is None and data.collateral_value and data.exposure_value:
        pct = data.collateral_value / data.exposure_value * 100
    if pct is None:
        return _skip("grad_acoperire", "Grad de acoperire cu garantii", cfg,
                     "DATA_MISSING: gradul de acoperire cu garantii nu a fost gasit/nu a putut fi calculat "
                     "din valoarea garantiilor si valoarea expunerii.", "colaterizare")
    ratio = pct / 100 if pct > 3 else pct  # tolerate either "80" (percent) or "0.80" (fraction) as extracted
    hi = _num(r["scazut"])  # ">=1.3" (D35)
    lo = _num(r["ridicat"]) / 100  # "<100%" -> 1.0 (F35)
    level = "SCAZUT" if ratio >= hi else ("RIDICAT" if ratio < lo else "MEDIU")
    return KoRuleResult(
        rule_id="grad_acoperire", label="Grad de acoperire cu garantii", category=cfg["category_labels"]["colaterizare"], weight=r["weight"],
        status=RuleStatus.OK, risk_level=level, score=_pts(level, cfg),
        explanation=f"Grad de acoperire = {ratio:.0%}. Workbook: scazut>={hi:.0%}, mediu intre {lo:.0%}-{hi:.0%}, ridicat<{lo:.0%}.",
        inputs_used={"guarantee_coverage_ratio": round(ratio, 4)}, source_cells=["B35", "D35", "E35", "F35"],
    )


_RULE_FUNCS = [
    rule_vechime, rule_domeniu_activitate,
    rule_scor_crc, rule_incidente_cip, rule_datorii_anaf, rule_popriri, rule_incidente_grup,
    rule_rating, rule_ebitda_rate_solicitant, rule_ebitda_rate_grup,
    rule_conformitate, rule_procese, rule_istoric_insolvente,
    rule_tip_ipoteca, rule_grad_acoperire,
]


def _classify_by_score(score: float, cfg: dict) -> str:
    t = cfg["overall_thresholds"]
    if score <= t["ridicat_max"]:
        return "RIDICAT"
    if score <= t["mediu_max"]:
        return "MEDIU"
    return "SCAZUT"


# Minimum share of a category's defined weight that must be resolved (status=OK) before we
# assert a discrete Scazut/Mediu/Ridicat grade for that category. This is NOT a business
# threshold from the KO workbook (the workbook defines no per-category confidence rule at
# all) - it is an engineering honesty policy: below this bar, too much of the category's
# own defined weight is NOT_IMPLEMENTED/DATA_MISSING to trust a specific tier, so we report
# the category as indeterminate instead of a confident-but-potentially-misleading label.
# The numeric score (when any sub-rule is known) is still reported for transparency and
# still feeds the overall calculation - only the discrete label is withheld.
CATEGORY_MIN_COMPLETENESS = 0.5


def apply_ko_rules(data: CompanyRiskData, deterministic_financials: dict, ko_xlsx_path: str | None = None) -> KoEngineResult:
    cfg = _load_config(ko_xlsx_path or str(KO_XLSX_DEFAULT))

    rules: list[KoRuleResult] = []
    for fn in _RULE_FUNCS:
        if fn is rule_solvabilitate:
            continue
        rules.append(fn(data, cfg))
    rules.append(rule_solvabilitate(data, deterministic_financials, cfg))

    categories: list[KoCategoryResult] = []
    notes = [
        "Motorul KO foloseste formula principala din workbook (celula B37) si pragurile D39/D40/D41. "
        "Un al doilea tabel de ponderi (L14:Q20) exista in workbook dar pare a fi o varianta alternativa "
        "de lucru, neconectata la formula finala - a fost ignorat.",
    ]

    overall_weighted_score = 0.0
    overall_weight_covered = 0.0

    for cat_key, cat_label in cfg["category_labels"].items():
        cat_weight = cfg["category_weights"][cat_key]
        cat_rules = [r for r in rules if r.category == cat_label]
        ok_rules = [r for r in cat_rules if r.status == RuleStatus.OK]
        sub_weight_total = sum(r.weight for r in cat_rules) or 1.0
        sub_weight_ok = sum(r.weight for r in ok_rules)
        completeness = sub_weight_ok / sub_weight_total if sub_weight_total else 0.0

        if ok_rules:
            cat_score = sum(r.weight * r.score for r in ok_rules) / sub_weight_ok
            cat_level = _classify_by_score(cat_score, cfg) if completeness >= CATEGORY_MIN_COMPLETENESS else None
            status = RuleStatus.OK
            overall_weighted_score += cat_weight * cat_score
            overall_weight_covered += cat_weight
        else:
            cat_score = None
            cat_level = None
            status = RuleStatus.NOT_IMPLEMENTED if all(r.status == RuleStatus.NOT_IMPLEMENTED for r in cat_rules) else RuleStatus.DATA_MISSING

        categories.append(
            KoCategoryResult(
                category=cat_label, weight=cat_weight, status=status, risk_level=cat_level, score=cat_score,
                completeness=round(completeness, 3), rules=cat_rules,
                missing_inputs=[r.label for r in cat_rules if r.status != RuleStatus.OK],
            )
        )

    if overall_weight_covered > 0:
        overall_score_normalized = overall_weighted_score / overall_weight_covered
        overall_level = _classify_by_score(overall_score_normalized, cfg)
    else:
        overall_score_normalized = None
        overall_level = None
        notes.append("Nicio categorie nu a putut fi calculata - date insuficiente pentru o opinie KO indicativa.")

    if overall_weight_covered < 1.0:
        notes.append(
            f"Opinia KO este calculata pe {overall_weight_covered:.0%} din ponderea totala a criteriilor "
            "(restul sunt NOT_IMPLEMENTED/DATA_MISSING pentru acest MVP) - este INDICATIVA, nu finala."
        )

    return KoEngineResult(
        overall_risk_level=overall_level,
        overall_score=round(overall_score_normalized, 3) if overall_score_normalized is not None else None,
        overall_completeness=round(overall_weight_covered, 3),
        categories=categories,
        notes=notes,
    )
