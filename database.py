import sqlite3
import json
import os
import re
import itertools
import contextlib
import time
import concurrent.futures
from typing import Dict, Any, List, Optional, Tuple, Callable
from metadata import extract_core_exif, compute_phash, hamming_distance, is_same_capture_time, are_same_photo_or_variant

IMAGE_EXTENSIONS_TUPLE = ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tiff', '.tif', '.heic', '.heif')
IMAGE_EXTENSIONS_SQL = " OR ".join([f"filename LIKE '%{ext}'" for ext in IMAGE_EXTENSIONS_TUPLE])

def rank_file_quality(file_dict: Dict[str, Any]) -> Tuple:
    """
    Critério de Qualidade Técnica (eleito no Grill-Me):
    1º Maior Resolução (Megapixels / Largura × Altura)
    2º Maior Tamanho em Disco (MB)
    3º Presença de EXIF original
    4º Preferência por nome original (sem 'copia', 'copy', '(1)', etc.)
    """
    w = file_dict.get("img_width") or 0
    h = file_dict.get("img_height") or 0
    mp = (w * h) / 1_000_000.0
    size = file_dict.get("size_bytes") or 0
    has_exif = 1 if file_dict.get("exif_date") else 0
    
    path_lower = file_dict.get("filepath", "").lower()
    is_copy_suffix = 1 if any(k in path_lower for k in ["copia", "copy", "(1)", "(2)", "- edit", "_edit", "whatsapp"]) else 0
    
    return (mp, size, has_exif, -is_copy_suffix)

