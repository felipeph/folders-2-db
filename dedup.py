import argparse
import sys
import os
import time
from typing import List, Dict, Any, Optional

# Garante saída UTF-8 no terminal Windows
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

from rich.console import Console
from rich.prompt import Prompt, Confirm

from scanner import preflight_check, scan_directory, chunked_iterable
from metadata import get_file_stats, get_partial_hash, batch_get_exif
from database import Database
from logger import AuditLogger
from reporter import generate_reports
from db_reporter import generate_report as generate_dashboard
from dedup_server import start_server as run_dedup_webapp
from ui import VerticalProgress
import webbrowser

DBS_DIR = "dbs"
if not os.path.exists(DBS_DIR):
    os.makedirs(DBS_DIR)

console = Console()

def run_index(project_name: str, directory: str):
    console.print(f"[bold blue]Iniciando Indexação do projeto '{project_name}' na pasta '{directory}'[/bold blue]")
    if not preflight_check(directory):
        return

    logger = AuditLogger(project_name)
    db_path = os.path.join(DBS_DIR, f"{project_name}.sqlite")
    
    start_time = time.time()
    total_files_scanned = 0
    processed = 0
    skipped = 0
    errors = 0

    # Primeira passada rápida para contar os arquivos (Anti-jitter UX)
    with console.status("[cyan]Fazendo varredura inicial para contagem de arquivos...[/cyan]"):
        file_list = list(scan_directory(directory))
    total_files = len(file_list)
    total_files_scanned = total_files
    
    if total_files == 0:
        console.print("[yellow]Nenhum arquivo de mídia encontrado.[/yellow]")
        return
        
    console.print(f"[bold green]{total_files} arquivos encontrados.[/bold green] Começando extração...")

    with Database(db_path) as db:
        # Exibição vertical customizada com Rich
        with VerticalProgress(
            f"Indexando arquivos do projeto '{project_name}'",
            total=total_files,
            console=console
        ) as progress:
            
            # Processa em lotes de 100 para o exiftool (External Tool Batching)
            for batch in chunked_iterable(file_list, 100):
                # Filtra os que já estão no banco e não mudaram
                to_process = []
                for filepath in batch:
                    progress.update(current_file=filepath)
                    try:
                        size, mtime = get_file_stats(filepath)
                        if db.file_exists(filepath, size, mtime):
                            skipped += 1
                            progress.update(
                                advance=1,
                                current_file=filepath,
                                extra_info=f"[green]{processed} novos[/green] | [yellow]{skipped} mantidos[/yellow] | [red]{errors} erros[/red]"
                            )
                        else:
                            to_process.append((filepath, size, mtime))
                    except Exception:
                        errors += 1
                        progress.update(
                            advance=1,
                            current_file=filepath,
                            extra_info=f"[green]{processed} novos[/green] | [yellow]{skipped} mantidos[/yellow] | [red]{errors} erros[/red]"
                        )
                        
                if not to_process:
                    continue
                    
                # Chama exiftool em batch
                paths_to_process = [item[0] for item in to_process]
                progress.update(
                    current_file=f"Extraindo metadados EXIF ({len(paths_to_process)} arquivos)..."
                )
                exif_data_map = batch_get_exif(paths_to_process)
                
                # Inserir no DB
                for filepath, size, mtime in to_process:
                    progress.update(current_file=filepath)
                    try:
                        exif = exif_data_map.get(filepath, {})
                        
                        # Se não tiver EXIF com datas originais, ou se formos puristas, calculamos hash parcial
                        # Como segurança extra, calculamos o hash parcial para todos, já que é muito rápido
                        p_hash = get_partial_hash(filepath)
                        
                        filename = os.path.basename(filepath)
                        db.insert_or_update_file(filepath, filename, size, mtime, p_hash, exif)
                        processed += 1
                    except Exception:
                        errors += 1
                        
                    progress.update(
                        advance=1,
                        current_file=filepath,
                        extra_info=f"[green]{processed} novos[/green] | [yellow]{skipped} mantidos[/yellow] | [red]{errors} erros[/red]"
                    )

    elapsed = time.time() - start_time
    logger.log_execution("index", total_files_scanned, processed, skipped, errors, elapsed)
    console.print(f"\n[bold green]Indexação concluída no banco '{db_path}'![/bold green]")


