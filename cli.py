import sys
import os
import json
import click
import uvicorn

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.prompt import Prompt


from agents.base import (
    assert_no_phi,
    SecurityException,
    default_audit_logger
)
from agents.models import (
    init_db,
    get_session,
    Case,
    Slide,
    SynopticReport,
    Alert,
    CaseStatus,
    QCGrade,
    MarginStatus,
    ValidationStatus
)
from agents.qc_worker import SlideQCAgent, WSIMetrics
from agents.synoptic_worker import SynopticValidatorAgent, SynopticReportInput, CAPProtocol
from agents.supervisor import PathMindSupervisor
from agents.llm_factory import LLMFactory, LLMProvider

console = Console()


@click.group()
def cli():
    """🔬 PathMind-Atlas: Autonomous Surgical Pathology QC & Synoptic Reporting Engine."""
    pass


@cli.command()
def init():
    """Initialize database tables and security keys."""
    init_db()
    console.print("[bold green]✔ PathMind-Atlas database initialized successfully.[/bold green]")


@cli.command()
@click.option("--accession", "-a", required=True, help="Anonymized accession ID (e.g. S26-1002)")
@click.option("--organ", "-o", default="BREAST", help="Organ system (e.g. BREAST, COLON, PROSTATE)")
@click.option("--specimen", "-s", required=True, help="Specimen description (e.g. Total Mastectomy)")
@click.option("--history", "-h", default="Invasive ductal carcinoma biopsy confirmed", help="De-identified clinical history")
def create_case(accession: str, organ: str, specimen: str, history: str):
    """Create a new de-identified pathology case."""
    try:
        init_db()
        assert_no_phi({"accession": accession, "history": history, "specimen": specimen})
        session = get_session()
        c = Case(
            accession_id_anonymized=accession,
            organ_system=organ.upper(),
            specimen_type=specimen,
            clinical_history_anonymized=history
        )
        session.add(c)
        session.commit()
        console.print(Panel(
            f"[bold green]Case Created Successfully![/bold green]\n"
            f"[cyan]Case ID:[/cyan] {c.case_id}\n"
            f"[cyan]Accession:[/cyan] {c.accession_id_anonymized}\n"
            f"[cyan]Organ:[/cyan] {c.organ_system}\n"
            f"[cyan]Specimen:[/cyan] {c.specimen_type}\n"
            f"[cyan]Status:[/cyan] {c.status.value}",
            title="PathMind Case Registration",
            border_style="green"
        ))
        session.close()
    except SecurityException as se:
        console.print(f"[bold red]🚫 Security Exception: PHI detected![/bold red] {se}")
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")


@cli.command()
@click.option("--case-id", "-c", required=True, help="Case ID or Accession ID")
@click.option("--blur-var", default=1200.0, type=float, help="Laplacian focus variance (>600 sharp, <300 blur)")
@click.option("--tissue-fold", default=0.0, type=float, help="Tissue fold area percentage (0-100)")
@click.option("--pen-mark", is_flag=True, default=False, help="Set flag if pen/ink mark is present on tissue")
@click.option("--air-bubble", default=0.0, type=float, help="Air bubble coverage percentage")
@click.option("--stain", default="H&E", help="Stain type (H&E, ER, PR, HER2)")
def qc_slide(case_id: str, blur_var: float, tissue_fold: float, pen_mark: bool, air_bubble: float, stain: str):
    """Run Whole Slide Imaging (WSI) Quality Control analysis."""
    init_db()
    agent = SlideQCAgent()
    metrics = WSIMetrics(
        case_id=case_id,
        stain_type=stain,
        laplacian_blur_var=blur_var,
        tissue_fold_pct=tissue_fold,
        pen_mark_detected=pen_mark,
        air_bubble_pct=air_bubble
    )
    result = agent.evaluate_slide(metrics, persist_to_db=True)

    grade_style = "green" if result.qc_grade == QCGrade.PASS else ("yellow" if result.qc_grade == QCGrade.WARN else "red")
    table = Table(title=f"WSI Quality Control Assessment: {result.slide_id}")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="white")

    table.add_row("QC Grade", f"[{grade_style}]{result.qc_grade.value}[/{grade_style}]")
    table.add_row("Quality Score", f"{result.qc_score:.1f}/100")
    table.add_row("Laplacian Blur Variance", f"{blur_var:.1f}")
    table.add_row("Tissue Fold Area %", f"{tissue_fold:.1f}%")
    table.add_row("Pen Mark Detected", str(pen_mark))
    table.add_row("Air Bubble Coverage %", f"{air_bubble:.1f}%")
    table.add_row("Identified Flags", ", ".join(result.flags) if result.flags else "None")
    table.add_row("Recommendation", result.recommended_action)

    console.print(table)
    if result.alerts_generated:
        console.print(Panel(
            "\n".join([f"• [{a['severity'].value}] {a['message']}" for a in result.alerts_generated]),
            title="Generated QC Alerts",
            border_style="red"
        ))


