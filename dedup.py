import argparse
import sys
import os
import time
from typing import List, Dict, Any

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
from ui import VerticalProgress

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


def run_compare_db(project_a: str, project_b: str):
    console.print(f"[bold blue]Comparando Projeto '{project_a}' contra Projeto '{project_b}'[/bold blue]")
    
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
        
        with console.status("[cyan]Rodando query de Inner Join via SQLite...[/cyan]"):
            duplicates_found = db_a.compare_with_db(db_b_path)

    elapsed = time.time() - start_time
    console.print(f"\nCruzamento concluído super rápido em {elapsed:.2f} segundos!")
    console.print(f"Total de {len(duplicates_found)} duplicatas exatas encontradas!")
    
    if duplicates_found:
        report_name = f"{project_a}_vs_{project_b}"
        json_rep, html_rep = generate_reports(report_name, total_a, duplicates_found)
        console.print(f"\n[bold green]Relatórios de Cruzamento Gerados com Sucesso![/bold green]")
        console.print(f"JSON: [cyan]{json_rep}[/cyan]")
        console.print(f"HTML: [cyan]{html_rep}[/cyan]")


def interactive_mode():
    console.print("[bold magenta]Bem vindo ao Dedup CLI Interativo[/bold magenta]")
    console.print("[dim]Esta ferramenta permite criar um banco de dados de mídia (index) e buscar duplicatas (compare).[/dim]\n")
    
    console.print("[bold cyan]--- Menu Principal ---[/bold cyan]")
    console.print(" [bold yellow][1][/bold yellow] Indexar HD Externo/Pasta (Construir Banco)")
    console.print(" [bold yellow][2][/bold yellow] Comparar Nova Pasta contra um Banco (Buscar Duplicatas)")
    console.print(" [bold yellow][3][/bold yellow] Comparar DOIS Bancos Diferentes entre si")
    console.print(" [bold yellow][4][/bold yellow] Sair")
    
    action = Prompt.ask("\nEscolha uma opção", choices=["1", "2", "3", "4"], default="1")
    
    if action == "4":
        sys.exit(0)
        
    if action in ["1", "2"]:
        console.print("\n[cyan]💡 DICA:[/cyan] Projetos funcionam como 'agrupadores'. Se você indexar múltiplas pastas ou HDs diferentes \nusando o [bold]mesmo nome de projeto[/bold], os dados serão somados no mesmo banco de dados SQLite.")
        project = Prompt.ask("Digite o nome do projeto do banco (ex: meu_acervo_principal)")
        
        if action == "1":
            directory = Prompt.ask("Digite o caminho da pasta/HD para INDEXAR (adicionar ao banco)")
            run_index(project, directory)
        elif action == "2":
            directory = Prompt.ask("Digite o caminho da pasta nova para COMPARAR (procurar duplicatas)")
            run_compare(project, directory)
    elif action == "3":
        project_a = Prompt.ask("Digite o nome do [bold]PRIMEIRO[/bold] projeto (ex: hdd_antigo)")
        project_b = Prompt.ask("Digite o nome do [bold]SEGUNDO[/bold] projeto (ex: hdd_novo)")
        run_compare_db(project_a, project_b)

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

    compare_db_parser = subparsers.add_parser("compare-db", help="Compara dois bancos de dados SQLite entre si")
    compare_db_parser.add_argument("--proj-a", required=True, help="Nome do primeiro projeto")
    compare_db_parser.add_argument("--proj-b", required=True, help="Nome do segundo projeto")
    
    args = parser.parse_args()
    
    try:
        if args.command == "index":
            run_index(args.project, args.directory)
        elif args.command == "compare":
            run_compare(args.project, args.directory)
        elif args.command == "compare-db":
            run_compare_db(args.proj_a, args.proj_b)
        elif not args.command:
            interactive_mode()
    except KeyboardInterrupt:
        console.print("\n[bold red]Execução abortada pelo usuário (Ctrl+C). O estado foi salvo de forma segura.[/bold red]")
        sys.exit(1)

if __name__ == "__main__":
    main()