def run_compare(project_name: str, directory: str):
    console.print(f"[bold blue]Comparando pasta '{directory}' contra o banco do projeto '{project_name}'[/bold blue]")
    if not preflight_check(directory):
        return

    db_path = os.path.join(DBS_DIR, f"{project_name}.sqlite")
    if not os.path.exists(db_path):
        console.print(f"[bold red]Erro:[/bold red] O banco de dados '{db_path}' não foi encontrado. Você já indexou o projeto?")
        return

    start_time = time.time()
    duplicates_found = []
    
    with console.status("[cyan]Fazendo varredura inicial para contagem de arquivos...[/cyan]"):
        file_list = list(scan_directory(directory))
    total_files = len(file_list)
    
    if total_files == 0:
        console.print("[yellow]Nenhum arquivo de mídia encontrado na nova pasta.[/yellow]")
        return
        
    console.print(f"[bold green]{total_files} arquivos encontrados na pasta.[/bold green] Comparando...")

    with Database(db_path) as db:
        with VerticalProgress(
            f"Verificando duplicatas no projeto '{project_name}'",
            total=total_files,
            console=console
        ) as progress:
            
            for filepath in file_list:
                progress.update(current_file=filepath)
                try:
                    size, _ = get_file_stats(filepath)
                    p_hash = get_partial_hash(filepath)
                    
                    db_duplicate = db.find_duplicate(size, p_hash)
                    
                    if db_duplicate:
                        duplicates_found.append({
                            "scanned_file": filepath,
                            "db_file": db_duplicate,
                            "size_bytes": size
                        })
                except Exception:
                    pass
                    
                dup_count = len(duplicates_found)
                dup_info = (
                    f"[bold yellow]{dup_count} duplicata(s) encontrada(s)[/bold yellow]"
                    if dup_count > 0
                    else "[green]0 duplicatas[/green]"
                )
                progress.update(advance=1, current_file=filepath, extra_info=dup_info)

    elapsed = time.time() - start_time
    console.print(f"\nVarredura concluída em {elapsed:.1f} segundos.")
    console.print(f"Total de {len(duplicates_found)} duplicatas encontradas!")
    
    if duplicates_found:
        json_rep, html_rep = generate_reports(project_name, total_files, duplicates_found)
        console.print(f"\n[bold green]Relatórios Gerados com Sucesso![/bold green]")
        console.print(f"JSON: [cyan]{json_rep}[/cyan]")
        console.print(f"HTML: [cyan]{html_rep}[/cyan]")
        
        if Confirm.ask("\n[bold cyan]Deseja abrir a Central Interativa no navegador para revisar e apagar duplicatas?[/bold cyan]", default=True):
            run_dedup_webapp(initial_report=os.path.basename(json_rep))