@cli.command()
@click.option("--case-id", "-c", required=True, help="Case ID or Accession ID")
@click.option("--protocol", "-p", default="BREAST_INVASIVE", help="CAP Protocol (e.g. BREAST_INVASIVE, COLORECTAL_RESECTION)")
@click.option("--histology", default="Invasive Ductal Carcinoma", help="Histologic subtype")
@click.option("--grade", default="Nottingham Grade 2", help="Histologic grade")
@click.option("--size", default=2.4, type=float, help="Tumor size in cm")
@click.option("--deep-margin", default=3.5, type=float, help="Deep margin clearance in mm")
@click.option("--radial-margin", default=1.2, type=float, help="Radial margin clearance in mm")
@click.option("--nodes-exam", default=12, type=int, help="Total regional lymph nodes examined")
@click.option("--nodes-pos", default=0, type=int, help="Number of positive lymph nodes")
@click.option("--pt", default="pT2", help="Pathologic T stage")
@click.option("--pn", default="pN0", help="Pathologic N stage")
def validate_synoptic(case_id: str, protocol: str, histology: str, grade: str, size: float,
                      deep_margin: float, radial_margin: float, nodes_exam: int, nodes_pos: int,
                      pt: str, pn: str):
    """Validate surgical pathology synoptic cancer reporting compliance."""
    init_db()
    agent = SynopticValidatorAgent()
    proto_enum = CAPProtocol[protocol] if protocol in CAPProtocol.__members__ else CAPProtocol.BREAST_INVASIVE

    req = SynopticReportInput(
        case_id=case_id,
        protocol_type=proto_enum,
        primary_tumor_site="Breast",
        histologic_type=histology,
        histologic_grade=grade,
        tumor_size_cm=size,
        deep_margin_mm=deep_margin,
        radial_margin_mm=radial_margin,
        nodes_examined=nodes_exam,
        nodes_positive=nodes_pos,
        pT=pt,
        pN=pn,
        biomarkers={"ER": "95%", "PR": "80%", "HER2": "Negative (1+)"}
    )
    result = agent.validate_report(req, persist_to_db=True)

    status_style = "green" if result.validation_status == ValidationStatus.VALID else ("yellow" if result.validation_status == ValidationStatus.INCOMPLETE else "red")
    table = Table(title=f"CAP Synoptic Protocol Validation: {result.report_id}")
    table.add_column("Protocol Attribute", style="cyan")
    table.add_column("Clinical Value", style="white")

    table.add_row("Protocol Standard", result.protocol_type.value)
    table.add_row("Validation Status", f"[{status_style}]{result.validation_status.value}[/{status_style}]")
    table.add_row("Completeness Score", f"{result.completeness_score:.1f}%")
    table.add_row("Margin Status", result.margin_status.value)
    table.add_row("Margin Clearance Summary", result.closest_margin_summary)
    table.add_row("AJCC Pathologic Stage", result.staging_summary)
    table.add_row("Protocol Violations", ", ".join(result.protocol_violations) if result.protocol_violations else "None")

    console.print(table)
    if result.alerts_generated:
        console.print(Panel(
            "\n".join([f"• [{a['severity'].value}] {a['message']}" for a in result.alerts_generated]),
            title="Synoptic Alerts",
            border_style="red"
        ))


