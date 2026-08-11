#!/usr/bin/env python
"""
Small localhost UI for the Corporate risk opinion MVP.

Run:
    python web_app.py

Then open:
    http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import cgi
import html
import json
import mimetypes
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import document_reader, extractor, ko_engine, opinion_generator
from src.azure_llm import AzureLLM, LLMNotConfigured

ROOT = Path(__file__).resolve().parent
DATA_ROOT = ROOT / "data" / "opinie_risc"
OUTPUT_ROOT = ROOT / "generated"
ALLOWED_OUTPUT_FILES = {"opinion.json", "opinion.md", "opinion.html", "opinion.docx"}
ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".xlsx", ".xlsm", ".doc"}


class WebLogger:
    def __init__(self, total: int):
        self.total = total
        self.current = 0
        self.lines: list[str] = []

    def step(self, message: str) -> None:
        self.current += 1
        self.lines.append(f"[{self.current}/{self.total}] {message}")

    def info(self, message: str) -> None:
        self.lines.append(f"      {message}")

    def warn(self, message: str) -> None:
        self.lines.append(f"      ! {message}")


def _company_names() -> list[str]:
    return document_reader.list_companies(DATA_ROOT)


def _find_company(company: str) -> Path | None:
    return document_reader.find_company_dir(DATA_ROOT, company)


def _sanitize_company_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name)
    cleaned = " ".join(cleaned.split()).strip()
    if not cleaned:
        raise ValueError("Numele firmei este obligatoriu.")
    return cleaned[:120]


def _safe_upload_filename(filename: str) -> str:
    name = Path(filename or "").name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name)
    name = " ".join(name.split()).strip()
    if not name:
        raise ValueError("Un fisier incarcat nu are nume valid.")
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_UPLOAD_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_UPLOAD_EXTENSIONS))
        raise ValueError(f"Fisierul '{name}' are extensie neacceptata. Acceptat: {allowed}.")
    if not name.lower().startswith("input"):
        name = f"INPUT - {name}"
    return name[:180]


def _unique_path(folder: Path, filename: str) -> Path:
    path = folder / filename
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for i in range(2, 1000):
        candidate = folder / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Prea multe fisiere cu numele '{filename}'.")


def _upload_company(form) -> dict:
    raw_name = form.getfirst("company_name", "")
    company_name = _sanitize_company_name(raw_name)
    company_dir = DATA_ROOT / company_name
    company_dir.mkdir(parents=True, exist_ok=True)

    file_items = form["documents"] if "documents" in form else []
    if not isinstance(file_items, list):
        file_items = [file_items]
    saved: list[str] = []
    for item in file_items:
        if not getattr(item, "filename", None):
            continue
        filename = _safe_upload_filename(item.filename)
        target = _unique_path(company_dir, filename)
        data = item.file.read()
        if not data:
            continue
        target.write_bytes(data)
        saved.append(target.name)

    if not saved:
        raise ValueError("Incarca cel putin un document PDF/XLSX/XLSM/DOC.")

    return {"company": company_name, "saved": saved}


def _output_summary(company_name: str) -> dict:
    company_dir = _find_company(company_name)
    canonical = company_dir.name if company_dir else company_name
    out_dir = OUTPUT_ROOT / canonical
    opinion_path = out_dir / "opinion.json"

    summary = {
        "company": canonical,
        "exists": opinion_path.exists(),
        "overall_risk": None,
        "overall_completeness": None,
        "run_mode": None,
        "partial": False,
        "risk_count": 0,
        "missing_count": 0,
        "updated_at": None,
        "files": {},
    }

    for filename in sorted(ALLOWED_OUTPUT_FILES):
        path = out_dir / filename
        summary["files"][filename] = {
            "exists": path.exists(),
            "url": f"/output?company={quote(canonical)}&file={quote(filename)}",
        }

    if not opinion_path.exists():
        return summary

    summary["updated_at"] = opinion_path.stat().st_mtime
    try:
        payload = json.loads(opinion_path.read_text(encoding="utf-8"))
        opinion = payload.get("opinion", {})
        ko_result = payload.get("ko_result", {})
        extracted_data = payload.get("extracted_data", {})
        missing_info = extracted_data.get("missing_information", [])
        no_llm = any("--no-llm" in str(item) or "GPT dezactivata" in str(item) for item in missing_info)
        summary.update(
            {
                "overall_risk": opinion.get("overall_risk"),
                "overall_completeness": ko_result.get("overall_completeness"),
                "run_mode": "Fara Azure OpenAI" if no_llm else "Azure OpenAI",
                "partial": no_llm or (ko_result.get("overall_completeness") or 0) < 0.8,
                "risk_count": len(opinion.get("risks", [])),
                "missing_count": len(opinion.get("missing_fields", [])),
            }
        )
    except Exception as exc:
        summary["read_error"] = str(exc)
    return summary


def _generate(company_name: str, use_llm: bool) -> dict:
    logger = WebLogger(total=5)
    company_dir = _find_company(company_name)
    if company_dir is None:
        raise ValueError(f"Compania '{company_name}' nu a fost gasita.")

    llm: AzureLLM | None = None
    if use_llm:
        try:
            llm = AzureLLM()
        except LLMNotConfigured as exc:
            raise RuntimeError(str(exc)) from exc

    logger.step("Citire documente")
    documents = document_reader.discover_company_documents(company_dir, include_output=False)
    for doc in documents:
        warning = f" [!] {'; '.join(doc.warnings)}" if doc.warnings else ""
        logger.info(f"{doc.filename} ({doc.doc_type}) - {doc.char_count} chars/cells{warning}")
    if not documents:
        logger.warn("Niciun document INPUT gasit.")

    logger.step("Extragere date" + (" cu Azure OpenAI" if use_llm else " fara LLM"))
    data, deterministic = extractor.extract_company_data(company_dir.name, documents, llm, logger)
    logger.info(f"Solvabilitate determinista: {deterministic.get('solvency_pct')}")

    logger.step("Aplicare reguli KO")
    ko = ko_engine.apply_ko_rules(data, deterministic)
    for category in ko.categories:
        logger.info(
            f"{category.category}: {category.status.value}, grad={category.risk_level}, "
            f"completitudine={category.completeness:.0%}"
        )
    logger.info(f"Risc general: {ko.overall_risk_level}, completitudine={ko.overall_completeness:.0%}")

    logger.step("Generare opinie")
    opinion = opinion_generator.generate_opinion(company_dir.name, data, ko, llm, logger)

    logger.step("Salvare output")
    out_dir = opinion_generator.save_outputs(company_dir.name, data, ko, opinion, OUTPUT_ROOT)
    logger.info(str(out_dir))

    return {"summary": _output_summary(company_dir.name), "logs": logger.lines}


INDEX_HTML = r"""<!doctype html>
<html lang="ro">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Risk Opinion Workbench</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f8fb;
      --ink: #1f2933;
      --muted: #64748b;
      --line: #d9e0e8;
      --panel: #ffffff;
      --accent: #116149;
      --accent-strong: #0b4937;
      --danger: #a23a33;
      --soft: #eef4f1;
      --focus: #3b82f6;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: Arial, Helvetica, sans-serif;
      line-height: 1.45;
    }
    header {
      border-bottom: 1px solid var(--line);
      background: #fff;
    }
    .bar {
      max-width: 1240px;
      margin: 0 auto;
      padding: 18px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }
    h1 {
      margin: 0;
      font-size: 22px;
      font-weight: 700;
      letter-spacing: 0;
    }
    .status-pill {
      min-width: 118px;
      text-align: center;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 7px 11px;
      color: var(--muted);
      background: #fff;
      font-size: 13px;
    }
    main {
      max-width: 1240px;
      margin: 0 auto;
      padding: 22px 24px 30px;
      display: grid;
      grid-template-columns: 380px minmax(0, 1fr);
      gap: 18px;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      min-width: 0;
    }
    .controls { padding: 18px; }
    label {
      display: block;
      margin: 0 0 7px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
    }
    select, button {
      font: inherit;
    }
    select {
      width: 100%;
      min-height: 42px;
      padding: 8px 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
    }
    select:focus, button:focus-visible, input:focus-visible {
      outline: 3px solid color-mix(in srgb, var(--focus) 25%, transparent);
      outline-offset: 1px;
    }
    .row { margin-top: 16px; }
    .add-box {
      margin-top: 16px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfd;
      padding: 12px;
      display: none;
    }
    .add-box.open { display: block; }
    input[type="text"], input[type="file"] {
      width: 100%;
      min-height: 40px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      padding: 8px 10px;
      font: inherit;
    }
    input[type="file"] {
      padding: 7px;
      font-size: 13px;
    }
    .toggle {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 11px 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fbfcfd;
    }
    .toggle span {
      font-size: 14px;
      font-weight: 700;
    }
    input[type="checkbox"] {
      width: 20px;
      height: 20px;
      accent-color: var(--accent);
    }
    .actions {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      margin-top: 16px;
    }
    button {
      min-height: 42px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      cursor: pointer;
      font-weight: 700;
    }
    button.primary {
      border-color: var(--accent);
      background: var(--accent);
      color: #fff;
    }
    button.primary:hover { background: var(--accent-strong); }
    button:hover { background: #f4f7fa; }
    button:disabled {
      opacity: .55;
      cursor: wait;
    }
    .summary {
      margin-top: 18px;
      border-top: 1px solid var(--line);
      padding-top: 18px;
      display: grid;
      gap: 10px;
    }
    .metric {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      font-size: 14px;
    }
    .metric strong { text-align: right; }
    .links {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-top: 14px;
    }
    .links a {
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      color: var(--accent-strong);
      text-decoration: none;
      font-size: 14px;
      font-weight: 700;
      background: #fff;
    }
    .links a.disabled {
      pointer-events: none;
      color: #9aa6b2;
      background: #f5f6f8;
    }
    .preview {
      min-height: 720px;
      display: grid;
      grid-template-rows: auto minmax(420px, 1fr);
    }
    .preview-head {
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .preview-title {
      margin: 0;
      font-size: 16px;
      font-weight: 700;
    }
    .risk-badge {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 6px 10px;
      color: var(--accent-strong);
      background: var(--soft);
      font-size: 13px;
      font-weight: 700;
      white-space: nowrap;
    }
    .warning {
      margin-top: 14px;
      border: 1px solid #f0c36b;
      background: #fff8e6;
      color: #6b4e00;
      border-radius: 6px;
      padding: 10px 11px;
      font-size: 13px;
      font-weight: 700;
    }
    iframe {
      width: 100%;
      height: 100%;
      border: 0;
      background: #fff;
      border-radius: 0 0 8px 8px;
    }
    .empty {
      display: grid;
      place-items: center;
      min-height: 420px;
      color: var(--muted);
      padding: 24px;
      text-align: center;
    }
    .log {
      margin-top: 18px;
      background: #0f172a;
      color: #dbeafe;
      border-radius: 8px;
      padding: 12px;
      min-height: 120px;
      max-height: 260px;
      overflow: auto;
      white-space: pre-wrap;
      font: 12px/1.5 Consolas, Monaco, monospace;
    }
    .error {
      color: var(--danger);
      font-weight: 700;
    }
    @media (max-width: 900px) {
      .bar {
        align-items: flex-start;
        flex-direction: column;
      }
      main {
        grid-template-columns: 1fr;
        padding: 16px;
      }
      .preview { min-height: 620px; }
      .actions, .links { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div class="bar">
      <h1>Risk Opinion Workbench</h1>
      <div class="status-pill" id="appStatus">Localhost</div>
    </div>
  </header>
  <main>
    <section class="controls">
      <label for="company">Client Corporate</label>
      <select id="company"></select>

      <div class="row toggle">
        <span>Azure OpenAI</span>
        <input id="useLlm" type="checkbox">
      </div>

      <div class="actions">
        <button class="primary" id="generate">Genereaza</button>
        <button id="refresh">Actualizeaza</button>
      </div>

      <div class="row">
        <button id="showAdd" type="button">Adauga firma</button>
      </div>

      <form class="add-box" id="addBox">
        <label for="newCompany">Nume firma</label>
        <input id="newCompany" name="company_name" type="text" autocomplete="off" placeholder="Ex: ACME INDUSTRIES SRL">
        <div class="row">
          <label for="documents">Documente</label>
          <input id="documents" name="documents" type="file" multiple accept=".pdf,.xlsx,.xlsm,.doc">
        </div>
        <div class="actions">
          <button class="primary" id="uploadCompany" type="submit">Incarca</button>
          <button id="cancelAdd" type="button">Renunta</button>
        </div>
      </form>

      <div class="summary" id="summary"></div>
      <div class="links" id="links"></div>
      <div class="log" id="log">Ready.</div>
    </section>

    <section class="preview">
      <div class="preview-head">
        <h2 class="preview-title" id="previewTitle">Preview opinie</h2>
        <div class="risk-badge" id="riskBadge">Fara output</div>
      </div>
      <div id="previewBody" class="empty">Selecteaza un client.</div>
    </section>
  </main>

  <script>
    const companyEl = document.getElementById('company');
    const useLlmEl = document.getElementById('useLlm');
    const generateEl = document.getElementById('generate');
    const refreshEl = document.getElementById('refresh');
    const showAddEl = document.getElementById('showAdd');
    const addBoxEl = document.getElementById('addBox');
    const cancelAddEl = document.getElementById('cancelAdd');
    const uploadCompanyEl = document.getElementById('uploadCompany');
    const newCompanyEl = document.getElementById('newCompany');
    const documentsEl = document.getElementById('documents');
    const summaryEl = document.getElementById('summary');
    const linksEl = document.getElementById('links');
    const logEl = document.getElementById('log');
    const appStatusEl = document.getElementById('appStatus');
    const previewTitleEl = document.getElementById('previewTitle');
    const previewBodyEl = document.getElementById('previewBody');
    const riskBadgeEl = document.getElementById('riskBadge');

    let currentSummary = null;

    function setBusy(isBusy) {
      generateEl.disabled = isBusy;
      refreshEl.disabled = isBusy;
      showAddEl.disabled = isBusy;
      uploadCompanyEl.disabled = isBusy;
      companyEl.disabled = isBusy;
      useLlmEl.disabled = isBusy;
      appStatusEl.textContent = isBusy ? 'Ruleaza' : 'Localhost';
    }

    function fmtDate(ts) {
      if (!ts) return '-';
      return new Date(ts * 1000).toLocaleString('ro-RO');
    }

    function fileLink(summary, filename, label) {
      const file = summary.files[filename];
      const cls = file && file.exists ? '' : ' class="disabled"';
      const href = file && file.exists ? file.url : '#';
      return `<a${cls} href="${href}" target="_blank" rel="noopener">${label}</a>`;
    }

    function renderSummary(summary) {
      currentSummary = summary;
      const completeness = summary.overall_completeness === null || summary.overall_completeness === undefined
        ? '-'
        : `${Math.round(summary.overall_completeness * 100)}%`;
      const warning = summary.partial
        ? `<div class="warning">Output partial: ${summary.run_mode || 'mod necunoscut'}. Pentru opinie reala, bifeaza Azure OpenAI si regenereaza.</div>`
        : '';
      summaryEl.innerHTML = `
        ${warning}
        <div class="metric"><span>Opinie generata</span><strong>${summary.exists ? 'Da' : 'Nu'}</strong></div>
        <div class="metric"><span>Mod rulare</span><strong>${summary.run_mode || '-'}</strong></div>
        <div class="metric"><span>Risc general</span><strong>${summary.overall_risk || '-'}</strong></div>
        <div class="metric"><span>Completitudine KO</span><strong>${completeness}</strong></div>
        <div class="metric"><span>Riscuri</span><strong>${summary.risk_count || 0}</strong></div>
        <div class="metric"><span>Campuri lipsa</span><strong>${summary.missing_count || 0}</strong></div>
        <div class="metric"><span>Actualizat</span><strong>${fmtDate(summary.updated_at)}</strong></div>
      `;
      linksEl.innerHTML = [
        fileLink(summary, 'opinion.html', 'HTML'),
        fileLink(summary, 'opinion.docx', 'DOCX'),
        fileLink(summary, 'opinion.json', 'JSON'),
        fileLink(summary, 'opinion.md', 'Markdown')
      ].join('');
      previewTitleEl.textContent = summary.company || 'Preview opinie';
      riskBadgeEl.textContent = summary.overall_risk || 'Fara output';
      if (summary.files && summary.files['opinion.html'] && summary.files['opinion.html'].exists) {
        previewBodyEl.className = '';
        previewBodyEl.innerHTML = `<iframe title="Preview opinie" src="${summary.files['opinion.html'].url}"></iframe>`;
      } else {
        previewBodyEl.className = 'empty';
        previewBodyEl.textContent = 'Nu exista opinie generata.';
      }
    }

    async function loadCompanies() {
      const response = await fetch('/api/companies');
      const data = await response.json();
      const previous = companyEl.value;
      companyEl.innerHTML = data.companies.map(c => `<option value="${c}">${c}</option>`).join('');
      if (previous && data.companies.includes(previous)) {
        companyEl.value = previous;
      }
      if (data.companies.length) {
        await loadStatus();
      }
    }

    async function loadStatus() {
      const company = companyEl.value;
      if (!company) return;
      const response = await fetch(`/api/status?company=${encodeURIComponent(company)}`);
      const data = await response.json();
      renderSummary(data.summary);
    }

    async function generateOpinion() {
      const company = companyEl.value;
      if (!company) return;
      if (!useLlmEl.checked) {
        const ok = confirm('Azure OpenAI este debifat. Se va genera doar un output partial de test, fara extractie din referat/CRC. Continui?');
        if (!ok) return;
      }
      setBusy(true);
      logEl.textContent = 'Pornire generare...';
      try {
        const response = await fetch('/api/generate', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({company, use_llm: useLlmEl.checked})
        });
        const data = await response.json();
        if (!response.ok || !data.ok) {
          logEl.innerHTML = `<span class="error">${data.error || 'Eroare necunoscuta'}</span>\n\n${data.trace || ''}`;
          return;
        }
        logEl.textContent = data.logs.join('\n');
        renderSummary(data.summary);
      } catch (error) {
        logEl.innerHTML = `<span class="error">${error}</span>`;
      } finally {
        setBusy(false);
      }
    }

    async function uploadCompany(event) {
      event.preventDefault();
      if (!newCompanyEl.value.trim()) {
        logEl.innerHTML = '<span class="error">Completeaza numele firmei.</span>';
        return;
      }
      if (!documentsEl.files.length) {
        logEl.innerHTML = '<span class="error">Alege documentele de incarcat.</span>';
        return;
      }
      setBusy(true);
      logEl.textContent = 'Incarcare documente...';
      try {
        const formData = new FormData(addBoxEl);
        const response = await fetch('/api/upload', { method: 'POST', body: formData });
        const data = await response.json();
        if (!response.ok || !data.ok) {
          logEl.innerHTML = `<span class="error">${data.error || 'Eroare la incarcare'}</span>`;
          return;
        }
        await loadCompanies();
        companyEl.value = data.company;
        addBoxEl.classList.remove('open');
        newCompanyEl.value = '';
        documentsEl.value = '';
        logEl.textContent = `Firma adaugata: ${data.company}\nFisiere:\n- ${data.saved.join('\n- ')}`;
        await loadStatus();
      } catch (error) {
        logEl.innerHTML = `<span class="error">${error}</span>`;
      } finally {
        setBusy(false);
      }
    }

    companyEl.addEventListener('change', loadStatus);
    refreshEl.addEventListener('click', loadStatus);
    generateEl.addEventListener('click', generateOpinion);
    showAddEl.addEventListener('click', () => addBoxEl.classList.toggle('open'));
    cancelAddEl.addEventListener('click', () => addBoxEl.classList.remove('open'));
    addBoxEl.addEventListener('submit', uploadCompany);
    loadCompanies().catch(error => {
      logEl.innerHTML = `<span class="error">${error}</span>`;
    });
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "RiskOpinionUI/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/companies":
            self._json(200, {"companies": _company_names()})
            return
        if parsed.path == "/api/status":
            qs = parse_qs(parsed.query)
            company = unquote(qs.get("company", [""])[0])
            if not company:
                self._json(400, {"error": "Parametrul company lipseste."})
                return
            self._json(200, {"summary": _output_summary(company)})
            return
        if parsed.path == "/output":
            self._serve_output(parsed.query)
            return
        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/generate":
            if parsed.path == "/api/upload":
                self._handle_upload()
                return
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            company = str(payload.get("company") or "")
            use_llm = bool(payload.get("use_llm"))
            result = _generate(company, use_llm)
            self._json(200, {"ok": True, **result})
        except Exception as exc:
            self._json(
                500,
                {
                    "ok": False,
                    "error": html.escape(str(exc)),
                    "trace": html.escape(traceback.format_exc(limit=8)),
                },
            )

    def _handle_upload(self) -> None:
        try:
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                self._json(400, {"ok": False, "error": "Cerere invalida: se asteapta multipart/form-data."})
                return
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": content_type,
                    "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
                },
            )
            result = _upload_company(form)
            self._json(200, {"ok": True, **result})
        except Exception as exc:
            self._json(
                500,
                {
                    "ok": False,
                    "error": html.escape(str(exc)),
                    "trace": html.escape(traceback.format_exc(limit=8)),
                },
            )

    def _serve_output(self, query: str) -> None:
        qs = parse_qs(query)
        company = unquote(qs.get("company", [""])[0])
        filename = Path(unquote(qs.get("file", [""])[0])).name
        company_dir = _find_company(company)
        if company_dir is None or filename not in ALLOWED_OUTPUT_FILES:
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        path = OUTPUT_ROOT / company_dir.name / filename
        if not path.exists():
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix == ".md":
            content_type = "text/plain; charset=utf-8"
        if path.suffix == ".json":
            content_type = "application/json; charset=utf-8"
        self._send(200, path.read_bytes(), content_type)


def main() -> int:
    parser = argparse.ArgumentParser(description="Localhost UI pentru generatorul de opinii de risc.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Risk Opinion UI running at {url}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