def run_internal(project_name: str):
    console.print(f"[bold blue]Buscando Duplicatas Internas no projeto '{project_name}'[/bold blue]")
    console.print("[dim]Detectando cópias idênticas e versões da mesma foto em resoluções/tamanhos reduzidos com tolerância de fuso horário...[/dim]")
    
    db_path = os.path.join(DBS_DIR, f"{project_name}.sqlite")
    if not os.path.exists(db_path):
        console.print(f"[bold red]Erro:[/bold red] O banco de dados '{db_path}' não foi encontrado. Você já indexou o projeto?")
        return

    start_time = time.time()
    with Database(db_path) as db:
        total_files = db.get_total_files()
        console.print(f"[cyan]O projeto possui {total_files} arquivos indexados.[/cyan] Analisando duplicatas internas...")
        
        with console.status("[cyan]Executando análise intra-banco (EXIF, fusos horários e pHash sob demanda)...[/cyan]"):
            duplicates_found = db.find_internal_duplicates()

    elapsed = time.time() - start_time
    console.print(f"\nVarredura interna concluída em {elapsed:.2f} segundos!")
    console.print(f"Total de {len(duplicates_found)} duplicata(s) encontrada(s)!")

    if duplicates_found:
        report_name = f"{project_name}_internal"
        json_rep, html_rep = generate_reports(report_name, total_files, duplicates_found)
        console.print(f"\n[bold green]Relatórios de Duplicatas Internas Gerados com Sucesso![/bold green]")
        console.print(f"JSON: [cyan]{json_rep}[/cyan]")
        console.print(f"HTML: [cyan]{html_rep}[/cyan]")

        if Confirm.ask("\n[bold cyan]Deseja abrir a Central Interativa no navegador para revisar e limpar essas duplicatas?[/bold cyan]", default=True):
            run_dedup_webapp(initial_internal=project_name)
    else:
        console.print("[green]Nenhuma duplicata interna encontrada neste banco.[/green]")


def run_compare_db(project_a: str, project_b: str, deep: bool = False):
    mode_label = " (Modo Profundo / Qualidades & Fusos)" if deep else " (Modo Exato)"
    console.print(f"[bold blue]Comparando Projeto '{project_a}' contra Projeto '{project_b}'{mode_label}[/bold blue]")
    
    db_a_path = os.path.join(DBS_DIR, f"{project_a}.sqlite")
    db_b_path = os.path.join(DBS_DIR, f"{project_b}.sqlite")
    
    if not os.path.exists(db_a_path):
        console.print(f"[bold red]Erro:[/bold red] O banco de dados '{db_a_path}' não foi encontrado.")
        return
    if not os.path.exists(db_b_path):
        console.print(f"[bold red]Erro:[/bold red] O banco de dados '{db_b_path}' não foi encontrado.")
        return

    start_time = time.time()
    
    with Database(db_a_path) as db_a:
        total_a = db_a.get_total_files()
        console.print(f"[cyan]O projeto A possui {total_a} arquivos indexados.[/cyan] Analisando duplicatas...")
        
        status_msg = "[cyan]Rodando comparação profunda entre bancos (EXIF, fusos horários e pHash)...[/cyan]" if deep else "[cyan]Rodando query de Inner Join via SQLite...[/cyan]"
        with console.status(status_msg):
            if deep:
                duplicates_found = db_a.compare_with_db_deep(db_b_path)
            else:
                duplicates_found = db_a.compare_with_db(db_b_path)

    elapsed = time.time() - start_time
    dup_type_text = "duplicatas (incluindo qualidades inferiores e fusos horários)" if deep else "duplicatas exatas"
    console.print(f"\nCruzamento concluído em {elapsed:.2f} segundos!")
    console.print(f"Total de {len(duplicates_found)} {dup_type_text} encontradas!")
    
    if duplicates_found:
        report_name = f"{project_a}_vs_{project_b}" + ("_deep" if deep else "")
        json_rep, html_rep = generate_reports(report_name, total_a, duplicates_found)
        console.print(f"\n[bold green]Relatórios de Cruzamento Gerados com Sucesso![/bold green]")
        console.print(f"JSON: [cyan]{json_rep}[/cyan]")
        console.print(f"HTML: [cyan]{html_rep}[/cyan]")

        if Confirm.ask("\n[bold cyan]Deseja abrir a Central Interativa no navegador para revisar e apagar duplicatas?[/bold cyan]", default=True):
            run_dedup_webapp(initial_safe=project_a, initial_cand=project_b, initial_deep=deep)