class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()
        self.batch_count = 0
        self.BATCH_LIMIT = 500
        self.conn = sqlite3.connect(self.db_path)
        # Habilita o modo WAL para melhor concorrência e performance
        self.conn.execute('PRAGMA journal_mode=WAL')

    def _init_db(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS files (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        filepath TEXT UNIQUE,
                        filename TEXT,
                        size_bytes INTEGER,
                        mtime REAL,
                        partial_hash TEXT,
                        exif_data TEXT,
                        img_width INTEGER,
                        img_height INTEGER,
                        exif_date TEXT,
                        camera_model TEXT,
                        phash TEXT,
                        scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        deleted_at TIMESTAMP,
                        deleted_reason TEXT
                    )
                ''')
                # Índices para acelerar a busca de duplicatas
                conn.execute('CREATE INDEX IF NOT EXISTS idx_size_hash ON files(size_bytes, partial_hash)')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_filepath ON files(filepath)')
                self._ensure_columns(conn)

    def _ensure_columns(self, conn: sqlite3.Connection):
        """Garante de forma não destrutiva que colunas de qualidade e índices existam em bancos já criados."""
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(files)")
        existing_cols = {row[1] for row in cursor.fetchall()}
        
        new_cols = [
            ("img_width", "INTEGER"),
            ("img_height", "INTEGER"),
            ("exif_date", "TEXT"),
            ("camera_model", "TEXT"),
            ("phash", "TEXT"),
            ("deleted_at", "TIMESTAMP"),
            ("deleted_reason", "TEXT")
        ]
        for col_name, col_type in new_cols:
            if col_name not in existing_cols:
                cursor.execute(f"ALTER TABLE files ADD COLUMN {col_name} {col_type}")
                
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_exif_cluster ON files(exif_date, camera_model)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_phash ON files(phash)')

    def insert_or_update_file(self, filepath: str, filename: str, size_bytes: int, mtime: float, partial_hash: str, exif_data: Dict[str, Any]):
        """
        Insere ou atualiza um registro de arquivo no banco de dados.
        """
        exif_json = json.dumps(exif_data)
        exif_date, camera_model, img_width, img_height = extract_core_exif(exif_data)
        
        try:
            self.conn.execute('''
                INSERT INTO files (filepath, filename, size_bytes, mtime, partial_hash, exif_data, img_width, img_height, exif_date, camera_model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(filepath) DO UPDATE SET
                    size_bytes=excluded.size_bytes,
                    mtime=excluded.mtime,
                    partial_hash=excluded.partial_hash,
                    exif_data=excluded.exif_data,
                    img_width=excluded.img_width,
                    img_height=excluded.img_height,
                    exif_date=excluded.exif_date,
                    camera_model=excluded.camera_model,
                    scanned_at=CURRENT_TIMESTAMP
            ''', (filepath, filename, size_bytes, mtime, partial_hash, exif_json, img_width, img_height, exif_date, camera_model))
            
            self.batch_count += 1
            if self.batch_count >= self.BATCH_LIMIT:
                self.commit()
                
        except sqlite3.Error:
            pass

    def update_file_phash(self, filepath: str, phash: str):
        """Salva o perceptual hash calculado sob demanda no banco para acelerar futuras consultas."""
        try:
            self.conn.execute('UPDATE files SET phash = ? WHERE filepath = ?', (phash, filepath))
            self.batch_count += 1
            if self.batch_count >= self.BATCH_LIMIT:
                self.commit()
        except sqlite3.Error:
            pass

    def get_pending_phash_count(self) -> int:
        """Retorna o total de arquivos de imagem elegíveis com pHash ainda não calculado."""
        cursor = self.conn.cursor()
        cursor.execute(f'''
            SELECT COUNT(*) FROM files 
            WHERE (phash IS NULL OR phash = '')
            AND ({IMAGE_EXTENSIONS_SQL})
            AND deleted_at IS NULL
        ''')
        return cursor.fetchone()[0]

    def populate_phashes(
        self,
        max_workers: int = 8,
        batch_size: int = 500,
        progress_callback: Optional[Callable[[int, str, int, int], None]] = None,
        stop_event: Optional[Any] = None
    ) -> Dict[str, int]:
        """
        Calcula e salva o pHash para todas as imagens elegíveis pendentes no banco.
        Opera em lotes com ThreadPoolExecutor e salva periodicamente (Resume-by-Design).
        """
        total_pending = self.get_pending_phash_count()
        if total_pending == 0:
            return {"processed": 0, "success": 0, "errors": 0, "remaining": 0}

        # Pre-flight check: verificar se os primeiros caminhos são acessíveis no disco
        cursor = self.conn.cursor()
        cursor.execute(f'''
            SELECT filepath FROM files 
            WHERE (phash IS NULL OR phash = '')
            AND ({IMAGE_EXTENSIONS_SQL})
            LIMIT 20
        ''')
        sample_paths = [r[0] for r in cursor.fetchall()]
        if sample_paths:
            accessible_count = sum(1 for p in sample_paths if os.path.exists(p))
            if accessible_count == 0:
                first_path = sample_paths[0]
                drive_or_root = os.path.splitdrive(first_path)[0] or os.path.dirname(first_path)
                if drive_or_root and not os.path.exists(drive_or_root):
                    raise FileNotFoundError(
                        f"O drive ou diretório '{drive_or_root}' não está montado ou acessível. "
                        f"Conecte o dispositivo antes de calcular os hashes para evitar erros em massa."
                    )

        processed_count = 0
        success_count = 0
        error_count = 0

        def _hash_worker(item: Tuple[int, str]) -> Tuple[int, str, str]:
            row_id, filepath = item
            try:
                h = compute_phash(filepath)
                if h and len(h) == 16:
                    return row_id, h, filepath
                else:
                    return row_id, "error", filepath
            except Exception:
                return row_id, "error", filepath

        updates = []
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        try:
            while True:
                if stop_event and stop_event.is_set():
                    break

                cursor.execute(f'''
                    SELECT id, filepath FROM files 
                    WHERE (phash IS NULL OR phash = '')
                    AND ({IMAGE_EXTENSIONS_SQL})
                    AND deleted_at IS NULL
                    LIMIT ?
                ''', (batch_size,))
                batch = cursor.fetchall()
                if not batch:
                    break

                updates = []
                for row_id, h, path in executor.map(_hash_worker, batch):
                    updates.append((h, row_id))
                    processed_count += 1
                    if h != "error":
                        success_count += 1
                    else:
                        error_count += 1

                    if progress_callback:
                        progress_callback(1, path, success_count, error_count)

                cursor.executemany('UPDATE files SET phash = ? WHERE id = ?', updates)
                self.conn.commit()
                updates = []

                if len(batch) < batch_size:
                    break
        except KeyboardInterrupt:
            if updates:
                try:
                    cursor.executemany('UPDATE files SET phash = ? WHERE id = ?', updates)
                    self.conn.commit()
                except Exception:
                    pass
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            try:
                executor.shutdown(wait=False)
            except Exception:
                pass

        remaining = self.get_pending_phash_count()
        return {
            "processed": processed_count,
            "success": success_count,
            "errors": error_count,
            "remaining": remaining
        }

    def ensure_exif_columns_populated(self, progress_callback: Optional[Callable[[str], None]] = None, batch_size: int = 25000) -> int:
        """
        Preenche as colunas img_width, img_height, exif_date, camera_model
        a partir do JSON de exif_data existente no banco para linhas antigas onde esses campos são NULL.
        Processa em lotes com commit para alta performance e feedback visual contínuo.
        """
        cursor = self.conn.cursor()
        total_updated = 0

        while True:
            cursor.execute('''
                SELECT id, exif_data 
                FROM files 
                WHERE exif_date IS NULL AND exif_data IS NOT NULL AND length(exif_data) > 20
                AND deleted_at IS NULL
                LIMIT ?
            ''', (batch_size,))
            rows = cursor.fetchall()
            if not rows:
                break

            if progress_callback:
                progress_callback(f"Otimizando índices EXIF ({total_updated + len(rows)} registros processados)...")

            updates = []
            for row_id, exif_json in rows:
                try:
                    data = json.loads(exif_json)
                    d, m, w, h = extract_core_exif(data)
                    updates.append((d or "", m, w, h, row_id))
                except Exception:
                    updates.append(("", None, None, None, row_id))

            if updates:
                cursor.executemany('''
                    UPDATE files 
                    SET exif_date = ?, camera_model = ?, img_width = ?, img_height = ?
                    WHERE id = ?
                ''', updates)
                self.conn.commit()
                total_updated += len(updates)

            if len(rows) < batch_size:
                break

        return total_updated

    def commit(self):
        """Salva a transação atual."""
        try:
            self.conn.commit()
        except sqlite3.Error:
            pass
        self.batch_count = 0

    def file_exists(self, filepath: str, size_bytes: int, mtime: float) -> bool:
        """
        Resume-by-Design: verifica se o arquivo já foi processado e não mudou.
        Retorna True se já existe e está atualizado, False caso contrário.
        """
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT size_bytes, mtime FROM files WHERE filepath = ? AND deleted_at IS NULL
        ''', (filepath,))
        row = cursor.fetchone()
        if row:
            db_size, db_mtime = row
            if db_size == size_bytes and abs(db_mtime - mtime) < 1.0:
                return True
        return False

    def find_duplicate(self, size_bytes: int, partial_hash: str) -> Optional[str]:
        """
        Busca uma duplicata exata baseada em tamanho e hash parcial.
        Retorna o filepath do HDD se existir, senão None.
        """
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT filepath FROM files 
            WHERE size_bytes = ? AND partial_hash = ? AND deleted_at IS NULL
        ''', (size_bytes, partial_hash))
        row = cursor.fetchone()
        return row[0] if row else None

    def compare_with_db(self, other_db_path: str) -> List[Dict]:
        """
        Anexa outro banco de dados SQLite e cruza as informações para achar duplicatas exatas (Inner Join).
        """
        cursor = self.conn.cursor()
        cursor.execute("ATTACH DATABASE ? AS db_b", (other_db_path,))
        
        try:
            cursor.execute('''
                SELECT a.filepath, b.filepath, a.size_bytes, a.mtime, b.mtime 
                FROM files a 
                INNER JOIN db_b.files b 
                ON a.size_bytes = b.size_bytes AND a.partial_hash = b.partial_hash
                WHERE a.deleted_at IS NULL AND b.deleted_at IS NULL
            ''')
            
            duplicates = []
            for a_path, b_path, size, a_mtime, b_mtime in cursor.fetchall():
                duplicates.append({
                    "scanned_file": a_path,  # Arquivo no DB principal (A)
                    "db_file": b_path,       # Duplicata encontrada no DB secundário (B)
                    "size_bytes": size,
                    "safe_mtime": a_mtime,
                    "cand_mtime": b_mtime
                })
            return duplicates
        finally:
            cursor.execute("DETACH DATABASE db_b")

    def find_internal_duplicates(self, progress_callback: Optional[Callable[[str], None]] = None) -> List[Dict]:
        """
        Encontra duplicatas internas dentro do mesmo banco de dados:
        1. Duplicatas 100% idênticas (mesmo tamanho em bytes e hash parcial).
        2. Fotos redimensionadas/editadas da mesma foto (mesma câmera e data/hora com tolerância de fuso horário,
           confirmadas por Perceptual Hash sob demanda).
        Retorna pares normalizados (scanned_file = Seguro/Melhor Qualidade, db_file = Candidata a Descarte).
        """
        if progress_callback:
            progress_callback("Normalizando índices e metadados no banco...")
        self.ensure_exif_columns_populated(progress_callback=progress_callback)

        duplicates = []
        handled_pairs = set()
        handled_candidates = set()
        cached_phashes = {}

        def get_phash_lazy(path: str, existing_phash: Optional[str] = None) -> str:
            if existing_phash and len(existing_phash) == 16:
                return existing_phash
            if path in cached_phashes:
                return cached_phashes[path]
            if os.path.exists(path):
                h = compute_phash(path)
                if h:
                    cached_phashes[path] = h
                    try:
                        self.update_file_phash(path, h)
                    except Exception:
                        pass
                    return h
            return ""

        # ========================================================
        # FASE 1: Duplicatas 100% Idênticas (mesmo hash parcial e tamanho)
        # ========================================================
        if progress_callback:
            progress_callback("Buscando cópias idênticas dentro do banco...")

        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT size_bytes, partial_hash, COUNT(*) as cnt
            FROM files
            WHERE size_bytes > 0 AND partial_hash IS NOT NULL AND partial_hash != '' AND partial_hash != 'error_reading_file' AND deleted_at IS NULL
            GROUP BY size_bytes, partial_hash
            HAVING cnt > 1
        ''')
        exact_clusters = cursor.fetchall()

        for size_bytes, partial_hash, cnt in exact_clusters:
            cursor.execute('''
                SELECT filepath, filename, size_bytes, mtime, img_width, img_height, exif_date, phash
                FROM files
                WHERE size_bytes = ? AND partial_hash = ? AND deleted_at IS NULL
            ''', (size_bytes, partial_hash))
            files = [
                {
                    "filepath": r[0], "filename": r[1], "size_bytes": r[2], "mtime": r[3],
                    "img_width": r[4], "img_height": r[5], "exif_date": r[6], "phash": r[7]
                }
                for r in cursor.fetchall()
            ]
            if len(files) < 2:
                continue

            # Ordena por qualidade técnica e nome de arquivo
            files.sort(key=rank_file_quality, reverse=True)
            safe_item = files[0]

            for cand_item in files[1:]:
                p1, p2 = safe_item["filepath"], cand_item["filepath"]
                if p1 == p2 or (p1, p2) in handled_pairs or (p2, p1) in handled_pairs:
                    continue
                handled_pairs.add((p1, p2))
                handled_candidates.add(cand_item["filepath"])

                duplicates.append({
                    "scanned_file": safe_item["filepath"],
                    "db_file": cand_item["filepath"],
                    "size_bytes": cand_item["size_bytes"],
                    "safe_size": safe_item["size_bytes"],
                    "cand_size": cand_item["size_bytes"],
                    "safe_mtime": safe_item["mtime"],
                    "cand_mtime": cand_item["mtime"],
                    "safe_width": safe_item["img_width"],
                    "safe_height": safe_item["img_height"],
                    "cand_width": cand_item["img_width"],
                    "cand_height": cand_item["img_height"],
                    "duplicate_type": "exact",
                    "tz_shifted": False,
                    "shift_hours": 0,
                    "quality_label": "Duplicata Idêntica"
                })

        # ========================================================
        # FASE 2: Fotos Redimensionadas/Editadas (EXIF + Fuso Horário)
        # ========================================================
        if progress_callback:
            progress_callback("Cruzando registros por EXIF e tolerância de fuso horário...")

        cursor.execute('''
            SELECT filepath, filename, size_bytes, mtime, img_width, img_height, exif_date, phash,
                   substr(exif_date, 1, 10) as ymd, camera_model, substr(exif_date, 15, 5) as mmss
            FROM (
                SELECT filepath, filename, size_bytes, mtime, img_width, img_height, exif_date, phash,
                       substr(exif_date, 1, 10) as ymd, camera_model, substr(exif_date, 15, 5) as mmss,
                       COUNT(*) OVER (PARTITION BY substr(exif_date, 1, 10), camera_model, substr(exif_date, 15, 5)) as grp_cnt
                FROM files
                WHERE exif_date IS NOT NULL AND length(exif_date) >= 19 AND camera_model IS NOT NULL AND deleted_at IS NULL
            )
            WHERE grp_cnt > 1
            ORDER BY ymd, camera_model, mmss
        ''')
        candidate_rows = cursor.fetchall()

        groups = itertools.groupby(candidate_rows, key=lambda r: (r[8], r[9], r[10]))

        for (ymd, camera_model, mmss), rows_iter in groups:
            group_files = [
                {
                    "filepath": r[0], "filename": r[1], "size_bytes": r[2], "mtime": r[3],
                    "img_width": r[4], "img_height": r[5], "exif_date": r[6], "phash": r[7]
                }
                for r in rows_iter
            ]
            group_files.sort(key=rank_file_quality, reverse=True)

            n = len(group_files)
            for i in range(n):
                f_a = group_files[i]
                p_a = f_a["filepath"]
                if p_a in handled_candidates:
                    continue

                for j in range(i + 1, n):
                    f_b = group_files[j]
                    p_b = f_b["filepath"]

                    if p_a == p_b or (p_a, p_b) in handled_pairs or (p_b, p_a) in handled_pairs:
                        continue
                    if p_b in handled_candidates:
                        continue

                    # Verifica se data/hora coincidem exatamente OU possuem diferença de fuso
                    is_time_match, shift_hours = is_same_capture_time(f_a["exif_date"], f_b["exif_date"], max_hour_shift=4)
                    if not is_time_match:
                        continue

                    # Validação de identidade de foto e rejeição automática de disparos contínuos (bursts)
                    is_photo_match, _ = are_same_photo_or_variant(f_a["filename"], f_b["filename"])
                    if not is_photo_match:
                        continue

                    # Validação por pHash se ambos já tiverem hash salvo
                    if f_a.get("phash") and f_b.get("phash"):
                        if hamming_distance(f_a["phash"], f_b["phash"]) > 5:
                            continue

                    safe_item = f_a
                    cand_item = f_b

                    handled_pairs.add((p_a, p_b))
                    handled_candidates.add(p_b)

                    # Etiqueta de qualidade
                    w_s = safe_item.get("img_width") or 0
                    h_s = safe_item.get("img_height") or 0
                    mp_s = (w_s * h_s) / 1_000_000.0

                    w_c = cand_item.get("img_width") or 0
                    h_c = cand_item.get("img_height") or 0
                    mp_c = (w_c * h_c) / 1_000_000.0

                    q_label = "Qualidade Inferior"
                    if mp_s > 0 and mp_c > 0 and mp_c < mp_s:
                        diff_pct = round(((mp_s - mp_c) / mp_s) * 100)
                        q_label = f"-{diff_pct}% Resolução"
                    elif cand_item["size_bytes"] < safe_item["size_bytes"]:
                        diff_mb_pct = round(((safe_item["size_bytes"] - cand_item["size_bytes"]) / safe_item["size_bytes"]) * 100)
                        q_label = f"-{diff_mb_pct}% MB (Mais Comprimida)"

                    duplicates.append({
                        "scanned_file": safe_item["filepath"],
                        "db_file": cand_item["filepath"],
                        "size_bytes": cand_item["size_bytes"],
                        "safe_size": safe_item["size_bytes"],
                        "cand_size": cand_item["size_bytes"],
                        "safe_mtime": safe_item["mtime"],
                        "cand_mtime": cand_item["mtime"],
                        "safe_width": safe_item["img_width"],
                        "safe_height": safe_item["img_height"],
                        "cand_width": cand_item["img_width"],
                        "cand_height": cand_item["img_height"],
                        "duplicate_type": "quality_diff",
                        "tz_shifted": (shift_hours != 0),
                        "shift_hours": shift_hours,
                        "quality_label": q_label
                    })

        self.commit()
        return duplicates

    def compare_with_db_deep(self, other_db_path: str, progress_callback: Optional[Callable[[str], None]] = None) -> List[Dict]:
        """
        Cruzamento aprofundado entre DOIS bancos SQLite (DB A = Seguro, DB B = Candidato):
        1. Duplicatas idênticas (mesmo tamanho e hash parcial).
        2. Fotos redimensionadas/editadas (mesma câmera e data/hora com tolerância de fuso, confirmadas por pHash).
        """
        if progress_callback:
            progress_callback("Conectando bancos e verificando índices...")
        self.ensure_exif_columns_populated(progress_callback=progress_callback)

        # Garante colunas no outro DB também
        with contextlib.closing(sqlite3.connect(other_db_path)) as other_conn:
            with other_conn:
                self._ensure_columns(other_conn)

        duplicates = []
        handled_pairs = set()
        cached_phashes = {}

        def get_phash_lazy(path: str, existing_phash: Optional[str] = None) -> str:
            if existing_phash and len(existing_phash) == 16:
                return existing_phash
            if path in cached_phashes:
                return cached_phashes[path]
            if os.path.exists(path):
                h = compute_phash(path)
                if h:
                    cached_phashes[path] = h
                    try:
                        self.update_file_phash(path, h)
                    except Exception:
                        pass
                    return h
            return ""

        cursor = self.conn.cursor()
        cursor.execute("ATTACH DATABASE ? AS db_b", (other_db_path,))

        try:
            # 1. Duplicatas Exatas
            if progress_callback:
                progress_callback("Cruzando duplicatas exatas entre os dois bancos...")
            cursor.execute('''
                SELECT a.filepath, b.filepath, a.size_bytes, b.size_bytes, a.mtime, b.mtime,
                       a.img_width, a.img_height, b.img_width, b.img_height
                FROM files a 
                INNER JOIN db_b.files b 
                ON a.size_bytes = b.size_bytes AND a.partial_hash = b.partial_hash
                WHERE a.deleted_at IS NULL AND b.deleted_at IS NULL
            ''')

            handled_cands = set()
            for a_p, b_p, sz_a, sz_b, m_a, m_b, w_a, h_a, w_b, h_b in cursor.fetchall():
                handled_pairs.add((a_p, b_p))
                handled_cands.add(b_p)
                duplicates.append({
                    "scanned_file": a_p,
                    "db_file": b_p,
                    "size_bytes": sz_b,
                    "safe_size": sz_a,
                    "cand_size": sz_b,
                    "safe_mtime": m_a,
                    "cand_mtime": m_b,
                    "safe_width": w_a,
                    "safe_height": h_a,
                    "cand_width": w_b,
                    "cand_height": h_b,
                    "duplicate_type": "exact",
                    "tz_shifted": False,
                    "shift_hours": 0,
                    "quality_label": "Duplicata Idêntica"
                })

            # 2. Fotos Redimensionadas / Qualidade / Fuso Horário
            if progress_callback:
                progress_callback("Cruzando por EXIF e tolerância de fuso horário entre os bancos...")

            cursor.execute('''
                SELECT a.filepath, b.filepath, a.size_bytes, b.size_bytes, a.mtime, b.mtime,
                       a.exif_date, b.exif_date, a.img_width, a.img_height, b.img_width, b.img_height,
                       a.phash, b.phash
                FROM files a
                INNER JOIN db_b.files b
                ON a.camera_model = b.camera_model
                AND substr(a.exif_date, 1, 10) = substr(b.exif_date, 1, 10)
                AND substr(a.exif_date, 15, 5) = substr(b.exif_date, 15, 5)
                WHERE a.camera_model IS NOT NULL AND a.exif_date IS NOT NULL AND b.exif_date IS NOT NULL
                AND a.deleted_at IS NULL AND b.deleted_at IS NULL
                AND NOT (a.size_bytes = b.size_bytes AND a.partial_hash = b.partial_hash)
            ''')

            for a_p, b_p, sz_a, sz_b, m_a, m_b, d_a, d_b, w_a, h_a, w_b, h_b, ph_a, ph_b in cursor.fetchall():
                if (a_p, b_p) in handled_pairs or b_p in handled_cands:
                    continue

                is_time_match, shift_hours = is_same_capture_time(d_a, d_b, max_hour_shift=4)
                if not is_time_match:
                    continue

                # Validação de identidade de foto e rejeição automática de disparos contínuos (bursts)
                is_photo_match, _ = are_same_photo_or_variant(os.path.basename(a_p), os.path.basename(b_p))
                if not is_photo_match:
                    continue

                if ph_a and ph_b:
                    if hamming_distance(ph_a, ph_b) > 5:
                        continue

                handled_pairs.add((a_p, b_p))
                handled_cands.add(b_p)

                # Avaliação de qualidade
                mp_a = ((w_a or 0) * (h_a or 0)) / 1_000_000.0
                mp_b = ((w_b or 0) * (h_b or 0)) / 1_000_000.0

                q_label = "Qualidade Inferior"
                if mp_a > 0 and mp_b > 0 and mp_b < mp_a:
                    diff_pct = round(((mp_a - mp_b) / mp_a) * 100)
                    q_label = f"-{diff_pct}% Resolução"
                elif mp_b > mp_a and mp_b > 0:
                    q_label = "Aviso: DB B possui maior resolução"
                elif sz_b < sz_a:
                    diff_mb_pct = round(((sz_a - sz_b) / sz_a) * 100)
                    q_label = f"-{diff_mb_pct}% MB (Mais Comprimida)"

                duplicates.append({
                    "scanned_file": a_p, # DB A (Seguro)
                    "db_file": b_p,      # DB B (Candidato)
                    "size_bytes": sz_b,
                    "safe_size": sz_a,
                    "cand_size": sz_b,
                    "safe_mtime": m_a,
                    "cand_mtime": m_b,
                    "safe_width": w_a,
                    "safe_height": h_a,
                    "cand_width": w_b,
                    "cand_height": h_b,
                    "duplicate_type": "quality_diff",
                    "tz_shifted": (shift_hours != 0),
                    "shift_hours": shift_hours,
                    "quality_label": q_label
                })

            return duplicates
        finally:
            cursor.execute("DETACH DATABASE db_b")

    def delete_file(self, filepath: str, reason: str = 'webapp_batch_delete') -> bool:
        """
        Marca o arquivo como deletado (Soft Delete) no banco de dados.
        Retorna True se afetou alguma linha.
        """
        cursor = self.conn.cursor()
        cursor.execute('UPDATE files SET deleted_at = CURRENT_TIMESTAMP, deleted_reason = ? WHERE filepath = ? AND deleted_at IS NULL', (reason, filepath))
        self.conn.commit()
        return cursor.rowcount > 0

    def get_file_info(self, filepath: str) -> Optional[Dict[str, Any]]:
        """
        Retorna os dados cadastrados de um arquivo no banco.
        """
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT id, filepath, filename, size_bytes, mtime, partial_hash, exif_data, scanned_at,
                   img_width, img_height, exif_date, camera_model, phash
            FROM files WHERE filepath = ? AND deleted_at IS NULL
        ''', (filepath,))
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "filepath": row[1],
            "filename": row[2],
            "size_bytes": row[3],
            "mtime": row[4],
            "partial_hash": row[5],
            "exif_data": json.loads(row[6]) if row[6] else {},
            "scanned_at": row[7],
            "img_width": row[8],
            "img_height": row[9],
            "exif_date": row[10],
            "camera_model": row[11],
            "phash": row[12]
        }

    def get_files_info_batch(self, filepaths: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Retorna metadados para um lote de arquivos.
        """
        if not filepaths:
            return {}
        cursor = self.conn.cursor()
        placeholders = ','.join(['?'] * len(filepaths))
        cursor.execute(f'''
            SELECT filepath, filename, size_bytes, mtime, exif_data, img_width, img_height, exif_date, camera_model
            FROM files WHERE filepath IN ({placeholders}) AND deleted_at IS NULL
        ''', filepaths)
        result = {}
        for row in cursor.fetchall():
            result[row[0]] = {
                "filename": row[1],
                "size_bytes": row[2],
                "mtime": row[3],
                "exif_data": json.loads(row[4]) if row[4] else {},
                "img_width": row[5],
                "img_height": row[6],
                "exif_date": row[7],
                "camera_model": row[8]
            }
        return result

    def get_total_files(self) -> int:
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM files WHERE deleted_at IS NULL')
        return cursor.fetchone()[0]

    def close(self):
        self.commit()
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
