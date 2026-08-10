"""
Pydantic data models used across the pipeline.

Flow of data:

    RawDocument (document_reader)
        -> CompanyRiskData   (extractor.py, filled by GPT-5-mini; facts + evidence only)
        -> KoEngineResult    (ko_engine.py, filled by deterministic Python rules)
        -> RiskOpinion       (opinion_generator.py, formulated by GPT-5-mini from the two above)

GPT is only ever allowed to produce CompanyRiskData (extraction) and the "mentions" /
free text of RiskOpinion (formulation). It never produces KoEngineResult - that is
100% deterministic Python. See ko_engine.py for details.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

# Placeholders mandated by the project spec. Never invent values instead of these.
MISSING_IBS_FLOW = "[DATE NECESARE DIN IBS/FLOW]"
MISSING_INFO = "[INFORMAȚIE INDISPONIBILĂ]"


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #
class RawDocument(BaseModel):
    """Uniform representation of a parsed input document."""

    filename: str
    doc_type: str  # "referat_pdf" | "crc_pdf" | "financial_xlsm" | "financial_group_xlsm" | "other"
    text: str = ""
    # For spreadsheets: sheet_name -> list of rows (each row a list of cell values)
    sheets: dict[str, list[list]] = Field(default_factory=dict)
    char_count: int = 0
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Extraction (GPT reads & extracts, never computes KO)
# --------------------------------------------------------------------------- #
class Evidence(BaseModel):
    """Source trace for one extracted fact - required for any non-null field of value."""

    field: str
    value: str
    source_document: str
    excerpt: Optional[str] = None


class Shareholder(BaseModel):
    name: Optional[str] = None
    percentage: Optional[float] = None


class CompanyRiskData(BaseModel):
    """
    Structured facts extracted from company documents by GPT-5-mini.

    Every field is Optional. GPT must return null (not a guess, not 0) when a value
    is not clearly present in the supplied documents. Do not use this model to store
    computed ratios/percentages that Python is supposed to calculate deterministically
    (e.g. revenue_change_pct, solvency_pct) - those live in KoEngineResult /
    financial_calcs, derived in Python from the raw numbers below.
    """

    # Identification
    company_name: Optional[str] = None
    cui: Optional[str] = None
    reg_com_number: Optional[str] = None
    branch: Optional[str] = None
    id_dosar: Optional[str] = None
    registration_date: Optional[str] = None  # ISO date string if determinable
    caen_code: Optional[str] = None
    activity_description: Optional[str] = None
    company_description: Optional[str] = None
    shareholders: list[Shareholder] = Field(default_factory=list)
    group_companies: list[str] = Field(default_factory=list)

    # Current request
    current_request: Optional[str] = None
    requested_amount: Optional[float] = None
    currency: Optional[str] = None
    duration_months: Optional[int] = None
    guarantees_description: Optional[str] = None

    # Rating / CRC / behaviour
    rating: Optional[str] = None
    crc_score: Optional[float] = None
    crc_score_group: Optional[float] = None
    cip_incident_present: Optional[bool] = None
    cip_details: Optional[str] = None
    cip_incident_group_present: Optional[bool] = None  # incidente CIP la entitati din grup (nu solicitant)
    cip_group_details: Optional[str] = None
    anaf_debt_amount: Optional[float] = None
    anaf_debt_group_amount: Optional[float] = None
    popriri_present: Optional[bool] = None
    popriri_details: Optional[str] = None
    popriri_group_present: Optional[bool] = None
    popriri_group_details: Optional[str] = None
    payment_history_notes: Optional[str] = None

    # Financials (raw figures, as found - Python computes ratios from these)
    revenue_current: Optional[float] = None
    revenue_previous: Optional[float] = None
    net_profit_current: Optional[float] = None
    ebitda_current: Optional[float] = None
    ebitda_group: Optional[float] = None
    installments_total_current: Optional[float] = None  # "rate totale" solicitant
    installments_total_group: Optional[float] = None  # "rate totale" grup
    total_equity: Optional[float] = None
    total_assets: Optional[float] = None
    repayment_capacity_notes: Optional[str] = None

    # Reputational / legal
    aml_risk_statement: Optional[str] = None  # literal wording found in docs, if any
    aml_risk_level_stated: Optional[str] = None  # literal grad (scazut/mediu/ridicat) DEJA declarat in document de matricea AML a bancii - nu calculat de noi
    legal_processes: Optional[str] = None
    legal_processes_present: Optional[bool] = None  # True/False daca exista vreun dosar (civil/penal) mentionat pt solicitant/asociati/administratori/fideiusori/grup
    insolvency_history_present: Optional[bool] = None
    insolvency_history_notes: Optional[str] = None

    # Collateral
    collateral_description: Optional[str] = None
    collateral_value: Optional[float] = None
    exposure_value: Optional[float] = None
    guarantee_coverage_pct: Optional[float] = None  # only if explicitly stated in docs

    other_relevant_risks: Optional[str] = None

    # Transparency
    missing_information: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# KO engine (Python only, deterministic)
# --------------------------------------------------------------------------- #
class RuleStatus(str, Enum):
    OK = "OK"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    DATA_MISSING = "DATA_MISSING"


class KoRuleResult(BaseModel):
    rule_id: str
    label: str
    category: str
    weight: float
    status: RuleStatus
    risk_level: Optional[str] = None  # "SCAZUT" | "MEDIU" | "RIDICAT"
    score: Optional[float] = None  # points on the 1/2/4 scale from the KO workbook
    explanation: str
    inputs_used: dict = Field(default_factory=dict)
    source_cells: list[str] = Field(default_factory=list)  # cells in criterii KO xlsx used


class KoCategoryResult(BaseModel):
    category: str
    weight: float
    status: RuleStatus
    risk_level: Optional[str] = None
    score: Optional[float] = None
    completeness: float = 0.0  # fraction of category weight backed by OK rules
    rules: list[KoRuleResult] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)


class KoEngineResult(BaseModel):
    overall_risk_level: Optional[str] = None
    overall_score: Optional[float] = None
    overall_completeness: float = 0.0
    categories: list[KoCategoryResult] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Final opinion (GPT formulates mentions; grades come verbatim from KoEngineResult)
# --------------------------------------------------------------------------- #
class RiskMention(BaseModel):
    type: str
    grade: str
    mentions: str
    missing_inputs: list[str] = Field(default_factory=list)


class RiskOpinion(BaseModel):
    client: str
    cui: str
    branch: str
    categorie_client: str = "Corporate"

    overall_risk: str
    recommendations: list[str] = Field(default_factory=list)
    risks: list[RiskMention] = Field(default_factory=list)

    missing_fields: list[str] = Field(default_factory=list)
    disclaimer: str = (
        "Aceasta opinie este generata automat pe baza documentelor disponibile si a "
        "regulilor KO aplicate determinist. Nu reprezinta o decizie finala de risc - "
        "validarea este in responsabilitatea ofiterului de risc."
    )