def run_dashboard(project_name: str):
    console.print(f"[bold blue]Gerando Dashboard HTML para o projeto '{project_name}'[/bold blue]")
    db_path = os.path.join(DBS_DIR, f"{project_name}.sqlite")
    
    if not os.path.exists(db_path):
        console.print(f"[bold red]Erro:[/bold red] O banco de dados '{db_path}' não foi encontrado. Você já indexou o projeto?")
        return

    result = generate_dashboard(db_path)
    if result:
        json_path, html_path = result
        console.print(f"\n[bold green]Dashboard Gerado com Sucesso![/bold green]")
        console.print(f"Abrindo no navegador: [cyan]{html_path}[/cyan]")
        webbrowser.open(f"file://{os.path.abspath(html_path)}")


def run_compute_phash(project_name: Optional[str] = None, all_projects: bool = False, workers: int = 8, batch_size: int = 500):
    """
    Executa o pipeline de cálculo em lote de pHash para um ou todos os projetos indexados.
    Completamente resiliente, multithreaded e retomável de onde parou.
    """
    projects_to_run = []
    if all_projects:
        projects_to_run = get_existing_projects()
        if not projects_to_run:
            console.print("[yellow]Nenhum banco de dados SQLite encontrado na pasta 'dbs/'.[/yellow]")
            return
    elif project_name:
        projects_to_run = [project_name]
    else:
        console.print("[red]Nenhum projeto especificado.[/red]")
        return

    console.print(f"[bold blue]Iniciando cálculo de pHash ({len(projects_to_run)} projeto(s), {workers} threads, lotes de {batch_size})[/bold blue]")

    for idx, proj in enumerate(projects_to_run, 1):
        db_path = os.path.join(DBS_DIR, f"{proj}.sqlite")
        if not os.path.exists(db_path):
            console.print(f"[red]Erro:[/red] Banco '{db_path}' não encontrado.")
            continue

        console.print(f"\n[bold cyan][{idx}/{len(projects_to_run)}] Analisando banco do projeto '{proj}'...[/bold cyan]")
        
        try:
            with Database(db_path) as db:
                total_pending = db.get_pending_phash_count()
                if total_pending == 0:
                    total_files = db.get_total_files()
                    console.print(f"[green]✓ Projeto '{proj}': Todos os hashes pHash já estão preenchidos! ({total_files:,} arquivos no banco)[/green]")
                    continue

                console.print(f"Encontradas [bold yellow]{total_pending:,}[/bold yellow] imagens pendentes de pHash.")
                
                with VerticalProgress(
                    f"Calculando pHash: {proj}",
                    total=total_pending,
                    console=console
                ) as progress:
                    def on_progress(advance: int, current_file: str, success: int, errors: int):
                        progress.update(
                            advance=advance,
                            current_file=current_file,
                            extra_info=f"[green]{success:,} hashes[/green] | [red]{errors:,} erros[/red]"
                        )

                    stats = db.populate_phashes(
                        max_workers=workers,
                        batch_size=batch_size,
                        progress_callback=on_progress
                    )

                console.print(f"[bold green]✓ Concluído projeto '{proj}':[/bold green] {stats['processed']:,} processados ({stats['success']:,} calculados, {stats['errors']:,} erros, {stats['remaining']:,} restantes).")

        except KeyboardInterrupt:
            console.print(f"\n[bold yellow]Operação pausada pelo usuário (Ctrl+C).[/bold yellow]")
            console.print(f"[green]O progresso calculado até este momento foi gravado com segurança no banco SQLite '{proj}'.[/green]")
            console.print("[cyan]Para continuar de onde parou, basta executar o comando novamente.[/cyan]\n")
            return
        except FileNotFoundError as fnf_err:
            console.print(f"[bold red]Atenção:[/bold red] {fnf_err}")
            if len(projects_to_run) > 1 and idx < len(projects_to_run):
                if not Confirm.ask("Deseja pular este projeto e tentar o próximo?", default=True):
                    return
            else:
                return
        except Exception as e:
            console.print(f"[bold red]Erro durante o cálculo de pHash para '{proj}':[/bold red] {e}")
            if len(projects_to_run) > 1 and idx < len(projects_to_run):
                if not Confirm.ask("Deseja pular este projeto e tentar o próximo?", default=True):
                    return


