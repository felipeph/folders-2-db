import json
import os
from datetime import datetime
from rich.console import Console
from rich.table import Table

console = Console()

class AuditLogger:
    def __init__(self, project_name: str, log_dir: str = "logs"):
        self.project_name = project_name
        self.log_dir = log_dir
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        self.log_file = os.path.join(log_dir, f"{project_name}_history.jsonl")

    def log_execution(self, command: str, total_input: int, processed: int, skipped: int, errors: int, elapsed_seconds: float):
        """Salva a execução no JSONL e faz o Mathematical Reconciliation."""
        record = {
            "timestamp": datetime.now().isoformat(),
            "command": command,
            "project": self.project_name,
            "total_input": total_input,
            "processed": processed,
            "skipped": skipped,
            "errors": errors,
            "elapsed_seconds": elapsed_seconds
        }
        
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
            
        self._print_reconciliation(record)

    def _print_reconciliation(self, record: dict):
        total = record["total_input"]
        sum_items = record["processed"] + record["skipped"] + record["errors"]
        
        table = Table(title="📊 Sumário de Execução e Reconciliação", show_header=True, header_style="bold magenta")
        table.add_column("Métrica")
        table.add_column("Valor", justify="right")
        
        table.add_row("Total de Arquivos Vistos", str(total))
        table.add_row("Processados (Novos/Atualizados)", str(record["processed"]))
        table.add_row("Ignorados (Já no DB)", str(record["skipped"]))
        table.add_row("Erros", str(record["errors"]))
        table.add_row("Tempo Decorrido (s)", f"{record['elapsed_seconds']:.1f}")
        
        console.print(table)
        
        if total != sum_items:
            console.print(f"[bold red]ATENÇÃO:[/bold red] Reconciliação falhou! {total} != {sum_items}")
        else:
            console.print("[bold green]✔ Reconciliação Matemática Perfeita![/bold green]")
