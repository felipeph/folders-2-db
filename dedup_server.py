import os
import sys
import json
import time
import hashlib
import sqlite3
import subprocess
from typing import List, Dict, Any, Optional
from datetime import datetime
from pathlib import Path

# Garante saída UTF-8 no terminal Windows
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, FileResponse, Response
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.middleware.cors import CORSMiddleware
from PIL import Image, ImageOps
import cv2
import send2trash

from database import Database
from logger import AuditLogger

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DBS_DIR = os.path.join(BASE_DIR, "dbs")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
CACHE_DIR = os.path.join(BASE_DIR, ".cache_thumbs")
WEB_DIR = os.path.join(BASE_DIR, "web")

os.makedirs(DBS_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(WEB_DIR, exist_ok=True)

# Estado global da sessão atual em memória
CURRENT_SESSION = {
    "title": "Nenhuma sessão carregada",
    "safe_project": "",
    "cand_project": "",
    "safe_db_path": "",
    "cand_db_path": "",
    "source_type": "", # "report" ou "live_db"
    "report_file": "",
    "duplicates": [], # Lista de dicionários normalizados
    "total_wasted_bytes": 0,
    "loaded_at": None
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff", ".heic"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".wmv", ".flv", ".3gp"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a"}

def format_bytes(num_bytes: int) -> str:
    """Formata bytes em unidades legíveis (B, KB, MB, GB, TB)."""
    if num_bytes is None or num_bytes < 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num_bytes < 1024.0:
            return f"{num_bytes:.2f} {unit}" if unit in ["MB", "GB", "TB"] else f"{num_bytes:.0f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f} PB"

def get_media_type(filepath: str) -> str:
    ext = os.path.splitext(filepath)[1].lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    return "other"

def get_thumb_cache_path(filepath: str, size: int = 400) -> str:
    stat = None
    try:
        stat = os.stat(filepath)
        mtime = stat.st_mtime
    except Exception:
        mtime = 0
    key = f"{filepath}_{mtime}_{size}".encode("utf-8")
    hash_str = hashlib.md5(key).hexdigest()
    return os.path.join(CACHE_DIR, f"{hash_str}.jpg")

def generate_thumbnail(filepath: str, size: int = 400) -> Optional[str]:
    """Gera thumbnail otimizado para imagem ou extrai frame de vídeo."""
    if not os.path.exists(filepath):
        return None
        
    cache_path = get_thumb_cache_path(filepath, size)
    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
        return cache_path

    m_type = get_media_type(filepath)
    try:
        if m_type == "image":
            with Image.open(filepath) as img:
                try:
                    img = ImageOps.exif_transpose(img)
                except Exception:
                    pass
                img.thumbnail((size, size), Image.Resampling.LANCZOS)
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                img.save(cache_path, "JPEG", quality=80, optimize=True)
            return cache_path
            
        elif m_type == "video":
            cap = cv2.VideoCapture(filepath)
            if not cap.isOpened():
                return None
            fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
            cap.set(cv2.CAP_PROP_POS_FRAMES, min(int(fps), int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 10) - 1))
            ret, frame = cap.read()
            if not ret:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ret, frame = cap.read()
            cap.release()
            
            if ret and frame is not None:
                h, w = frame.shape[:2]
                scale = min(size / w, size / h)
                new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
                resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
                cv2.imwrite(cache_path, resized, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                return cache_path
    except Exception as e:
        print(f"Erro ao gerar thumbnail para {filepath}: {e}")
        return None
    return None

# ==========================================
# Handlers de Rotas API
# ==========================================

async def api_list_available(request: Request):
    """Lista todos os bancos SQLite e relatórios JSON disponíveis."""
    dbs = []
    if os.path.exists(DBS_DIR):
        for f in os.listdir(DBS_DIR):
            if f.endswith(".sqlite"):
                full_p = os.path.join(DBS_DIR, f)
                dbs.append({
                    "name": f[:-7],
                    "filename": f,
                    "size_bytes": os.path.getsize(full_p),
                    "size_human": format_bytes(os.path.getsize(full_p))
                })
    dbs.sort(key=lambda x: x["name"])

    reports = []
    if os.path.exists(REPORTS_DIR):
        for f in os.listdir(REPORTS_DIR):
            if f.endswith(".json"):
                full_p = os.path.join(REPORTS_DIR, f)
                try:
                    with open(full_p, "r", encoding="utf-8") as rf:
                        data = json.load(rf)
                    reports.append({
                        "filename": f,
                        "project": data.get("project", f),
                        "generated_at": data.get("generated_at", ""),
                        "duplicates_count": len(data.get("duplicates", [])),
                        "total_analyzed": data.get("total_analyzed", 0),
                        "file_size": os.path.getsize(full_p)
                    })
                except Exception:
                    pass
    reports.sort(key=lambda x: x.get("generated_at", ""), reverse=True)

    return JSONResponse({
        "databases": dbs,
        "reports": reports,
        "current_session": {
            "title": CURRENT_SESSION["title"],
            "safe_project": CURRENT_SESSION["safe_project"],
            "cand_project": CURRENT_SESSION["cand_project"],
            "duplicates_count": len(CURRENT_SESSION["duplicates"]),
            "total_wasted_human": format_bytes(CURRENT_SESSION["total_wasted_bytes"])
        }
    })

async def api_init_session(request: Request):
    """
    Inicializa a sessão de duplicatas:
    - Ou carregando um relatório JSON existente com definição de quem é o lado seguro.
    - Ou executando o cruzamento dinâmico em tempo real entre dois bancos SQLite.
    """
    body = await request.json()
    mode = body.get("mode", "report")
    
    if mode == "report":
        report_file = body.get("report_file")
        full_report_path = os.path.join(REPORTS_DIR, report_file) if not os.path.isabs(report_file) else report_file
        if not os.path.exists(full_report_path):
            return JSONResponse({"error": f"Relatório '{report_file}' não encontrado."}, status_code=404)
            
        with open(full_report_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        raw_duplicates = data.get("duplicates", [])
        project_name = data.get("project", "Projeto")
        
        swap_sides = body.get("swap_sides", False)
        safe_key = "scanned_file" if swap_sides else "db_file"
        cand_key = "db_file" if swap_sides else "scanned_file"
        
        safe_proj_name = body.get("safe_project") or ("Origem B (Pasta/Nova)" if swap_sides else "Origem A (Banco/HD)")
        cand_proj_name = body.get("cand_project") or ("Origem A (Banco/HD)" if swap_sides else "Origem B (Pasta/Nova)")

        duplicates = []
        total_wasted = 0
        for i, item in enumerate(raw_duplicates):
            size = item.get("size_bytes", 0)
            total_wasted += size
            safe_p = item.get(safe_key, "")
            cand_p = item.get(cand_key, "")
            duplicates.append({
                "id": i + 1,
                "safe_path": safe_p,
                "safe_name": os.path.basename(safe_p),
                "safe_exists": os.path.exists(safe_p),
                "cand_path": cand_p,
                "cand_name": os.path.basename(cand_p),
                "cand_exists": os.path.exists(cand_p),
                "size_bytes": size,
                "size_human": format_bytes(size),
                "media_type": get_media_type(safe_p or cand_p),
                "deleted": False
            })

        CURRENT_SESSION.update({
            "title": f"Relatório: {project_name}",
            "safe_project": safe_proj_name,
            "cand_project": cand_proj_name,
            "safe_db_path": os.path.join(DBS_DIR, f"{safe_proj_name}.sqlite"),
            "cand_db_path": os.path.join(DBS_DIR, f"{cand_proj_name}.sqlite"),
            "source_type": "report",
            "report_file": report_file,
            "duplicates": duplicates,
            "total_wasted_bytes": total_wasted,
            "loaded_at": datetime.now().isoformat()
        })

    elif mode == "live_db":
        safe_proj = body.get("safe_project")
        cand_proj = body.get("cand_project")
        
        safe_db_path = os.path.join(DBS_DIR, f"{safe_proj}.sqlite")
        cand_db_path = os.path.join(DBS_DIR, f"{cand_proj}.sqlite")
        
        if not os.path.exists(safe_db_path) or not os.path.exists(cand_db_path):
            return JSONResponse({"error": "Um ou ambos os bancos SQLite não foram encontrados."}, status_code=404)
            
        with Database(safe_db_path) as db_safe:
            raw_dups = db_safe.compare_with_db(cand_db_path)
            
        duplicates = []
        total_wasted = 0
        for i, item in enumerate(raw_dups):
            size = item.get("size_bytes", 0)
            total_wasted += size
            safe_p = item.get("scanned_file", "")
            cand_p = item.get("db_file", "")
            duplicates.append({
                "id": i + 1,
                "safe_path": safe_p,
                "safe_name": os.path.basename(safe_p),
                "safe_exists": os.path.exists(safe_p),
                "cand_path": cand_p,
                "cand_name": os.path.basename(cand_p),
                "cand_exists": os.path.exists(cand_p),
                "size_bytes": size,
                "size_human": format_bytes(size),
                "media_type": get_media_type(safe_p or cand_p),
                "deleted": False
            })

        CURRENT_SESSION.update({
            "title": f"Cruzamento Direto: {safe_proj} vs {cand_proj}",
            "safe_project": safe_proj,
            "cand_project": cand_proj,
            "safe_db_path": safe_db_path,
            "cand_db_path": cand_db_path,
            "source_type": "live_db",
            "report_file": "",
            "duplicates": duplicates,
            "total_wasted_bytes": total_wasted,
            "loaded_at": datetime.now().isoformat()
        })

    return JSONResponse({
        "success": True,
        "session": {
            "title": CURRENT_SESSION["title"],
            "safe_project": CURRENT_SESSION["safe_project"],
            "cand_project": CURRENT_SESSION["cand_project"],
            "duplicates_count": len(CURRENT_SESSION["duplicates"]),
            "total_wasted_bytes": CURRENT_SESSION["total_wasted_bytes"],
            "total_wasted_human": format_bytes(CURRENT_SESSION["total_wasted_bytes"])
        }
    })

async def api_swap_sides(request: Request):
    """Inverte imediatamente o Lado Seguro e o Lado Candidato na sessão atual."""
    if not CURRENT_SESSION["duplicates"]:
        return JSONResponse({"error": "Nenhuma sessão ativa."}, status_code=400)
        
    CURRENT_SESSION["safe_project"], CURRENT_SESSION["cand_project"] = (
        CURRENT_SESSION["cand_project"],
        CURRENT_SESSION["safe_project"]
    )
    CURRENT_SESSION["safe_db_path"], CURRENT_SESSION["cand_db_path"] = (
        CURRENT_SESSION["cand_db_path"],
        CURRENT_SESSION["safe_db_path"]
    )
    
    for item in CURRENT_SESSION["duplicates"]:
        item["safe_path"], item["cand_path"] = item["cand_path"], item["safe_path"]
        item["safe_name"], item["cand_name"] = item["cand_name"], item["safe_name"]
        item["safe_exists"], item["cand_exists"] = item["cand_exists"], item["safe_exists"]
        
    return JSONResponse({
        "success": True,
        "safe_project": CURRENT_SESSION["safe_project"],
        "cand_project": CURRENT_SESSION["cand_project"]
    })

async def api_get_duplicates(request: Request):
    """Retorna itens de duplicatas com ordenação, filtros e paginação."""
    params = request.query_params
    page = max(1, int(params.get("page", 1)))
    limit = max(1, min(100, int(params.get("limit", 24))))
    sort_by = params.get("sort", "size_desc")
    media_filter = params.get("type", "all")
    search = params.get("search", "").strip().lower()
    min_size_mb = float(params.get("min_size_mb", 0))

    items = [d for d in CURRENT_SESSION["duplicates"] if not d["deleted"]]

    if media_filter != "all":
        items = [d for d in items if d["media_type"] == media_filter]

    if min_size_mb > 0:
        min_bytes = min_size_mb * 1024 * 1024
        items = [d for d in items if d["size_bytes"] >= min_bytes]

    if search:
        items = [
            d for d in items 
            if search in d["safe_path"].lower() or search in d["cand_path"].lower()
        ]

    # Ordenação padrão: Maior Tamanho Primeiro
    if sort_by == "size_desc":
        items.sort(key=lambda x: x["size_bytes"], reverse=True)
    elif sort_by == "size_asc":
        items.sort(key=lambda x: x["size_bytes"], reverse=False)
    elif sort_by == "name":
        items.sort(key=lambda x: x["cand_name"].lower())
    elif sort_by == "path":
        items.sort(key=lambda x: x["cand_path"].lower())

    total_filtered = len(items)
    total_pages = max(1, (total_filtered + limit - 1) // limit)
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    page_items = items[start_idx:end_idx]

    return JSONResponse({
        "page": page,
        "limit": limit,
        "total_filtered": total_filtered,
        "total_pages": total_pages,
        "total_items": len(CURRENT_SESSION["duplicates"]),
        "total_wasted_human": format_bytes(CURRENT_SESSION["total_wasted_bytes"]),
        "safe_project": CURRENT_SESSION["safe_project"],
        "cand_project": CURRENT_SESSION["cand_project"],
        "items": page_items
    })

async def api_thumbnail(request: Request):
    """Endpoint de thumbnail com cache em disco e alta performance."""
    filepath = request.query_params.get("path")
    size = int(request.query_params.get("size", 400))
    
    if not filepath or not os.path.exists(filepath):
        svg_content = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {size} {size}">
            <rect width="100%" height="100%" fill="#f1f5f9"/>
            <text x="50%" y="45%" text-anchor="middle" fill="#94a3b8" font-family="sans-serif" font-size="14" font-weight="bold">Arquivo Inacessível</text>
            <text x="50%" y="58%" text-anchor="middle" fill="#cbd5e1" font-family="sans-serif" font-size="11">Drive ou arquivo desconectado</text>
        </svg>"""
        return Response(content=svg_content, media_type="image/svg+xml")

    thumb_path = generate_thumbnail(filepath, size)
    if thumb_path and os.path.exists(thumb_path):
        return FileResponse(thumb_path, media_type="image/jpeg")

    m_type = get_media_type(filepath)
    label = "Vídeo" if m_type == "video" else "Imagem" if m_type == "image" else "Arquivo"
    svg_fallback = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {size} {size}">
        <rect width="100%" height="100%" fill="#e2e8f0"/>
        <text x="50%" y="50%" text-anchor="middle" fill="#64748b" font-family="sans-serif" font-size="16" font-weight="bold">Sem Prévia ({label})</text>
    </svg>"""
    return Response(content=svg_fallback, media_type="image/svg+xml")

async def api_media(request: Request):
    """Serve arquivo de mídia (vídeo ou imagem completa) com suporte a HTTP Range."""
    filepath = request.query_params.get("path")
    if not filepath or not os.path.exists(filepath):
        return Response("Arquivo não encontrado", status_code=404)
    return FileResponse(filepath)

async def api_reveal_explorer(request: Request):
    """Abre o Windows Explorer selecionando e destacando o arquivo."""
    try:
        body = await request.json()
        filepath = body.get("path")
        if not filepath:
            return JSONResponse({"error": "Caminho não fornecido"}, status_code=400)
            
        norm_path = os.path.normpath(filepath)
        if os.path.exists(norm_path):
            # Formato canônico do Windows Explorer para selecionar o arquivo
            subprocess.Popen(f'explorer /select,"{norm_path}"')
            return JSONResponse({"success": True})
        else:
            # Se o arquivo não existir diretamente mas a pasta existir, abre a pasta
            parent_dir = os.path.dirname(norm_path)
            if os.path.exists(parent_dir):
                subprocess.Popen(f'explorer "{parent_dir}"')
                return JSONResponse({"success": True, "opened_parent": True})
            return JSONResponse({"error": f"Arquivo ou pasta não encontrado: {norm_path}"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

async def api_open_file(request: Request):
    """Abre o arquivo no programa padrão do Windows (VLC, Media Player, Fotos, etc)."""
    try:
        body = await request.json()
        filepath = body.get("path")
        if not filepath:
            return JSONResponse({"error": "Caminho não fornecido"}, status_code=400)
            
        norm_path = os.path.normpath(filepath)
        if not os.path.exists(norm_path):
            return JSONResponse({"error": f"Arquivo não encontrado no disco: {norm_path}"}, status_code=404)
            
        os.startfile(norm_path)
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

async def api_delete_batch(request: Request):
    """
    Exclui com segurança a lista de arquivos selecionados:
    1. Move para a Lixeira do Windows (send2trash).
    2. Remove o registro do arquivo no banco de dados SQLite correspondente.
    3. Registra a ação no log de auditoria.
    4. Atualiza o estado da sessão em memória.
    """
    body = await request.json()
    file_paths = body.get("paths", [])
    if not file_paths:
        return JSONResponse({"error": "Nenhum arquivo informado para exclusão."}, status_code=400)

    logger = AuditLogger("dedup_webapp_deletions")
    deleted_paths = []
    errors = []
    freed_bytes = 0

    cand_db = None
    if CURRENT_SESSION["cand_db_path"] and os.path.exists(CURRENT_SESSION["cand_db_path"]):
        try:
            cand_db = Database(CURRENT_SESSION["cand_db_path"])
        except Exception:
            cand_db = None

    for p in file_paths:
        norm_p = os.path.normpath(p)
        try:
            f_size = os.path.getsize(norm_p) if os.path.exists(norm_p) else 0
            if os.path.exists(norm_p):
                send2trash.send2trash(norm_p)
                freed_bytes += f_size
                
            if cand_db:
                try:
                    cand_db.delete_file(norm_p)
                except Exception:
                    pass

            deleted_paths.append(norm_p)

            for d in CURRENT_SESSION["duplicates"]:
                if os.path.normpath(d["cand_path"]) == norm_p:
                    d["deleted"] = True

        except Exception as e:
            errors.append({"path": norm_p, "error": str(e)})

    if cand_db:
        cand_db.close()

    logger.log_execution(
        action="webapp_batch_delete",
        total_files=len(file_paths),
        processed=len(deleted_paths),
        skipped=0,
        errors=len(errors),
        elapsed_sec=0.0
    )

    CURRENT_SESSION["total_wasted_bytes"] = max(0, CURRENT_SESSION["total_wasted_bytes"] - freed_bytes)

    return JSONResponse({
        "success": True,
        "deleted_count": len(deleted_paths),
        "freed_bytes": freed_bytes,
        "freed_human": format_bytes(freed_bytes),
        "errors": errors
    })

async def serve_index(request: Request):
    index_file = os.path.join(WEB_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return Response("Interface Web não encontrada. Verifique se web/index.html foi criado.", status_code=404)

routes = [
    Route("/", endpoint=serve_index),
    Route("/index.html", endpoint=serve_index),
    Route("/api/available", endpoint=api_list_available, methods=["GET"]),
    Route("/api/session/init", endpoint=api_init_session, methods=["POST"]),
    Route("/api/session/swap", endpoint=api_swap_sides, methods=["POST"]),
    Route("/api/duplicates", endpoint=api_get_duplicates, methods=["GET"]),
    Route("/api/thumbnail", endpoint=api_thumbnail, methods=["GET"]),
    Route("/api/media", endpoint=api_media, methods=["GET"]),
    Route("/api/reveal", endpoint=api_reveal_explorer, methods=["POST"]),
    Route("/api/open", endpoint=api_open_file, methods=["POST"]),
    Route("/api/delete", endpoint=api_delete_batch, methods=["POST"]),
]

app = Starlette(debug=True, routes=routes)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def start_server(port: int = 8555, auto_open: bool = True, initial_report: Optional[str] = None):
    """Inicia o servidor Uvicorn e opcionalmente abre o navegador."""
    import webbrowser

    if initial_report:
        report_path = os.path.join(REPORTS_DIR, initial_report) if not os.path.isabs(initial_report) else initial_report
        if os.path.exists(report_path):
            with open(report_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw_dups = data.get("duplicates", [])
            dups = []
            total_w = 0
            for i, item in enumerate(raw_dups):
                sz = item.get("size_bytes", 0)
                total_w += sz
                sp = item.get("db_file", "")
                cp = item.get("scanned_file", "")
                dups.append({
                    "id": i + 1,
                    "safe_path": sp,
                    "safe_name": os.path.basename(sp),
                    "safe_exists": os.path.exists(sp),
                    "cand_path": cp,
                    "cand_name": os.path.basename(cp),
                    "cand_exists": os.path.exists(cp),
                    "size_bytes": sz,
                    "size_human": format_bytes(sz),
                    "media_type": get_media_type(sp or cp),
                    "deleted": False
                })
            CURRENT_SESSION.update({
                "title": f"Relatório: {data.get('project', 'Projeto')}",
                "safe_project": "Origem Preservada (Base)",
                "cand_project": "Origem Candidata (Varredura)",
                "source_type": "report",
                "report_file": initial_report,
                "duplicates": dups,
                "total_wasted_bytes": total_w,
                "loaded_at": datetime.now().isoformat()
            })

    url = f"http://127.0.0.1:{port}"
    print(f"\n=======================================================")
    print(f"🚀 Central de Ação de Duplicatas ativa em: {url}")
    print(f"=======================================================\n")
    
    if auto_open:
        webbrowser.open(url)
        
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")

if __name__ == "__main__":
    report_arg = sys.argv[1] if len(sys.argv) > 1 else None
    start_server(port=8555, auto_open=True, initial_report=report_arg)