def get_existing_projects() -> List[str]:
    projects = []
    if os.path.exists(DBS_DIR):
        for f in os.listdir(DBS_DIR):
            if f.endswith(".sqlite"):
                projects.append(f[:-7])
    return sorted(projects)

def ask_project(prompt_text: str, allow_new: bool = True) -> str:
    projects = get_existing_projects()
    if not projects:
        return Prompt.ask(prompt_text).strip()
        
    console.print("\n[cyan]Projetos existentes:[/cyan]")
    for i, p in enumerate(projects, 1):
        console.print(f" [bold yellow][{i}][/bold yellow] {p}")
    
    if allow_new:
        console.print(" [bold yellow][N][/bold yellow] Digitar o nome de um NOVO projeto")
        
    while True:
        choice_str = Prompt.ask(prompt_text).strip()
        if allow_new and choice_str.upper() == 'N':
            return Prompt.ask("Digite o nome do NOVO projeto").strip()
            
        if choice_str.isdigit():
            idx = int(choice_str)
            if 1 <= idx <= len(projects):
                return projects[idx - 1]
                
        if choice_str in projects:
            return choice_str
        elif allow_new and choice_str and not choice_str.isdigit():
            return choice_str
            
        console.print("[red]Escolha inválida. Digite o número correspondente, o nome do projeto, ou 'N' para criar um novo.[/red]")