@cli.command()
@click.option("--case-id", "-c", required=True, help="Case ID or Accession ID to orchestrate")
def review_case(case_id: str):
    """Supervisor Agent orchestration of complete case review."""
    init_db()
    supervisor = PathMindSupervisor()
    try:
        summary = supervisor.review_case(case_id)
        status_color = "green" if summary.overall_status == CaseStatus.SUPERVISOR_APPROVED else "red"
        
        console.print(Panel(
            f"[bold cyan]Case ID:[/bold cyan] {summary.case_id} ([white]{summary.accession_id_anonymized}[/white])\n"
            f"[bold cyan]Overall Consensus Status:[/bold cyan] [{status_color}]{summary.overall_status.value}[/{status_color}]\n"
            f"[bold cyan]WSI QC:[/bold cyan] {summary.qc_pass_count} Passed / {summary.slides_reviewed_count} Total ({summary.qc_reject_count} Rejected)\n"
            f"[bold cyan]Synoptic Reports:[/bold cyan] {summary.synoptic_reports_count} ({summary.validation_status.value}, Margin: {summary.margin_status.value})\n"
            f"[bold cyan]Active Alerts:[/bold cyan] {summary.critical_alerts_count}\n"
            f"[bold cyan]Recommended Action:[/bold cyan] {summary.recommended_action}\n\n"
            f"[bold cyan]Supervisor Clinical Note:[/bold cyan]\n{summary.supervisor_consensus}",
            title="🔬 PathMind Supervisor Final Review",
            border_style=status_color
        ))
    except Exception as e:
        console.print(f"[bold red]Review failed:[/bold red] {e}")


@cli.command()
def audit_verify():
    """Verify cryptographic HMAC-SHA256 audit chain integrity."""
    is_valid, reason = default_audit_logger.verify_integrity()
    events = default_audit_logger.get_events()
    if is_valid:
        console.print(Panel(
            f"[bold green]✔ Tamper-Evident HMAC-SHA256 Audit Chain Verified![/bold green]\n"
            f"[cyan]Total Chained Events:[/cyan] {len(events)}\n"
            f"[cyan]Head Block Hash:[/cyan] {default_audit_logger.last_hash}\n"
            f"[cyan]Status:[/cyan] All hashes and signatures strictly authentic.",
            title="Cryptographic Audit Integrity",
            border_style="green"
        ))
    else:
        console.print(Panel(
            f"[bold red]❌ Audit Chain Verification FAILED![/bold red]\n"
            f"[yellow]Reason:[/yellow] {reason}",
            title="Security Audit Breach",
            border_style="red"
        ))


@cli.command()
def chat():
    """Interactive zero-PHI clinical assistant session."""
    init_db()
    supervisor = PathMindSupervisor()
    console.print(Panel(
        "[bold cyan]PathMind Atlas Clinical Operations Console[/bold cyan]\n"
        "Ask questions about pathology QC, CAP Synoptic protocols, or specific cases.\n"
        "Type [yellow]'exit'[/yellow] or [yellow]'quit'[/yellow] to end session.\n"
        "[red]Note: Strict Zero-PHI guards active. Never input patient identifiers.[/red]",
        border_style="cyan"
    ))

    while True:
        try:
            q = Prompt.ask("\n[bold green]Pathologist / User[/bold green]")
            if q.lower().strip() in ["exit", "quit", "q"]:
                console.print("[yellow]Exiting PathMind Console. Goodbye![/yellow]")
                break
            if not q.strip():
                continue

            resp = supervisor.chat_query(q)
            console.print(f"\n[bold cyan]PathMind Supervisor:[/bold cyan]\n{resp}")
        except SecurityException as se:
            console.print(f"[bold red]🚫 PHI Blocked:[/bold red] {se}")
        except KeyboardInterrupt:
            break


@cli.command()
@click.option("--host", default="127.0.0.1", help="Host address")
@click.option("--port", default=8000, type=int, help="Port number")
@click.option("--reload", is_flag=True, default=False, help="Enable auto-reload")
def serve(host: str, port: int, reload: bool):
    """Launch FastAPI 0.111+ REST API Server."""
    console.print(f"[bold green]Starting PathMind Atlas API Server on http://{host}:{port}...[/bold green]")
    uvicorn.run("agents.api:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    cli()
