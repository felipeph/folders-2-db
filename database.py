import sqlite3
import json
import os
import contextlib
from typing import Dict, Any, List

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
        # Escrevemos em um arquivo temporário primeiro se for criar do zero? Não necessariamente pro sqlite, 
        # mas as transações dão a atomicidade necessária.
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
                        scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                # Índices para acelerar a busca de duplicatas
                conn.execute('CREATE INDEX IF NOT EXISTS idx_size_hash ON files(size_bytes, partial_hash)')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_filepath ON files(filepath)')

    def insert_or_update_file(self, filepath: str, filename: str, size_bytes: int, mtime: float, partial_hash: str, exif_data: Dict[str, Any]):
        """
        Insere ou atualiza um registro de arquivo no banco de dados.
        """
        exif_json = json.dumps(exif_data)
        
        try:
            self.conn.execute('''
                INSERT INTO files (filepath, filename, size_bytes, mtime, partial_hash, exif_data)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(filepath) DO UPDATE SET
                    size_bytes=excluded.size_bytes,
                    mtime=excluded.mtime,
                    partial_hash=excluded.partial_hash,
                    exif_data=excluded.exif_data,
                    scanned_at=CURRENT_TIMESTAMP
            ''', (filepath, filename, size_bytes, mtime, partial_hash, exif_json))
            
            self.batch_count += 1
            if self.batch_count >= self.BATCH_LIMIT:
                self.commit()
                
        except sqlite3.Error as e:
            # Em um cenário real, poderiamos logar isso estruturado.
            pass

    def commit(self):
        """Salva a transação atual."""
        if self.batch_count > 0:
            self.conn.commit()
            self.batch_count = 0

    def file_exists(self, filepath: str, size_bytes: int, mtime: float) -> bool:
        """
        Resume-by-Design: verifica se o arquivo já foi processado e não mudou.
        Retorna True se já existe e está atualizado, False caso contrário.
        """
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT size_bytes, mtime FROM files WHERE filepath = ?
        ''', (filepath,))
        row = cursor.fetchone()
        if row:
            db_size, db_mtime = row
            # Considerando uma pequena margem para float point do mtime dependendo do OS
            if db_size == size_bytes and abs(db_mtime - mtime) < 1.0:
                return True
        return False

    def find_duplicate(self, size_bytes: int, partial_hash: str) -> str:
        """
        Busca uma duplicata exata baseada em tamanho e hash parcial (nossa 'assinatura').
        Retorna o filepath do HDD se existir, senão None.
        """
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT filepath FROM files 
            WHERE size_bytes = ? AND partial_hash = ?
        ''', (size_bytes, partial_hash))
        row = cursor.fetchone()
        return row[0] if row else None

    def compare_with_db(self, other_db_path: str) -> List[Dict]:
        """
        Anexa outro banco de dados SQLite e cruza as informações para achar duplicatas (Inner Join).
        Muito mais rápido e usa pouca memória.
        """
        cursor = self.conn.cursor()
        cursor.execute("ATTACH DATABASE ? AS db_b", (other_db_path,))
        
        try:
            cursor.execute('''
                SELECT a.filepath, b.filepath, a.size_bytes 
                FROM files a 
                INNER JOIN db_b.files b 
                ON a.size_bytes = b.size_bytes AND a.partial_hash = b.partial_hash
            ''')
            
            duplicates = []
            for a_path, b_path, size in cursor.fetchall():
                duplicates.append({
                    "scanned_file": a_path,  # Arquivo no DB principal (A)
                    "db_file": b_path,       # Duplicata encontrada no DB secundário (B)
                    "size_bytes": size
                })
            return duplicates
        finally:
            cursor.execute("DETACH DATABASE db_b")

    def delete_file(self, filepath: str) -> bool:
        """
        Remove o registro de um arquivo do banco de dados (usado após exclusão no disco).
        Retorna True se removeu, False se não existia.
        """
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM files WHERE filepath = ?', (filepath,))
        self.conn.commit()
        return cursor.rowcount > 0

    def get_file_info(self, filepath: str) -> Dict[str, Any]:
        """
        Retorna os dados cadastrados de um arquivo no banco.
        """
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT id, filepath, filename, size_bytes, mtime, partial_hash, exif_data, scanned_at 
            FROM files WHERE filepath = ?
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
            "scanned_at": row[7]
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
            SELECT filepath, filename, size_bytes, mtime, exif_data 
            FROM files WHERE filepath IN ({placeholders})
        ''', filepaths)
        result = {}
        for row in cursor.fetchall():
            result[row[0]] = {
                "filename": row[1],
                "size_bytes": row[2],
                "mtime": row[3],
                "exif_data": json.loads(row[4]) if row[4] else {}
            }
        return result

    def get_total_files(self) -> int:
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM files')
        return cursor.fetchone()[0]

    def close(self):
        self.commit()
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