def interactive_mode():
    console.print("[bold magenta]Bem vindo ao Dedup CLI Interativo[/bold magenta]")
    console.print("[dim]Esta ferramenta permite criar um banco de dados de mídia (index) e buscar duplicatas (compare).[/dim]\n")
    
    console.print("[bold cyan]--- Menu Principal ---[/bold cyan]")
    console.print(" [bold yellow][1][/bold yellow] Indexar HD Externo/Pasta (Construir Banco)")
    console.print(" [bold yellow][2][/bold yellow] Comparar Nova Pasta contra um Banco (Buscar Duplicatas)")
    console.print(" [bold yellow][3][/bold yellow] Buscar Duplicatas Internas no MESMO Banco (Qualidades Diferentes & Idênticas)")
    console.print(" [bold yellow][4][/bold yellow] Comparar DOIS Bancos Diferentes entre si (Cross-DB)")
    console.print(" [bold yellow][5][/bold yellow] Gerar Relatório HTML de um Banco (Dashboard)")
    console.print(" [bold yellow][6][/bold yellow] Abrir Central de Ação de Duplicatas (WebApp Interativo)")
    console.print(" [bold yellow][7][/bold yellow] Calcular Perceptual Hashes (pHash) em lote (Antecipar processamento)")
    console.print(" [bold yellow][8][/bold yellow] Sair")
    
    action = Prompt.ask("\nEscolha uma opção", choices=["1", "2", "3", "4", "5", "6", "7", "8"], default="1")
    
    if action == "8":
        sys.exit(0)
        
    if action in ["1", "2"]:
        console.print("\n[cyan]💡 DICA:[/cyan] Projetos funcionam como 'agrupadores'. Se você indexar múltiplas pastas ou HDs diferentes \nusando o [bold]mesmo nome de projeto[/bold], os dados serão somados no mesmo banco de dados SQLite.")
        project = ask_project("Escolha o projeto", allow_new=(action == "1"))
        
        if action == "1":
            directory = Prompt.ask("Digite o caminho da pasta/HD para INDEXAR (adicionar ao banco)")
            run_index(project, directory)
        elif action == "2":
            directory = Prompt.ask("Digite o caminho da pasta nova para COMPARAR (procurar duplicatas)")
            run_compare(project, directory)
    elif action == "3":
        project = ask_project("Escolha o projeto para buscar duplicatas internas", allow_new=False)
        run_internal(project)
    elif action == "4":
        project_a = ask_project("Escolha o [bold]PRIMEIRO[/bold] projeto (Base Segura)", allow_new=False)
        project_b = ask_project("Escolha o [bold]SEGUNDO[/bold] projeto (Alvo de Limpeza)", allow_new=False)
        deep = Confirm.ask("Deseja fazer a comparação PROFUNDA (detectar fotos em menor qualidade e tolerar fusos horários)?", default=True)
        run_compare_db(project_a, project_b, deep=deep)
    elif action == "5":
        project = ask_project("Escolha o projeto para gerar o relatório", allow_new=False)
        run_dashboard(project)
    elif action == "6":
        console.print("\n[bold cyan]--- Central de Ação de Duplicatas (WebApp) ---[/bold cyan]")
        projects = get_existing_projects()
        if not projects:
            console.print("[yellow]Nenhum banco indexado encontrado. Abrindo Central vazia no navegador...[/yellow]")
            run_dedup_webapp()
            return
            
        console.print("Como deseja iniciar o WebApp?")
        console.print(" [bold yellow][1][/bold yellow] Buscar Duplicatas Internas em 1 Banco (Qualidades Diferentes)")
        console.print(" [bold yellow][2][/bold yellow] Cruzar 2 Bancos Diferentes (Base Segura vs Alvo)")
        console.print(" [bold yellow][3][/bold yellow] Abrir navegador e selecionar livremente na tela")
        sub_choice = Prompt.ask("Escolha o modo", choices=["1", "2", "3"], default="1")
        
        if sub_choice == "1":
            p = ask_project("Escolha o banco para análise interna", allow_new=False)
            run_dedup_webapp(initial_internal=p)
        elif sub_choice == "2":
            if len(projects) < 2:
                console.print("[yellow]Aviso: É necessário ter pelo menos 2 bancos indexados para cruzar. Abrindo no modo livre...[/yellow]")
                run_dedup_webapp()
            else:
                safe_p = ask_project("Escolha o banco SEGURO (Preservar / Nunca apagar)", allow_new=False)
                cand_p = ask_project("Escolha o banco CANDIDATO (Alvo de limpeza)", allow_new=False)
                deep = Confirm.ask("Ativar comparação profunda (resoluções menores e fusos horários)?", default=True)
                run_dedup_webapp(initial_safe=safe_p, initial_cand=cand_p, initial_deep=deep)
        else:
            run_dedup_webapp()
    elif action == "7":
        console.print("\n[bold cyan]--- Calcular Perceptual Hashes (pHash) em Lote ---[/bold cyan]")
        console.print("[dim]Calcula previamente os hashes visuais de todas as fotos para acelerar as deduplicações futuras.[/dim]\n")
        projects = get_existing_projects()
        if not projects:
            console.print("[yellow]Nenhum banco indexado encontrado.[/yellow]")
            return
            
        console.print(" [bold yellow][1][/bold yellow] Todos os bancos de dados em lote")
        console.print(" [bold yellow][2][/bold yellow] Escolher um banco de dados específico")
        sub_choice = Prompt.ask("Escolha o modo", choices=["1", "2"], default="1")
        
        if sub_choice == "1":
            run_compute_phash(all_projects=True)
        else:
            proj = ask_project("Escolha o projeto para calcular pHash", allow_new=False)
            run_compute_phash(project_name=proj)

