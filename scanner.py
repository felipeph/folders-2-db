import os
import itertools
from typing import Iterator, List, Any
from rich.console import Console

console = Console()

def preflight_check(directory: str) -> bool:
    """Valida se o diretório existe e se temos permissão de leitura."""
    if not os.path.exists(directory):
        console.print(f"[bold red]Erro:[/bold red] O diretório '{directory}' não existe.")
        return False
    if not os.path.isdir(directory):
        console.print(f"[bold red]Erro:[/bold red] O caminho '{directory}' não é uma pasta.")
        return False
    if not os.access(directory, os.R_OK):
        console.print(f"[bold red]Erro:[/bold red] Sem permissão de leitura para '{directory}'.")
        return False
    return True

def scan_directory(directory: str, extensions: tuple = ('.jpg', '.jpeg', '.png', '.mp4', '.mov', '.avi', '.mkv', '.heic')) -> Iterator[str]:
    """Faz uma varredura recursiva rápida e retorna os caminhos absolutos dos arquivos válidos."""
    for root, _, files in os.walk(directory):
        for file in files:
            if file.lower().endswith(extensions):
                yield os.path.abspath(os.path.join(root, file))

def chunked_iterable(iterable: Iterator[Any], size: int) -> Iterator[List[Any]]:
    """Divide um iterável em pedaços menores (chunks) do tamanho especificado."""
    it = iter(iterable)
    while True:
        chunk = list(itertools.islice(it, size))
        if not chunk:
            break
        yield chunk
