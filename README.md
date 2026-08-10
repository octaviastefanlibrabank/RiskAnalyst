# Risk Opinion MVP - generare automata a opiniilor de risc Corporate

MVP care citeste documentele disponibile pentru o companie Corporate (referat,
verificari CRC, analiza financiara), extrage informatii structurate cu Azure
OpenAI GPT-5-mini, aplica determinist regulile KO citite din
`data/opinie_risc/criterii KO - baze de date independente.xlsx`, si genereaza o
opinie de risc pre-completata (JSON + Markdown + HTML).

**Ofiterul de risc valideaza opinia. AI-ul nu ia decizia finala** - fiecare
opinie generata contine un disclaimer explicit si lista campurilor lipsa/
neimplementate.

Fluxul:

```
documente companie -> extragere (GPT-5-mini) -> reguli KO (Python, determinist) -> opinie structurata -> JSON/MD/HTML
```

## 1. Instalare

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

## 2. Configurare Azure OpenAI

```bash
copy .env.example .env
```

Completeaza in `.env`:

```
AZURE_OPENAI_API_KEY=<cheia ta>
AZURE_OPENAI_ENDPOINT=https://<resursa-ta>.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-5-mini      # numele deployment-ului tau, nu neaparat "gpt-5-mini"
AZURE_OPENAI_API_VERSION=2024-12-01-preview
```

Fara aceste credentiale, aplicatia poate rula in continuare cu `--no-llm`
(testeaza parserele + motorul KO, fara apeluri catre API).

## 3. Rulare

```bash
python main.py --list-companies

python main.py --company "BEO TRADE COM SRL"
python main.py --company "BEO TRADE COM SRL" --no-llm   # fara costuri API, doar parsere + KO
```

Output-ul se salveaza in:

```
generated/<COMPANIE>/opinion.json
generated/<COMPANIE>/opinion.md
generated/<COMPANIE>/opinion.html
```

## 4. Evaluare (comparatie cu opinia de referinta a bancii)

```bash
python evaluate.py --company "BEO TRADE COM SRL"
```

Compara STRICT sintactic (nume sectiuni + text de grad) opinia generata cu
`OUTPUT - opinie_risc <companie>.doc` (fisier HTML salvat cu extensia .doc).
**Nu este o evaluare semantica** - arata doar ce sectiuni exista in ambele, daca
gradele coincid ca text, si ce lipseste. Fisierul OUTPUT nu este niciodata
folosit ca input pentru generarea opiniei aceleiasi companii (ar fi data
leakage) - e citit doar de `evaluate.py`, separat de `main.py`.

## 5. Structura proiectului

```
risk_opinion_mvc/
├── data/opinie_risc/          # date sursa (companii + criterii KO)
├── generated/                 # output-uri generate (json/md/html)
├── src/
│   ├── document_reader.py     # citire PDF / XLSM / .doc(HTML)
│   ├── azure_llm.py           # wrapper Azure OpenAI (GPT-5-mini)
│   ├── extractor.py           # extragere structurata (LLM + calcule Python)
│   ├── ko_engine.py           # reguli KO determinist, citite din xlsx
│   ├── opinion_generator.py   # formulare mentiuni (LLM) + randare output
│   ├── models.py               # modele Pydantic
│   └── utils.py
├── main.py
├── evaluate.py
├── requirements.txt
├── .env.example
└── README.md
```

## 6. Principiu de arhitectura

* **LLM (GPT-5-mini)**: citeste documentele si extrage fapte structurate
  (`CompanyRiskData`), cu sursa/evidence pentru fiecare camp important. Ulterior
  formuleaza textul ("mentions") pentru fiecare tip de risc si recomandarile,
  pe baza gradelor deja calculate. **Nu calculeaza si nu modifica niciun grad
  de risc.**
* **Python**: parseaza fisierele, calculeaza cateva figuri determinist direct
  din workbook-ul de analiza financiara (ex. solvabilitatea), si aplica
  regulile/pragurile KO citite din `criterii KO - baze de date independente.xlsx`
  (sheet `Corporate`) - vezi `src/ko_engine.py`.

## 7. Ce este implementat vs. NOT_IMPLEMENTED / DATA_MISSING

Workbook-ul `criterii KO...xlsx` este un document de lucru al bancii, cu
intrebari deschise ("de lamurit", "alte intrebari") si doua tabele de ponderi
partial inconsistente. Am folosit formula finala neambigua (`B37` +
pragurile `D39/D40/D41`) si am implementat determinist regulile ale caror
praguri sunt complete si clare in workbook:

| Regula implementata (OK) | Sursa |
|---|---|
| Vechime companie | data infiintare (documente) vs praguri D15/E15/F15 |
| Scor CRC | scor extras vs praguri D18/E18/F18 |
| Incidente CIP (ultimele 12 luni) | prezenta/absenta incident |
| Datorii ANAF (% din CA) | suma ANAF / cifra afaceri vs praguri D20/E20/F20 |
| Popriri | prezenta/absenta |
| Rating | incadrare in listele D24/E24/F24 |
| EBITDA / rate - solicitant si grup | raport EBITDA/rate vs praguri D25/E25/F25 (D26/E26/F26) |
| Solvabilitate | calculata determinist din sheet-ul "Indicatori financiari" (Equity Ratio) vs D27/E27/F27 |
| Istoric insolvente | prezenta/absenta |
| Grad de acoperire cu garantii | valoare garantii/expunere vs D35/E35/F35 |

