"""footprint-scanner command line."""

from __future__ import annotations

import asyncio
import logging
import math
import shutil
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import __version__
from .config import Config, load_config, safe_case_id
from .identifiers import ClientIdentifiers, load_from_tracker, load_from_yaml
from .logging_setup import add_file_log, set_secrets, setup_logging
from .pipeline import AuthorizationError, Scanner, check_authorization
from .queries import Query, build_queries
from .search import ResponseCache, make_providers
from .sites import SiteDirectory, load_sites

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Discover and document where a consenting client appears online. "
    "Never sends removal requests; every request is filed manually after human review.",
)

# Turkish characters must print on Windows consoles using a legacy code page.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure") and (_stream.encoding or "").lower() not in ("utf-8", "utf8"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

console = Console()
log = logging.getLogger("footprint_scanner")

TrackerOpt = typer.Option(..., "--tracker", "-t", exists=True, dir_okay=False, help="Tracker .xlsx to read (never overwritten).")
ClientYamlOpt = typer.Option(None, "--client-yaml", exists=True, dir_okay=False, help="Read identifiers from client.yaml instead of Client Profile.")
ConfigOpt = typer.Option(Path("config.yaml"), "--config", "-c", help="config.yaml path.")
ProvidersOpt = typer.Option(None, "--providers", help="Comma-separated, e.g. serpapi,brave (overrides config).")
MaxQueriesOpt = typer.Option(None, "--max-queries", min=1, help="Run only the first N queries (highest priority first).")
AuthOpt = typer.Option(False, "--i-have-authorization", help="Proceed although Client Profile doesn't say 'Yes'.")
VerboseOpt = typer.Option(False, "--verbose", "-v")


def _load(config: Path, tracker: Optional[Path], client_yaml: Optional[Path]) -> tuple[Config, ClientIdentifiers, SiteDirectory]:
    cfg = load_config(config)
    set_secrets(cfg.secrets())
    if client_yaml:
        ident = load_from_yaml(client_yaml)
    elif tracker:
        ident = load_from_tracker(tracker)
    else:
        raise typer.BadParameter("Give --tracker or --client-yaml")
    sites = load_sites(cfg.resolve(cfg.paths.brokers_file), cfg.resolve(cfg.paths.categories_file))
    return cfg, ident, sites


def _provider_names(cfg: Config, providers: Optional[str]) -> list[str]:
    names = [p.strip().lower() for p in (providers.split(",") if providers else cfg.search.providers) if p.strip()]
    if not names:
        raise typer.BadParameter("No search providers configured")
    return names


def _provider_options(cfg: Config) -> dict[str, dict]:
    return {"serpapi": cfg.search.serpapi, "brave": cfg.search.brave}


