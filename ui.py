import sys
import os
import time
from datetime import datetime, timedelta
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress_bar import ProgressBar
from rich.text import Text
from rich.live import Live
from rich import box

# Ensure UTF-8 on Windows for robust console output
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


def format_path_for_display(path: str, max_len: int = 60) -> str:
    """
    Trunca o caminho de forma inteligente para exibição no terminal
    sem quebrar linhas (ex: C:\\Origem\\...\\foto.jpg).
    """
    if not path:
        return "-"
    norm_path = os.path.normpath(path) if path else path
    if len(norm_path) <= max_len:
        return norm_path

    filename = os.path.basename(norm_path)
    if len(filename) >= max_len - 6:
        return filename[:max_len - 3] + "..."

    prefix_len = max_len - len(filename) - 6
    prefix = norm_path[:prefix_len]
    return f"{prefix}...\\{filename}"


class VerticalProgress:
    """
    Exibição vertical de progresso com Rich.
    Cada linha apresenta uma métrica/informação específica para máxima legibilidade:
    - Arquivo Atual (com truncamento inteligente anti-jitter)
    - Progresso (barra gráfica + porcentagem + contagem)
    - Velocidade (itens/s)
    - Tempo Decorrido (HH:MM:SS)
    - Horário de Início real (relógio local)
    - Previsão Término com ETA absoluto (relógio local e contagem regressiva)
    - Status Detalhado (métricas extras dinâmicas)
    """

    def __init__(
        self,
        title: str,
        total: int,
        console: Optional[Console] = None,
        transient: bool = False,
    ):
        self.title = title
        self.total = max(1, total)
        self.completed = 0
        self.console = console or Console()
        self.transient = transient
        self.current_file = ""
        self.extra_info = ""

        # Métricas de tempo
        self.start_dt = datetime.now()
        self.start_mono = time.monotonic()
        self._last_completed = 0
        self._last_time = self.start_mono
        self._smoothed_speed = 0.0

        self.live = Live(
            self.render(),
            console=self.console,
            refresh_per_second=8,
            transient=self.transient,
        )

    def __enter__(self):
        self.live.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Renderiza o estado final antes de fechar
        try:
            self.live.update(self.render())
        except Exception:
            pass
        self.live.stop()

    def update(
        self,
        advance: int = 0,
        current_file: Optional[str] = None,
        extra_info: Optional[str] = None,
    ):
        now = time.monotonic()
        if advance > 0:
            self.completed = min(self.total, self.completed + advance)

            # Cálculo de velocidade com suavização exponencial (EMA)
            dt = now - self._last_time
            if dt >= 0.2:
                items_delta = self.completed - self._last_completed
                instant_speed = items_delta / dt
                if self._smoothed_speed == 0.0:
                    self._smoothed_speed = instant_speed
                else:
                    self._smoothed_speed = 0.7 * self._smoothed_speed + 0.3 * instant_speed
                self._last_time = now
                self._last_completed = self.completed

        if current_file is not None:
            self.current_file = current_file
        if extra_info is not None:
            self.extra_info = extra_info

        self.live.update(self.render())

    def render(self) -> Panel:
        now_mono = time.monotonic()
        elapsed_sec = max(0.0, now_mono - self.start_mono)

        pct = (self.completed / self.total) * 100.0

        # Velocidade
        speed = self._smoothed_speed
        if speed <= 0 and elapsed_sec > 1.0 and self.completed > 0:
            speed = self.completed / elapsed_sec

        # Horário real de início
        start_display = self.start_dt.strftime("%H:%M:%S")

        # Horário absoluto do ETA
        remaining_items = max(0, self.total - self.completed)
        if self.completed >= self.total:
            eta_display = f"[bold green]{datetime.now().strftime('%H:%M:%S')}[/bold green] [dim](concluído)[/dim]"
        elif speed > 0 and self.completed > 0:
            eta_sec = remaining_items / speed
            eta_dt = datetime.now() + timedelta(seconds=eta_sec)
            rem_m, rem_s = divmod(int(eta_sec), 60)
            rem_h, rem_m = divmod(rem_m, 60)
            rem_days, rem_h = divmod(rem_h, 24)

            if rem_days > 0:
                rem_str = f"{rem_days}d {rem_h:02d}:{rem_m:02d}:{rem_s:02d}"
            else:
                rem_str = f"{rem_h:02d}:{rem_m:02d}:{rem_s:02d}"

            # Se a estimativa passar da meia-noite (outro dia)
            if eta_dt.date() > datetime.now().date():
                eta_time_str = eta_dt.strftime("%d/%m %H:%M:%S")
            else:
                eta_time_str = eta_dt.strftime("%H:%M:%S")

            eta_display = f"[bold green]{eta_time_str}[/bold green] [dim](restam ~{rem_str})[/dim]"
        else:
            eta_display = "[yellow]Calculando estimativa...[/yellow]"

        # Tempo decorrido
        el_m, el_s = divmod(int(elapsed_sec), 60)
        el_h, el_m = divmod(el_m, 60)
        elapsed_display = f"{el_h:02d}:{el_m:02d}:{el_s:02d}"

        # Velocidade
        speed_display = f"{speed:,.1f} arq/s".replace(",", ".") if speed > 0 else "-- arq/s"

        # Dimensões do console
        console_width = self.console.width or 80
        label_width = 24
        max_path_len = max(25, console_width - label_width - 10)
        display_file = format_path_for_display(self.current_file, max_len=max_path_len)

        # Barra visual de progresso
        bar_width = max(12, min(32, console_width - label_width - 36))
        bar = ProgressBar(total=self.total, completed=self.completed, width=bar_width)

        prog_table = Table.grid(padding=(0, 1))
        prog_table.add_column("bar")
        prog_table.add_column("pct")
        prog_table.add_column("count")
        prog_table.add_row(
            bar,
            Text(f"{pct:>5.1f}%", style="bold white"),
            Text(f"({self.completed:,} de {self.total:,} arquivos)".replace(",", "."), style="dim white"),
        )

        # Tabela vertical de linhas
        table = Table.grid(padding=(0, 2))
        table.add_column("label", style="bold cyan", width=label_width)
        table.add_column("value")

        table.add_row("Arquivo Atual:", Text(display_file, style="white", no_wrap=True))
        table.add_row("Progresso:", prog_table)
        table.add_row("Velocidade:", Text(speed_display, style="bold yellow"))
        table.add_row("Tempo Decorrido:", Text(elapsed_display, style="white"))
        table.add_row("Horário de Início:", Text(start_display, style="bold white"))
        table.add_row("Previsão Término (ETA):", eta_display)

        if self.extra_info:
            table.add_row("Status Detalhado:", self.extra_info)

        return Panel(
            table,
            title=f"[bold blue] {self.title} [/bold blue]",
            border_style="cyan",
            padding=(0, 1),
            box=box.ROUNDED,
        )
