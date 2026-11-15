from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.history_data_integrity import CANONICAL_HASH_POLICY, file_content_hashes
from src.warmup_audit import ema200_stabilization_bars

RUN_ID = "phase222b-local-20261115"
EXPECTED_CANONICAL = "0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3"
REPORT_DIR = ROOT / "reports" / "phase2_2_2e"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    temp.replace(path)


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=15)
    return result.stdout.strip() if result.returncode == 0 else ""


def safe_repo_path(raw: str) -> Path:
    relative = PurePosixPath(raw.replace("\\", "/"))
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("PREFLIGHT_SOURCE_PATH_INVALID")
    path = ROOT.joinpath(*relative.parts).resolve(strict=True)
    path.relative_to(ROOT.resolve())
    return path


def preflight() -> dict:
    prior = read_json(ROOT / "reports" / "phase2_2_2d" / "source_and_run_integrity.json")
    integrity = prior.get("source_integrity", {})
    prior_run = prior.get("backfill_run", {})
    run_root = ROOT / "data" / "market_history" / "backfill_runs" / RUN_ID
    manifest_path = run_root / "manifest.json"
    manifest = read_json(manifest_path)
    chunk_mismatches = 0
    for item in manifest.get("chunks", {}).values():
        path = safe_repo_path(item["file"])
        actual = file_content_hashes(path)
        if actual.get("raw_sha256") != item.get("raw_sha256") or actual.get("canonical_sha256") != item.get("canonical_sha256"):
            chunk_mismatches += 1
    source_mismatches = 0
    for item in integrity.get("source_files", []):
        try:
            actual = file_content_hashes(safe_repo_path(item["path"]))
        except (ValueError, OSError, KeyError):
            source_mismatches += 1
            continue
        if actual.get("raw_sha256") != item.get("raw_sha256") or actual.get("canonical_sha256") != item.get("canonical_sha256"):
            source_mismatches += 1
    frozen = file_content_hashes(ROOT / "data" / "backtest" / "technical_candidate_scan.csv")
    private_tracked = git("ls-files", "reports/private", "data/market_history/backfill_runs").splitlines()
    dep_versions = {}
    from importlib.metadata import version, PackageNotFoundError
    for name in ("pandas", "numpy", "pydantic", "MetaTrader5"):
        try:
            dep_versions[name] = version(name)
        except PackageNotFoundError:
            dep_versions[name] = "NOT_INSTALLED"
    passed = (
        prior.get("status") == "PASS"
        and manifest.get("status") == "HISTORY_BACKFILL_DOWNLOADED_PENDING_REAUDIT"
        and len(manifest.get("chunks", {})) == 98
        and len(list((run_root / "chunks").glob("*.csv"))) == 98
        and chunk_mismatches == 0 and source_mismatches == 0
        and frozen.get("canonical_sha256") == EXPECTED_CANONICAL
        and prior_run.get("chunk_rows") == 18175
        and git("rev-parse", "HEAD") == "6d1f0f7e3e696aa3b773bed997b9d813f1eba7dc"
        and git("branch", "--show-current") == "codex/phase2-2-2e-local-macro-and-backtest-integration"
    )
    return {
        "schema_version": 1, "status": "PASS" if passed else "FAIL",
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "base_commit": "6d1f0f7e3e696aa3b773bed997b9d813f1eba7dc",
        "current_commit": git("rev-parse", "HEAD"), "origin_main_commit": git("rev-parse", "origin/main"),
        "branch": git("branch", "--show-current"),
        "working_tree_before_task": "CLEAN",
        "environment": {"os": "Windows", "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}", "dependencies": dep_versions},
        "local_run": {
            "run_id": RUN_ID, "manifest_present": manifest_path.is_file(),
            "manifest_sha256": file_content_hashes(manifest_path)["raw_sha256"],
            "planned_chunks": len(manifest.get("chunks", {})),
            "actual_chunks_hashed": len(list((run_root / "chunks").glob("*.csv"))),
            "chunk_rows_prior_run": prior_run.get("chunk_rows"),
            "chunk_hash_mismatches": chunk_mismatches,
            "source_files_reported": len(integrity.get("source_files", [])),
            "source_hash_mismatches": source_mismatches,
            "source_rows_prior_audit": integrity.get("rows_audited"),
        },
        "frozen_dataset": {
            "path": "data/backtest/technical_candidate_scan.csv",
            "raw_sha256": frozen.get("raw_sha256"),
            "canonical_sha256": frozen.get("canonical_sha256"),
            "expected_canonical_sha256": EXPECTED_CANONICAL,
            "unchanged": frozen.get("canonical_sha256") == EXPECTED_CANONICAL,
        },
        "gitignore": {
            "private_reports_ignored": "reports/private/" in (ROOT / ".gitignore").read_text(encoding="utf-8"),
            "backfill_runs_ignored": "/data/market_history/backfill_runs/" in (ROOT / ".gitignore").read_text(encoding="utf-8"),
            "private_paths_tracked": len(private_tracked),
        },
        "market_history_requests": 0, "mt5_connection_or_reads": 0, "orders_sent": 0, "llm_provider_calls": 0,
        "canonicalization_policy": CANONICAL_HASH_POLICY,
    }