def _print_queries(queries: list[Query]) -> None:
    table = Table(title=f"Query matrix ({len(queries)} queries)", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("Kind")
    table.add_column("Query", overflow="fold")
    for i, q in enumerate(queries, 1):
        table.add_row(str(i), q.kind, q.text)
    console.print(table)


def _estimate(cfg: Config, ident: ClientIdentifiers, queries: list[Query], names: list[str]) -> None:
    """Worst-case API calls for a run, minus pages already cached."""
    providers = make_providers(names, cfg.api_keys, _provider_options(cfg), require_keys=False)
    cache = ResponseCache(cfg.client_dir(ident.case_id) / "cache" / "search")
    table = Table(title="Estimated API calls (upper bound)")
    for col in ("Provider", "Pages/query", "Calls needed", "Already cached"):
        table.add_column(col)
    total = 0
    for p in providers:
        pages = math.ceil(cfg.search.max_results_per_query / p.page_size)
        if p.max_page_index is not None:
            pages = min(pages, p.max_page_index + 1)
        cached = needed = 0
        for q in queries:
            for i in range(pages):
                req = p.build_request(q.text, i)
                if cache.get(p.name, ResponseCache.key(p.name, req)) is not None:
                    cached += 1
                else:
                    needed += 1
        total += needed
        key_note = "" if cfg.api_keys.get(p.name) else " (no API key set!)"
        table.add_row(p.name + key_note, str(pages), str(needed), str(cached))
    console.print(table)
    console.print(f"Budget (search.max_api_calls): {cfg.search.max_api_calls}. Worst case needs {total}.")


def _run(mode: str, tracker: Path, client_yaml, config, providers, max_queries, dry_run, i_have_authorization,
         max_pages, no_screenshots, verbose) -> None:
    setup_logging(verbose)
    try:
        cfg, ident, sites = _load(config, tracker, client_yaml)
    except (ValueError, OSError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(2) from None
    names = _provider_names(cfg, providers)
    if max_pages is not None:
        cfg.fetch.max_pages = max_pages
    if no_screenshots:
        cfg.fetch.screenshots = False

    queries = build_queries(ident, sites, cfg.search.site_query_mode, cfg.search.site_group_size)
    if max_queries:
        queries = queries[:max_queries]

    if dry_run:
        _print_queries(queries)
        _estimate(cfg, ident, queries, names)
        if not ident.authorized and not i_have_authorization:
            console.print("[yellow]Note:[/yellow] authorization is not recorded as 'Yes'; a real run will refuse.")
        console.print("[green]Dry run: nothing was sent to any API or website.[/green]")
        return

    try:
        basis = check_authorization(ident, i_have_authorization)
    except AuthorizationError as exc:
        console.print(f"[red]Refusing to scan:[/red] {exc}")
        raise typer.Exit(3) from None

    try:
        provider_objs = make_providers(names, cfg.api_keys, _provider_options(cfg))
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}. Set it in .env (see .env.example).")
        raise typer.Exit(2) from None

    with Progress(
        TextColumn("[bold]{task.description}"), BarColumn(), MofNCompleteColumn(), TimeElapsedColumn(),
        console=console, transient=False,
    ) as progress:
        tasks: dict[str, int] = {}

        def on_progress(stage: str, done: int, total: int) -> None:
            if stage not in tasks:
                tasks[stage] = progress.add_task({"search": "Searching", "pages": "Analyzing pages"}[stage], total=total)
            progress.update(tasks[stage], completed=done, total=total)

        scanner = Scanner(
            cfg, ident, sites, provider_objs, mode=mode, tracker_path=tracker, authorization_basis=basis,
            max_queries=max_queries, progress=on_progress,
        )
        handler = add_file_log(scanner.run_dir / "scan.log")
        log.info("footprint-scanner %s %s for %s; authorization basis: %s", __version__, mode, ident.case_id, basis)
        try:
            result = asyncio.run(scanner.run())
        except Exception as exc:
            log.exception("Run failed")
            console.print(f"[red]Run failed:[/red] {exc}")
            raise typer.Exit(1) from None
        finally:
            logging.getLogger().removeHandler(handler)
            handler.close()

    from .report import write_report

    report = write_report(result)
    console.print()
    console.print(f"[green]Tracker copy:[/green] {result.output_tracker}")
    console.print(f"[green]Report:[/green]       {report}")
    console.print(
        f"New rows: {len(result.new_rows)}  |  already tracked: {len(result.already_tracked)}  |  "
        f"API calls: {sum(result.api_calls.values())}  |  errors: {len(result.errors)}"
    )
    if mode == "rescan":
        console.print(f"Re-appeared: {len(result.reappeared)}  |  possibly removed: {len(result.possibly_removed)}")
    console.print("[bold]Nothing was sent to any site. Review every row before filing a request.[/bold]")


@app.command()
def scan(
    tracker: Path = TrackerOpt,
    client_yaml: Optional[Path] = ClientYamlOpt,
    config: Path = ConfigOpt,
    providers: Optional[str] = ProvidersOpt,
    max_queries: Optional[int] = MaxQueriesOpt,
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the queries and cost estimate; spend nothing."),
    i_have_authorization: bool = AuthOpt,
    max_pages: Optional[int] = typer.Option(None, "--max-pages", min=0, help="Override fetch.max_pages."),
    no_screenshots: bool = typer.Option(False, "--no-screenshots"),
    verbose: bool = VerboseOpt,
) -> None:
    """Search, analyze and append new findings to a timestamped copy of the tracker."""
    _run("scan", tracker, client_yaml, config, providers, max_queries, dry_run, i_have_authorization,
         max_pages, no_screenshots, verbose)


@app.command()
def rescan(
    tracker: Path = TrackerOpt,
    client_yaml: Optional[Path] = ClientYamlOpt,
    config: Path = ConfigOpt,
    providers: Optional[str] = ProvidersOpt,
    max_queries: Optional[int] = MaxQueriesOpt,
    dry_run: bool = typer.Option(False, "--dry-run"),
    i_have_authorization: bool = AuthOpt,
    max_pages: Optional[int] = typer.Option(None, "--max-pages", min=0),
    no_screenshots: bool = typer.Option(False, "--no-screenshots"),
    verbose: bool = VerboseOpt,
) -> None:
    """Re-check every existing row, flag re-appearances and 404s, and append new URLs."""
    _run("rescan", tracker, client_yaml, config, providers, max_queries, dry_run, i_have_authorization,
         max_pages, no_screenshots, verbose)


@app.command()
def queries(
    tracker: Optional[Path] = typer.Option(None, "--tracker", "-t", exists=True, dir_okay=False),
    client_yaml: Optional[Path] = ClientYamlOpt,
    config: Path = ConfigOpt,
) -> None:
    """Print the query matrix only (no network)."""
    setup_logging(False)
    cfg, ident, sites = _load(config, tracker, client_yaml)
    _print_queries(build_queries(ident, sites, cfg.search.site_query_mode, cfg.search.site_group_size))


LangOpt = typer.Option(None, "--lang", help="en or tr (default: report.lang in config).")
IncludeUnverifiedOpt = typer.Option(
    False, "--include-unverified",
    help="Also include rows still 'Unverified' (they may be other people with the same name).",
)
PreparedByOpt = typer.Option(None, "--prepared-by", help="Printed on the cover.")
OrderOpt = typer.Option(
    "id", "--order",
    help="id = same order as the tracker (default); severity = Critical first. Item numbers are always tracker IDs.",
)


def _customer_report(fmt: str, tracker: Path, client_yaml, config: Path, lang, include_unverified: bool,
                     prepared_by, out: Optional[Path], order: str = "id") -> None:
    setup_logging(False)
    cfg, ident, _sites = _load(config, tracker, client_yaml)
    r = cfg.report
    language = (lang or r.lang or "en").lower()
    if language not in ("en", "tr"):
        raise typer.BadParameter("--lang must be en or tr")
    if order not in ("id", "severity"):
        raise typer.BadParameter("--order must be id or severity")
    from datetime import datetime

    now = datetime.now()
    target = out or cfg.client_dir(ident.case_id) / "reports" / (
        f"{safe_case_id(ident.case_id)}_report_{now:%Y-%m-%d_%H%M}_{language}.{fmt}"
    )
    who = prepared_by if prepared_by is not None else r.prepared_by
    try:
        if fmt == "docx":
            from .docx_report import generate as gen_docx

            path, n = gen_docx(tracker, ident, target, lang=language, include_unverified=include_unverified,
                               prepared_by=who, today=now.date(), order=order)
        else:
            from .pdf_report import generate as gen_pdf

            path, n = gen_pdf(tracker, ident, target, lang=language, include_unverified=include_unverified,
                              prepared_by=who, font_regular=r.font or None, font_bold=r.font_bold or None,
                              today=now.date(), order=order)
    except PermissionError:
        console.print(f"[red]Error:[/red] cannot write {target} - is it open in Word/Acrobat? Close it and retry.")
        raise typer.Exit(2) from None
    except RuntimeError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(2) from None
    scope = "Confirmed + Likely" + (" + Unverified" if include_unverified else "")
    console.print(f"[green]{fmt.upper()}:[/green] {path}")
    console.print(f"{n} item(s) included ({scope}; namesakes never included).")
    if n == 0 and not include_unverified:
        console.print("[yellow]No Confirmed/Likely rows yet.[/yellow] Review Match Confidence in the tracker, "
                      "or use --include-unverified.")
    console.print("[bold]The report contains the client's personal data; share it only with the client.[/bold]")


@app.command()
def docx(
    tracker: Path = TrackerOpt,
    client_yaml: Optional[Path] = ClientYamlOpt,
    config: Path = ConfigOpt,
    lang: Optional[str] = LangOpt,
    include_unverified: bool = IncludeUnverifiedOpt,
    prepared_by: Optional[str] = PreparedByOpt,
    order: str = OrderOpt,
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Output .docx (default: client folder/reports/)."),
) -> None:
    """Build the customer report as a Word document (edit in Word, then print or save as PDF):
    findings with screenshots and a Remove / Keep tick box per item. Sends nothing anywhere."""
    _customer_report("docx", tracker, client_yaml, config, lang, include_unverified, prepared_by, out, order)


@app.command()
def pdf(
    tracker: Path = TrackerOpt,
    client_yaml: Optional[Path] = ClientYamlOpt,
    config: Path = ConfigOpt,
    lang: Optional[str] = LangOpt,
    include_unverified: bool = IncludeUnverifiedOpt,
    prepared_by: Optional[str] = PreparedByOpt,
    order: str = OrderOpt,
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Output .pdf (default: client folder/reports/)."),
) -> None:
    """Build the customer report as a fillable PDF (on-screen Remove / Keep fields). Sends nothing anywhere."""
    _customer_report("pdf", tracker, client_yaml, config, lang, include_unverified, prepared_by, out, order)


@app.command()
def purge(
    client: str = typer.Option(..., "--client", help="Client ID / Case #, e.g. CASE-2026-001"),
    config: Path = ConfigOpt,
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation."),
) -> None:
    """Delete ALL local data for a client: cached API responses, evidence, run reports/logs and
    tracker copies in the client's folder. The original input tracker is not touched."""
    setup_logging(False)
    cfg = load_config(config)
    try:
        folder = cfg.client_dir(safe_case_id(client))
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(2) from None
    root = cfg.resolve(cfg.paths.clients_dir).resolve()
    folder = folder.resolve()
    if folder.parent != root:
        console.print("[red]Refusing: client folder is outside the clients directory.[/red]")
        raise typer.Exit(2) from None
    if not folder.exists():
        console.print(f"Nothing to purge: {folder} does not exist.")
        return
    files = [p for p in folder.rglob("*") if p.is_file()]
    size = sum(p.stat().st_size for p in files)
    groups = {}
    for p in files:
        top = p.relative_to(folder).parts[0] if len(p.relative_to(folder).parts) > 1 else "(tracker copies)"
        groups[top] = groups.get(top, 0) + 1
    console.print(f"About to permanently delete [bold]{folder}[/bold]: {len(files)} files, {size / 1e6:.1f} MB")
    for k, v in sorted(groups.items()):
        console.print(f"  {k}: {v} files")
    if not yes and not typer.confirm("Delete all of it?", default=False):
        console.print("Aborted; nothing deleted.")
        raise typer.Exit(1) from None
    shutil.rmtree(folder)
    console.print(f"[green]Purged {client}.[/green] Remember to also delete the client's original tracker "
                  "and any copies outside this folder according to the retention date on Client Profile.")


@app.command()
def version() -> None:
    """Print the version."""
    console.print(__version__)


if __name__ == "__main__":
    app()
