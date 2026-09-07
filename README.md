# folders-2-db 📸🗄️

> Ferramenta de linha de comando (CLI/TUI) resiliente e de alta performance para catalogação, extração completa de metadados EXIF e identificação de duplicatas entre acervos massivos de fotos e vídeos (como Google Takeout em HDs mecânicos lentos) e novos diretórios.

Construído sob os princípios arquiteturais do **Universal Python Pipeline** (idempotência, streaming hashing, processamento seguro de I/O em lotes, telemetria anti-jitter e reconciliação matemática de dados).

---

## 🚀 O Problema que Resolve

Ao lidar com grandes volumes de fotos e vídeos (ex.: ~2TB de exportações do Google Takeout em discos rígidos externos lentos), surgem desafios críticos:
1. **Gargalo de I/O:** Ler arquivos gigabyte a gigabyte para calcular hashes criptográficos completos (SHA-256) em HDs mecânicos consome dias e sobrecarrega o hardware.
2. **Falta de espaço:** Não há espaço em disco local (SSD) para transferir temporariamente os dados para análise.
3. **Perda de metadados e duplicações:** Identificar se novas pastas de backup já existem no acervo arquivado exige correlacionar tamanho de arquivo, assinaturas de conteúdo e metadados EXIF detalhados.

O **folders-2-db** resolve isso:
- Lê cada HD externo **apenas uma vez** e armazena metadados e assinaturas em bancos SQLite locais dedicados por projeto.
- Coleta o **EXIF completo** (câmera, lente, abertura, timestamps originais, etc.) chamando o `exiftool` em lotes (*External Tool Batching*).
- Gera uma assinatura rápida e precisa através de **Streaming Hashing** (primeiro e último 1MB do arquivo), sem sobrecarregar a memória RAM.
- Cruza dados entre pastas e bancos ou entre **dois bancos SQLite** via `ATTACH DATABASE` e `INNER JOIN` direto no motor SQL, com resultado instantâneo.
- Gera relatórios duplos: **JSON** estruturado para automação segura e **HTML** interativo para inspeção humana.

---

## 🛠️ Arquitetura e Módulos

```
folders-2-db/
├── dbs/              # Armazena os bancos de dados SQLite (*.sqlite) gerados
├── logs/             # Histórico de auditoria estruturado em formato JSONL
├── reports/          # Relatórios de duplicatas gerados (JSON e HTML interativo)
├── database.py       # Gerenciador de conexão SQLite, transações atômicas e queries
├── metadata.py       # Extração de estatísticas, hash parcial e lotes do exiftool
├── scanner.py        # Varredura recursiva de diretórios com preflight checks
├── logger.py         # Auditoria append-only e reconciliação matemática (DoD)
├── reporter.py       # Gerador dos relatórios JSON e dashboard HTML interativo
├── dedup.py          # Ponto de entrada (Menu interativo Rich TUI + subcomandos CLI)
├── requirements.txt  # Dependências Python
└── .gitignore        # Proteção rigorosa contra vazamento de dados pessoais
```

---

## 🔒 Privacidade e Segurança dos Dados

Este repositório foi configurado para **garantir que nenhum dado pessoal, caminho de diretório, coordenada GPS ou metadado de foto seja enviado para o GitHub**:
- A pasta `dbs/` e todos os arquivos `*.sqlite*` estão no `.gitignore`.
- A pasta `logs/` e os históricos `*.jsonl` estão no `.gitignore`.
- A pasta `reports/` e os arquivos `*.html` e `*.json` gerados estão no `.gitignore`.
- O código-fonte contém apenas a lógica dos algoritmos.

---

## 📋 Pré-requisitos

1. **Python 3.10+**
2. **ExifTool** instalado e configurado no `PATH` do sistema:
   - *Windows:* Baixe o executável no site oficial do [ExifTool](https://exiftool.org/) e renomeie para `exiftool.exe` adicionando-o ao PATH, ou via `winget install PhilHarvey.ExifTool` / `choco install exiftool`.

---

## 📦 Instalação

Clone o repositório e instale as dependências:

```bash
git clone https://github.com/felipeph/folders-2-db.git
cd folders-2-db
pip install -r requirements.txt
```

---

## 💻 Como Usar

### 1. Modo Interativo (TUI Amigável com Rich)
Basta rodar o comando principal sem argumentos. O assistente numerado guiará todas as ações:

```bash
python dedup.py
```

Menu exibido:
```text
--- Menu Principal ---
 [1] Indexar HD Externo/Pasta (Construir Banco)
 [2] Comparar Nova Pasta contra um Banco (Buscar Duplicatas)
 [3] Comparar DOIS Bancos Diferentes entre si
 [4] Sair
```

---

### 2. Modo Linha de Comando (CLI / Automação)

#### A. Indexar uma pasta ou HD externo em um projeto
> 💡 *Dica:* Você pode rodar o comando `index` várias vezes apontando para pastas ou HDs diferentes usando o **mesmo nome de projeto**. Os dados serão incrementalmente adicionados ao mesmo banco SQLite (`dbs/<projeto>.sqlite`).

```bash
python dedup.py index --project hdd_takeout "D:\MeuGoogleTakeout"
```
Se interrompido via `Ctrl+C`, o script salva o progresso e, na próxima execução, ignora instantaneamente os arquivos já catalogados (*Resume-by-Design*).

#### B. Comparar uma pasta nova contra um banco existente
Varre a pasta de destino, calcula as assinaturas e compara com o banco de dados sem alterar nenhum arquivo:

```bash
python dedup.py compare --project hdd_takeout "C:\MinhasFotosNovas"
```

#### C. Comparar dois bancos de dados SQLite entre si
Cruza o inventário de dois projetos inteiros diretamente em nível de SQL (alta performance com baixo consumo de memória):

```bash
python dedup.py compare-db --proj-a hdd_takeout --proj-b backup_antigo
```

---

## 📊 Relatórios de Duplicatas

Toda operação de comparação gera dois arquivos em `reports/`:

1. **`reports/<projeto>_report_<data_hora>.json`**:
   Arquivo estruturado contendo a lista completa de arquivos analisados e duplicatas (com caminhos de origem e correspondentes no banco). Ideal para ser consumido por scripts automatizados de exclusão ou movimentação segura.
   
2. **`reports/<projeto>_report_<data_hora>.html`**:
   Dashboard visual independente (funciona offline) contendo:
   - Cards com total de arquivos analisados, duplicatas e estimativa de espaço desperdiçado.
   - Campo de busca e filtro em tempo real por nome ou caminho.
   - Tabela comparativa com ordenação por colunas.

---

## 📝 Licença

Distribuído sob a licença MIT. Consulte `LICENSE` para mais detalhes.