| Regula NOT_IMPLEMENTED / DATA_MISSING | Motiv |
|---|---|
| Domeniu de activitate (CAEN restrictionat) | lista CAEN-urilor interzise/restrictionate din strategia/norma bancii nu este in datele disponibile |
| Incidente la nivel de grup | agregarea per grup e marcata "de verificat" chiar in workbook, fara definitie clara |
| Conformitate (AML) | workbook-ul are 'da' atat pentru mediu, cat si pentru ridicat - fara prag care sa le separe |
| Procese | idem - 'da' pentru mediu si ridicat, fara criteriu de diferentiere |
| Tip ipoteca | doar pragul "scazut" (D34) este completat in workbook, E34/F34 lipsesc |

Fiecare regula NOT_IMPLEMENTED/DATA_MISSING apare explicit in opinia generata
(sectiunea "Detaliu reguli KO" si "Campuri lipsa"), impreuna cu explicatia. Grad
de risc general este calculat doar din ponderea criteriilor efectiv disponibile
si este marcat explicit ca fiind **indicativ / partial** cand nu toate
criteriile au putut fi evaluate.

Campurile care in mod normal vin din IBS/Flow (Branch, Id Dosar, Credit ID,
detalii de expunere grup) si nu apar in documentele disponibile sunt marcate cu
`[DATE NECESARE DIN IBS/FLOW]`. Orice alta informatie lipsa este marcata cu
`[INFORMAȚIE INDISPONIBILĂ]`. Nimic nu este inventat sau presupus.

## 8. Ce functioneaza

* Citire PDF (referat, CRC) cu PyMuPDF.
* Citire XLSM/XLSX (analiza financiara) cu openpyxl, read-only, fara a modifica fisierele originale.
* Citire fisiere `.doc` care sunt de fapt HTML (confirmat pentru toate cele 4 companii din `data/`).
* Calcul determinist al solvabilitatii direct din workbook (validat: 60.8% calculat pentru BEO TRADE vs. "61% in 04.2026" mentionat in opinia de referinta a bancii).
* Motor KO complet functional pentru cele 11 reguli cu praguri clare, cu breakdown si transparenta completa.
* Generare `opinion.json` / `opinion.md` / `opinion.html` pentru toate cele 4 companii, rulate end-to-end cu `--no-llm`.
* `evaluate.py` functional (testat pe toate cele 4 companii - parseaza corect sectiunile si gradele din opiniile de referinta).
* Extragere GPT-5-mini + formulare mentiuni testate live pe toate cele 4 companii (necesita `.env` configurat).

## 9. Ce NU este implementat / limitari cunoscute

* Testat end-to-end cu Azure OpenAI GPT-5-mini pentru toate cele 4 companii (extragere + formulare mentiuni functionale). Completitudine KO obtinuta: BEO TRADE 100%, GIUPA ACR 100%, FIALD ~85%, SWISS GOLD ~85% (restul sunt regulile NOT_IMPLEMENTED/DATA_MISSING descrise mai jos, plus cateva campuri lipsa punctual din documente).
* Riscul general calculat difera de opinia de referinta a bancii la toate cele 4 companii (vezi `evaluate.py`) - motivul principal: sectiunile "Conformitate (AML)" si "Procese" sunt NOT_IMPLEMENTED (workbook-ul nu diferentiaza determinist mediu/ridicat pentru ele), iar in opiniile de referinta ale bancii tocmai acestea par sa ridice frecvent gradul la "mediu". Cand toate criteriile relevante au putut fi calculate (ex. Risc strategic, Risc financiar la FIALD), gradul generat coincide cu referinta.
* Regulile marcate NOT_IMPLEMENTED/DATA_MISSING (tabelul de mai sus) - intentionat, pentru ca workbook-ul KO nu ofera praguri complete/neambigue pentru ele in acest MVP.
* "Solvabilitate" este aproximata cu "Equity Ratio" (Capitaluri proprii / Total active) din sheet-ul "Indicatori financiari" - la GIUPA ACR, valoarea calculata (22.1%) difera de "Solvabilitate ajustata 58%" mentionata in opinia de referinta, ceea ce sugereaza ca banca foloseste o formula ajustata (posibil excluzand anumite active/pasive) pe care nu am gasit-o documentata explicit; am preferat sa folosim o valoare reala, determinista si trasabila, in loc sa ghicim ajustarea.
* Nu exista integrare IBS/Flow (intentionat, conform cerintei).
* Fara baza de date, autentificare, Docker - intentionat, MVP.
* `evaluate.py` face o comparatie strict sintactica (nume sectiune + text grad), nu semantica.

## 10. IBS / Flow

Integrarea cu IBS/Flow NU este implementata (conform cerintei). Orice camp care
ar trebui sa vina din aceste sisteme si nu poate fi determinat sigur din
documentele disponibile este marcat explicit `[DATE NECESARE DIN IBS/FLOW]`, nu
inventat.