def main():
    parser = argparse.ArgumentParser(description="Ferramenta de indexação e deduplicação de mídias.")
    subparsers = parser.add_subparsers(dest="command")
    
    # Index command
    index_parser = subparsers.add_parser("index", help="Indexa um diretório em um banco de dados SQLite")
    index_parser.add_argument("--project", required=True, help="Nome do projeto (ex: hdd_antigo)")
    index_parser.add_argument("directory", help="Caminho do diretório a indexar")
    
    compare_parser = subparsers.add_parser("compare", help="Compara um diretório local com um banco de dados SQLite existente")
    compare_parser.add_argument("--project", required=True, help="Nome do projeto do banco (ex: hdd_antigo)")
    compare_parser.add_argument("directory", help="Caminho do diretório a comparar")

    internal_parser = subparsers.add_parser("internal", help="Busca duplicatas internas no mesmo banco de dados (fotos reduzidas, comprimidas, fusos horários e idênticas)")
    internal_parser.add_argument("--project", required=True, help="Nome do projeto do banco (ex: hdd_antigo)")

    compare_db_parser = subparsers.add_parser("compare-db", help="Compara dois bancos de dados SQLite entre si")
    compare_db_parser.add_argument("--proj-a", required=True, help="Nome do primeiro projeto")
    compare_db_parser.add_argument("--proj-b", required=True, help="Nome do segundo projeto")
    compare_db_parser.add_argument("--deep", action="store_true", help="Ativa comparação profunda (qualidades diferentes, fusos horários e pHash)")
    
    webapp_parser = subparsers.add_parser("webapp", help="Inicia a Central Interativa de Ação de Duplicatas (WebApp)")
    webapp_parser.add_argument("--report", help="Nome ou caminho do relatório JSON inicial")
    webapp_parser.add_argument("--safe", help="Nome do projeto SQLite seguro (preservar)")
    webapp_parser.add_argument("--cand", help="Nome do projeto SQLite candidato (alvo de limpeza)")
    webapp_parser.add_argument("--internal", help="Nome do projeto para carregar duplicatas internas no WebApp")
    webapp_parser.add_argument("--deep", action="store_true", help="Ativa comparação profunda no cruzamento de bancos")
    webapp_parser.add_argument("--port", type=int, default=8555, help="Porta HTTP do servidor local (padrão: 8555)")

    # compute-phash command
    phash_parser = subparsers.add_parser("compute-phash", help="Calcula perceptual hashes (pHash) em lote para fotos de um ou todos os bancos")
    phash_group = phash_parser.add_mutually_exclusive_group(required=True)
    phash_group.add_argument("--project", help="Nome do projeto específico (ex: gphotos-takeout)")
    phash_group.add_argument("--all", action="store_true", help="Executa para todos os bancos SQLite existentes em dbs/")
    phash_parser.add_argument("--workers", type=int, default=8, help="Número de threads simultâneas para cálculo (padrão: 8)")
    phash_parser.add_argument("--batch-size", type=int, default=500, help="Tamanho do lote para commit no SQLite (padrão: 500)")

    args = parser.parse_args()
    
    try:
        if args.command == "index":
            run_index(args.project, args.directory)
        elif args.command == "compare":
            run_compare(args.project, args.directory)
        elif args.command == "internal":
            run_internal(args.project)
        elif args.command == "compare-db":
            run_compare_db(args.proj_a, args.proj_b, deep=args.deep)
        elif args.command == "webapp":
            run_dedup_webapp(
                port=args.port,
                initial_report=args.report,
                initial_safe=args.safe,
                initial_cand=args.cand,
                initial_internal=args.internal,
                initial_deep=args.deep
            )
        elif args.command == "compute-phash":
            run_compute_phash(project_name=args.project, all_projects=args.all, workers=args.workers, batch_size=args.batch_size)
        elif not args.command:
            interactive_mode()
    except KeyboardInterrupt:
        console.print("\n[bold red]Execução abortada pelo usuário (Ctrl+C). O estado foi salvo de forma segura.[/bold red]")
        sys.exit(1)

if __name__ == "__main__":
    main()