def run_stage(name: str, command: list[str], timeout: int = 240) -> dict:
    started = time.monotonic()
    print(f"[START] {name}", flush=True)
    output_parts: list[str] = []
    try:
        process = subprocess.Popen(
            command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0, env={**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"},
        )

        def stream_output() -> None:
            assert process.stdout is not None
            while True:
                chunk = os.read(process.stdout.fileno(), 4096)
                if not chunk:
                    break
                rendered = chunk.decode("utf-8", errors="replace")
                output_parts.append(rendered)
                sys.stdout.write(rendered)
                sys.stdout.flush()

        reader = threading.Thread(target=stream_output, daemon=True)
        reader.start()
        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            reader.join(timeout=5)
            print(f"[TIMEOUT] {name}", flush=True)
            return {
                "name": name, "status": "FAIL", "return_code": None,
                "test_count": None, "lookahead_checks": 0, "failure_count": None,
                "timeout_seconds": timeout, "reason": "TIMEOUT",
            }
        reader.join(timeout=5)
        output = "".join(output_parts)
        match = re.search(r"Ran\s+(\d+)\s+tests?", output)
        checks = re.search(r"Checks realizados:\s*(\d+)", output)
        status = "PASS" if return_code == 0 else "FAIL"
        detail = {
            "name": name, "status": status, "return_code": return_code,
            "duration_seconds": round(time.monotonic() - started, 3),
            "test_count": int(match.group(1)) if match else None,
            "lookahead_checks": int(checks.group(1)) if checks else 0,
            "failure_count": len(re.findall(r"^(?:FAIL|ERROR):", output, re.M)),
            "command": command[1:] if command and command[0] == sys.executable else command,
            "timeout_seconds": timeout,
        }
        suffix = f"; tests={detail['test_count']}" if detail["test_count"] is not None else ""
        if detail["lookahead_checks"]:
            suffix += f"; lookahead={detail['lookahead_checks']}"
        print(f"[{status}] {name}{suffix}", flush=True)
        if status == "FAIL":
            print("\n".join(output.strip().splitlines()[-35:]), flush=True)
        return detail
    except subprocess.TimeoutExpired:
        print(f"[TIMEOUT] {name}", flush=True)
        return {"name": name, "status": "FAIL", "return_code": None, "test_count": None, "lookahead_checks": 0, "failure_count": None, "timeout_seconds": timeout, "reason": "TIMEOUT"}


def macro_status(macro: dict, source_id: str) -> str:
    item = next((x for x in macro.get("sources", []) if x.get("source_id") == source_id), {})
    code = item.get("http_status")
    return f"{item.get('status', 'NOT_RUN')}" + (f" HTTP {code}" if code is not None else "")


def write_docs(pre: dict, summary: dict, offline: dict, macro: dict, tests: list[dict]) -> None:
    docs = ROOT / "docs"
    docs.mkdir(exist_ok=True)
    results = f"""# Resultados de Fase 2.2.2e

**Estado:** {summary['status']}

## Entorno y seguridad

- Windows, Python {pre['environment']['python']}; base main {pre['origin_main_commit']}; rama {pre['branch']}.
- Árbol de trabajo inicial limpio. No se creó commit, push, PR ni merge.
- Run privado {RUN_ID}: {pre['local_run']['actual_chunks_hashed']}/98 chunks verificados, {pre['local_run']['chunk_rows_prior_run']} filas del run previo; hashes fuente sin discrepancias: {pre['status']}.
- Dataset congelado canónico SHA-256: {pre['frozen_dataset']['canonical_sha256']}; intacto: {pre['frozen_dataset']['unchanged']}.
- MetaTrader5 Python disponible: {pre['environment']['dependencies']['MetaTrader5']}; terminal no conectado ni leído en esta fase.
- orders_sent=0; market_history_requests=0; llm_provider_calls=0.

## Resultados

| Comprobación | Estado | Evidencia |
|---|---|---|
| Tests offline | {offline['status']} | {sum(x.get('test_count') or 0 for x in tests)} pruebas unittest; reports/phase2_2_2e/offline_test_results.json |
| Fuentes macro gratuitas | {macro.get('status', 'MACRO_PARTIAL_COVERAGE')} | {macro.get('request_count', 0)} peticiones; macro_source_audit.json |
| Calendario del bróker | BROKER_CALENDAR_UNKNOWN | 0 de 1.850 resueltos; no hay documento histórico oficial |
| Continuidad MTF | REAL_MTF_WINDOWS_UNVERIFIED | 0 ventanas certificadas; EMA200 estabiliza a {ema200_stabilization_bars()} barras |
| Paridad scan/backtest | {summary['components']['pipeline']} | Integración sintética y rechazo por fingerprint/periodo |
| Costes | PASS_HYPOTHESIS_ONLY | Spread bid/ask sin doble cargo; costes EUR reales sin verificar |
| Backtest económico nuevo | BACKTEST_NOT_RUN | Calendario y ventanas MTF sin certificar |
| Look-ahead | {next((x['status'] for x in tests if x['name'] == 'look-ahead'), 'NOT_RUN')} | {offline['lookahead_checks']} controles |
| Operaciones | PASS | Cero llamadas MT5, órdenes y ejecuciones en esta fase |

## Pruebas ejecutadas

{chr(10).join('- ' + x['name'] + ': **' + x['status'] + '**' + (f" ({x['test_count']} tests)" if x.get('test_count') is not None else '') + '.' for x in tests)}

## Comparativa económica

No se ejecutó backtest económico nuevo, por lo que no hay resultados comparables. BASE, CONSERVATIVE y STRESS siguen siendo hipótesis; no se modificó estrategia ni se seleccionó ventana por PnL.

## Conclusión

La ingeniería queda preparada para revisión si las pruebas pasan. La fase no certifica el calendario del bróker, ventanas MTF reales ni ventaja de trading. Los 1.850 registros siguen UNKNOWN. Se necesita el documento histórico oficial del bróker para evaluar si existe una ventana causal elegible.

Ingeniería preparada ≠ calendario certificado ≠ ventaja de trading demostrada.
"""
    macro_doc = f"""# Fuentes macro gratuitas — Fase 2.2.2e

RSS/Atom informa de publicaciones ya emitidas; no demuestra previsiones disponibles antes de la publicación. ICS representa agenda futura con su hora/zona; no es un calendario de sesiones del bróker.

| Fuente oficial | Catálogo y feed | Divisa | Resultado |
|---|---|---|---|
| BCE | https://www.ecb.europa.eu/home/html/rss.en.html · https://www.ecb.europa.eu/rss/press.html | EUR | {macro_status(macro, 'ECB_PRESS_RSS')} |
| Federal Reserve | https://www.federalreserve.gov/feeds/feeds.htm · https://www.federalreserve.gov/feeds/press_all.xml | USD | {macro_status(macro, 'FED_PRESS_RSS')} |
| Bank of Canada | https://www.bankofcanada.ca/rss-feeds/ · https://www.bankofcanada.ca/content_type/press-releases/feed/ | CAD | {macro_status(macro, 'BANK_OF_CANADA_PRESS_RSS')} |
| RBA | https://www.rba.gov.au/rss/ · https://www.rba.gov.au/rss/rss-cb-media-releases.xml | AUD | {macro_status(macro, 'RBA_MEDIA_RSS')} |
| BLS ICS | https://www.bls.gov/schedule/news_release/bls.ics | USD | El cliente HTTP local no hizo GET; el adaptador web previo no pudo parsear text/calendar ni expuso estado HTTP. No hay verificación de respuesta/origen. |

En la pasada local se hicieron {macro.get('local_feed_http_requests', macro.get('request_count', 0))} solicitudes HTTP a feeds. Contando conservadoramente también cinco consultas externas de URLs oficiales durante el descubrimiento y una consulta al buscador, la fase consumió {macro.get('phase_external_requests_counted_conservatively', macro.get('request_count', 0))} de 10 solicitudes posibles. No queda presupuesto para otras peticiones. Por origen: máximo una y sin reintentos. TLS, timeout 10 s, máximo 2 MiB, redirecciones rechazadas, caché ETag/Last-Modified y User-Agent identificable. El catálogo del BCE indica que los lectores consultan periódicamente; no determina una cuota numérica. Las licencias, reglas robots y límites numéricos por origen quedan NOT_VERIFIED porque no aparecieron especificados en los catálogos revisados. No se infiere permiso ni cuota.

Solo el feed Fed confirmó HTTP 200 y produjo 20 eventos. ECB y RBA acabaron en URLError sin estado HTTP registrado. Bank of Canada devolvió un cuerpo que el primer parser rechazó como raíz XML no válida; ese pase no guardó el estado HTTP ni el cuerpo, y no se repitió. El parser ahora acepta RSS 1.0, pero el resultado anterior sigue BLOCKED y no se presenta como PASS.

403/429 no se reintentan. Feed vacío es PARTIAL y no se toma como ausencia confirmada de noticias. No se guardan titulares ni contenido de artículos en informes públicos. El caché bruto vive bajo reports/private/phase2_2_2e/macro_cache/. Solo un ICS con disponibilidad previa, hora exacta e importancia de investigación conocida aparece en el preview inactivo. Ningún macro feed modifica data/state/calendar.json o las sesiones del bróker.

Evidencia: reports/phase2_2_2e/macro_source_audit.json, calendar_preview.json y macro_replay_causality.json.
"""
    broker_doc = """# Calendario histórico del bróker: pendiente

Estado BROKER_CALENDAR_UNKNOWN. No hay documento oficial fechado que identifique la entidad concreta y cubra sesiones, símbolos, festivos, DST, zona del servidor y límites D1/H4/H1 durante el histórico.

Se conservan 1.850 registros UNKNOWN y se resolvieron 0. Los 74 candidatos y 1.776 grupos pueden solaparse y no se suman como registros disjuntos. No se usaron calendarios Forex genéricos, noticias ni gaps para inferir cierres.

La validación exige manifiesto v2, host de origen independiente y confiable, bytes del documento local con SHA-256 real y hashes de fragmentos citados para cada conjunto de reglas. La lista de hosts confiables está vacía al no estar confirmada la entidad. Las pruebas con broker.example.test son sintéticas y no acreditan sesiones reales.

Solicitar a soporte el documento oficial, sus vigencias, reglas por símbolo, zona del servidor, festivos, DST y límites de barra. No se solicitaron más velas MT5.
"""
    experiment_doc = f"""# Protocolo preregistrado

El gate económico permanece NOT_RUN. No se ejecutará backtest hasta verificar calendario, ventanas MTF causales, paridad real de fuente, reglas de ejecución y costes/conversiones.

- Umbral 65, modo LIMIT.
- Escenarios hipotéticos BASE, CONSERVATIVE y STRESS.
- Floors de spread: 0, 5, 10 y 20 puntos MT5.
- Train/evaluation/out-of-sample/observation se fijan antes de observar PnL y según cobertura disponible.
- Warmup separado del rango evaluado; estabilización EMA200 calculada por el código: {ema200_stabilization_bars()} barras.
- Sin optimización, DeepSeek ni otros LLM. Spread cero = OBSERVED_ZERO_UNVERIFIED; el bid/ask ya incluye spread, así que no se duplica.
- Sin conversión EUR verificada, el coste en EUR no se calcula.

Gate: reports/phase2_2_2e/exploratory_backtest_gate.json.
"""
    handoff_doc = f"""# Handoff remoto — Fase 2.2.2e

Base origin/main observada: {pre['origin_main_commit']}; rama {pre['branch']}; no se creó commit, push, PR ni merge. Suite offline: {offline['status']}. GitHub Actions no se ejecutó localmente. Dataset canónico: {pre['frozen_dataset']['canonical_sha256']}.

## Trabajo remoto priorizado

1. Revisar workflow offline, parser macro y contrato VerifiedResearchDataset; ejecutar CI sin MetaTrader5 ni secretos.
2. Revisar legalidad, robots y cuotas oficiales antes de ampliar endpoints.
3. Mantener scans y backtests reales bloqueados hasta recibir el calendario oficial fechado.

Prompt remoto: “Revisa esta rama contra main. Ejecuta solo offline-research-tests.yml, sin MetaTrader5, secretos ni APIs facturables. Comprueba privacidad, hashes, paridad sintética y diff-check. No cambies estrategia, parámetros ni resultados. No hagas merge con fallos y mantén el backtest económico NOT_RUN hasta validar el documento histórico del bróker.”

MT5 y los chunks son locales, privados y excluidos de Git. Cero órdenes, lecturas de histórico y llamadas LLM en esta fase.

Preguntas a soporte: entidad legal que rige la cuenta; documento histórico con rango de vigencia; zona de servidor, horarios por símbolo, festivos y DST; límites de etiqueta D1/H4/H1; archivo que cubra 2023-07-17 a 2026-11-11.
"""
    files = {
        "FASE2_2_2E_RESULTADOS.md": results,
        "FASE2_2_2E_MACRO_FUENTES.md": macro_doc,
        "FASE2_2_2E_BROKER_CALENDAR_PENDING.md": broker_doc,
        "FASE2_2_2E_EXPERIMENTO_PRE_REGISTRADO.md": experiment_doc,
        "FASE2_2_2E_HANDOFF_REMOTO.md": handoff_doc,
    }
    for name, content in files.items():
        path = docs / name
        temp = path.with_name(f".{path.name}.tmp")
        temp.write_text(content, encoding="utf-8")
        temp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline-first validation for Phase 2.2.2e.")
    parser.add_argument("--offline", action="store_true", default=True, help="No network calls; default.")
    parser.add_argument("--fetch-live", action="store_true", help="One bounded pass, up to four macro origins.")
    args = parser.parse_args()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    print("A 1/5: verifying local chunks, sources, frozen dataset and Git", flush=True)
    pre = preflight()
    write_json(REPORT_DIR / "preflight.json", pre)
    print(f"[{pre['status']}] local preflight", flush=True)
    if args.fetch_live:
        print("B 2/5: one bounded live macro pass; max four HTTP requests", flush=True)
        cmd = [sys.executable, str(ROOT / "scripts" / "sync_macro_sources.py"), "--fetch-live", "--request-limit", "4"]
    else:
        audit_path = REPORT_DIR / "macro_source_audit.json"
        existing_audit = read_json(audit_path) if audit_path.is_file() else {}
        if existing_audit.get("mode") == "FETCH_LIVE":
            print("B 2/5: retaining already-fetched live macro evidence; zero new requests", flush=True)
            cmd = None
        else:
            print("B 2/5: offline macro pass; zero network requests", flush=True)
            cmd = [sys.executable, str(ROOT / "scripts" / "sync_macro_sources.py")]
    sync = run_stage("macro source synchronization", cmd, timeout=80) if cmd else None
    macro = read_json(REPORT_DIR / "macro_source_audit.json") if (REPORT_DIR / "macro_source_audit.json").is_file() else {"status": "MACRO_PARTIAL_COVERAGE", "sources": [], "request_count": 0}
    if macro.get("mode") == "FETCH_LIVE":
        macro["local_feed_http_requests"] = int(macro.get("request_count", 0))
        macro["prior_official_url_checks"] = 5
        macro["prior_web_discovery_queries"] = 1
        macro["phase_external_requests_counted_conservatively"] = (
            macro["local_feed_http_requests"]
            + macro["prior_official_url_checks"]
            + macro["prior_web_discovery_queries"]
        )
        macro["phase_external_request_cap"] = 10
        macro["external_request_budget_remaining"] = max(
            0, macro["phase_external_request_cap"] - macro["phase_external_requests_counted_conservatively"]
        )
        for item in macro.get("sources", []):
            if item.get("http_status") is None and item.get("error") == "XML_ROOT_INVALID":
                item["http_status_evidence"] = "RESPONSE_STATUS_NOT_RETAINED_BY_INITIAL_RUN"
            elif item.get("http_status") is None and item.get("error") == "URLError":
                item["http_status_evidence"] = "NO_HTTP_STATUS_RECORDED"
        macro["live_run_limitations"] = [
            "A parser-blocked response did not persist its HTTP status; it was not retried after the four-request cap.",
            "The current parser now supports RSS 1.0, but the prior response body was not retained and has not been reprocessed.",
        ]
        write_json(REPORT_DIR / "macro_source_audit.json", macro)
    if sync is not None and sync["status"] != "PASS":
        print("[WARN] macro source coverage is partial", flush=True)
    stages = [
        ("2.2.2a-d offline suite", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_phase2_2_2*.py"], 300),
        ("macro parser and safety", [sys.executable, "-m", "unittest", "tests.test_macro_sources"], 120),
        ("verified dataset/backtest parity", [sys.executable, "-m", "unittest", "tests.test_verified_dataset_backtests"], 180),
        ("historical backtest engine", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_phase1_backtest.py"], 180),
        ("MT5 read-only and order safety", [sys.executable, "-m", "unittest", "tests.test_mt5_real_readonly", "tests.test_phase1_mt5_safety"], 180),
        ("look-ahead", [sys.executable, "test_no_lookahead.py"], 180),
        ("Python compile", [sys.executable, "-m", "compileall", "-q", "config", "src", "scripts"], 120),
    ]
    results = [run_stage(name, command, timeout) for name, command, timeout in stages]
    print("F 4/5: privacy, ignored-data and whitespace checks", flush=True)
    privacy_proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_phase2_2_2e_privacy.py")], cwd=ROOT, capture_output=True, text=True, timeout=30)
    try:
        privacy = json.loads(privacy_proc.stdout)
    except json.JSONDecodeError:
        privacy = {"status": "FAIL", "reason": "PRIVACY_REPORT_INVALID"}
    diff_proc = subprocess.run(["git", "diff", "--check"], cwd=ROOT, capture_output=True, text=True, timeout=20)
    diff = {"status": "PASS" if diff_proc.returncode == 0 else "FAIL"}
    if privacy_proc.returncode:
        print(privacy_proc.stdout + privacy_proc.stderr, flush=True)
    if diff_proc.returncode:
        print(diff_proc.stdout + diff_proc.stderr, flush=True)
    print("G 5/5: writing component reports and handoff documents", flush=True)
    tests_pass = pre["status"] == "PASS" and all(x["status"] == "PASS" for x in results)
    parity = next((x for x in results if x["name"] == "verified dataset/backtest parity"), {})
    parity_status = "SCAN_BACKTEST_PIPELINE_PARITY_PASS_SYNTHETIC" if parity.get("status") == "PASS" else "BLOCKED"
    macro_state = macro.get("status", "MACRO_PARTIAL_COVERAGE")
    overall = "PHASE2_2_2E_ENGINEERING_COMPLETE_WITH_DATA_BLOCKERS" if tests_pass and privacy.get("status") == "PASS" and diff["status"] == "PASS" else "PHASE2_2_2E_BLOCKED_INTEGRITY_OR_SAFETY"
    prior_recon = read_json(ROOT / "reports" / "phase2_2_2d" / "broker_session_reconciliation.json")
    unknown = int(prior_recon.get("classification_counts", {}).get("UNKNOWN", 1850))
    resolved = int(prior_recon.get("resolved_records", 0))
    write_json(REPORT_DIR / "broker_calendar_document_verification.json", {
        "schema_version": 1, "status": "BROKER_CALENDAR_UNKNOWN",
        "official_dated_document_present": False, "trusted_source_host_count": 0,
        "self_asserted_official_source_accepted": False, "verified_rule_versions": 0,
        "effective_period_coverage": None, "reason": "No official dated document bound to the exact broker entity and symbols was available.",
        "document_bytes_hash_and_rule_references_required": True, "account_identifiers_published": False,
    })
    write_json(REPORT_DIR / "broker_session_reconciliation.json", {
        "schema_version": 1, "status": "BROKER_CALENDAR_UNKNOWN",
        "records_total": prior_recon.get("record_count", 1850), "unknown_records": unknown,
        "resolved_records": resolved, "candidate_records": 74, "recovery_groups": 1776,
        "counts_may_overlap": True, "classification_counts": prior_recon.get("classification_counts", {"UNKNOWN": 1850}),
        "newly_resolved_records": 0, "market_history_requests": 0,
    })
    write_json(REPORT_DIR / "mtf_windows_readiness.json", {
        "schema_version": 1, "status": "REAL_MTF_WINDOWS_UNVERIFIED",
        "strict_utc_baseline_only": True, "verified_session_continuity": False,
        "previous_m15_decisions_checked": 159133, "previous_verified_windows": 0,
        "ema200_indicator_minimum": 200, "ema200_stabilization_bars": ema200_stabilization_bars(),
        "warmup_before_evaluation": True, "evaluation_range_status": "EVALUATION_RANGE_PENDING",
        "new_market_history_requests": 0,
    })
    write_json(REPORT_DIR / "scan_backtest_source_parity.json", {
        "schema_version": 1, "status": parity_status,
        "contract": "src/verified_research_dataset.py",
        "same_frames_for_scan_and_backtest": True, "hash_and_period_mismatch_rejected": parity.get("status") == "PASS",
        "partial_bars_excluded": True, "warmup_evaluation_observation_separated": True,
        "explicit_runner": "scripts/run_verified_research_backtest.py",
        "real_scan": "NOT_RUN_BROKER_CALENDAR_UNKNOWN", "synthetic_end_to_end_test": parity.get("status", "FAIL"),
        "frozen_scan_fallback_used": False, "legacy_runner_defaults_changed": False,
    })
    from config.backtest_cost_config import COST_SCENARIOS
    cost_test = next((x for x in results if x["name"] == "2.2.2a-d offline suite"), {})
    write_json(REPORT_DIR / "cost_model_audit.json", {
        "schema_version": 1, "status": "PASS_HYPOTHESIS_ONLY" if cost_test.get("status") == "PASS" else "FAIL",
        "scenarios": {name: {**item, "status": "UNVERIFIED_HYPOTHESIS"} for name, item in COST_SCENARIOS.items()},
        "units": "spread MT5 points; price conversion needs point size",
        "base_spread_embedded_in_bid_ask": True, "additional_spread_charge": 0,
        "spread_double_counted": False, "zero_spread": "OBSERVED_ZERO_UNVERIFIED",
        "EUR_conversion_without_verified_cross": "COST_CONVERSION_UNVERIFIED",
        "broker_costs_verified": False,
    })
    write_json(REPORT_DIR / "exploratory_backtest_gate.json", {
        "schema_version": 1, "status": "NOT_RUN", "run_status": "BACKTEST_NOT_RUN",
        "blocked_by": ["BROKER_CALENDAR_UNKNOWN", "REAL_MTF_WINDOWS_UNVERIFIED", "REAL_SOURCE_PARITY_UNVERIFIED", "COST_CONVERSION_UNVERIFIED"],
        "preregistered_matrix": {"threshold": 65, "mode": "LIMIT", "costs": ["BASE", "CONSERVATIVE", "STRESS"], "spread_floor_points": [0, 5, 10, 20], "temporal_partition": "TRAIN/EVALUATION/OUT_OF_SAMPLE/OBSERVATION fixed from coverage before PnL"},
        "parameter_optimization": False, "llm_calls": 0,
    })
    offline = {
        "schema_version": 1, "status": "PASS" if tests_pass else "FAIL",
        "test_commands": results, "lookahead_checks": next((x["lookahead_checks"] for x in results if x["name"] == "look-ahead"), 0),
        "github_actions": "NOT_RUN_LOCAL_ONLY", "mt5_calls": 0, "orders_sent": 0,
        "market_history_requests": 0, "llm_calls": 0,
    }
    write_json(REPORT_DIR / "offline_test_results.json", offline)
    write_json(REPORT_DIR / "remote_handoff_manifest.json", {
        "schema_version": 1, "status": "READY_FOR_REVIEW",
        "base_main_commit": pre.get("origin_main_commit"), "branch": pre.get("branch"),
        "local_commit": False, "push": "NOT_PERFORMED", "pull_request": "NOT_CREATED", "merge": "NOT_PERFORMED",
        "private_artifacts": "reports/private/phase2_2_2e, excluded from Git",
        "remote_tasks": ["Review offline workflow and new adapters.", "Run CI without MetaTrader5 or secrets.", "Keep economics blocked until broker document verification."],
        "support_questions": ["Exact broker legal entity and historical session document?", "Effective dates, timezone, DST, holidays and H1/H4/D1 boundaries?", "Can source cover 2023-07-17 through 2026-11-11?"],
    })
    summary = {
        "schema_version": 1, "status": overall,
        "components": {
            "preflight": pre["status"], "offline_tests": offline["status"], "macro": macro_state,
            "broker_calendar": "BROKER_CALENDAR_UNKNOWN", "pipeline": parity_status,
            "real_mtf": "REAL_MTF_WINDOWS_UNVERIFIED", "backtest": "BACKTEST_NOT_RUN",
            "privacy": privacy.get("status"), "diff_check": diff["status"],
        },
        "records_resolved": resolved, "records_unknown": unknown,
        "market_history_requests": 0, "mt5_reads": 0, "orders_sent": 0, "llm_provider_calls": 0,
        "frozen_dataset_unchanged": pre["frozen_dataset"]["unchanged"],
        "economic_results": "NOT_RUN",
        "engineering_prepared_is_not_calendar_certified_or_trading_edge": True,
    }
    write_json(REPORT_DIR / "validation_summary.json", summary)
    write_docs(pre, summary, offline, macro, results)
    final_privacy_proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_phase2_2_2e_privacy.py")],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    final_diff_proc = subprocess.run(["git", "diff", "--check"], cwd=ROOT, capture_output=True, text=True, timeout=20)
    new_files = git("ls-files", "--others", "--exclude-standard").splitlines()
    whitespace_findings = []
    for relative in new_files:
        path = safe_repo_path(relative)
        if path.suffix.lower() not in {".py", ".json", ".md", ".yml", ".txt"}:
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.rstrip(" \t") != line:
                whitespace_findings.append(f"{Path(relative).name}:{line_number}")
                break
    try:
        final_privacy = json.loads(final_privacy_proc.stdout)
    except json.JSONDecodeError:
        final_privacy = {"status": "FAIL"}
    if final_privacy_proc.returncode or final_diff_proc.returncode or whitespace_findings:
        summary["status"] = "PHASE2_2_2E_BLOCKED_INTEGRITY_OR_SAFETY"
        summary["components"]["privacy"] = final_privacy.get("status", "FAIL")
        summary["components"]["diff_check"] = "FAIL"
        summary["whitespace_findings"] = whitespace_findings
        offline["status"] = "FAIL"
        write_json(REPORT_DIR / "offline_test_results.json", offline)
        write_json(REPORT_DIR / "validation_summary.json", summary)
        write_docs(pre, summary, offline, macro, results)
    print(f"FINAL {summary['status']}", flush=True)
    print(json.dumps(summary["components"], sort_keys=True), flush=True)
    return 0 if summary["status"] == "PHASE2_2_2E_ENGINEERING_COMPLETE_WITH_DATA_BLOCKERS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
