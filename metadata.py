import os
import hashlib
import subprocess
import json
from typing import List, Dict, Any, Tuple

def get_file_stats(filepath: str) -> Tuple[int, float]:
    """Retorna tamanho (bytes) e data de modificação."""
    stat = os.stat(filepath)
    return stat.st_size, stat.st_mtime

def get_partial_hash(filepath: str, chunk_size: int = 1024 * 1024) -> str:
    """
    Streaming Hashing (Low RAM Profile).
    Lê o primeiro e o último megabyte do arquivo para gerar um hash extremamente rápido.
    Se o arquivo for menor que 2MB, lê ele inteiro.
    """
    hasher = hashlib.md5()
    try:
        size = os.path.getsize(filepath)
        with open(filepath, 'rb') as f:
            if size <= chunk_size * 2:
                # Arquivo pequeno, ler tudo
                for chunk in iter(lambda: f.read(chunk_size), b""):
                    hasher.update(chunk)
            else:
                # Ler o primeiro MB
                hasher.update(f.read(chunk_size))
                # Ler o último MB
                f.seek(-chunk_size, os.SEEK_END)
                hasher.update(f.read(chunk_size))
    except (OSError, IOError):
        return "error_reading_file"
    
    return hasher.hexdigest()

def batch_get_exif(filepaths: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    External Tool Batching: Chama o exiftool uma única vez para um lote de arquivos.
    Retorna um dicionário mapeando o filepath (absoluto) para os dados EXIF.
    """
    if not filepaths:
        return {}
        
    try:
        # Chama o exiftool com saída JSON e lê múltiplos arquivos
        # -G extrai grupos, -n previne formatação dos valores para ficarem puros (raw)
        args = ['exiftool', '-json', '-G'] + filepaths
        result = subprocess.run(args, capture_output=True, text=True, check=False, encoding='utf-8')
        
        # Pode haver um erro na leitura de um arquivo, mas o exiftool continuará com os outros
        if not result.stdout.strip():
            return {}
            
        data = json.loads(result.stdout)
        
        # Mapear SourceFile (caminho retornado pelo exiftool) para o payload
        exif_map = {}
        for item in data:
            source = item.get('SourceFile')
            if source:
                # Converter para o caminho absoluto (normalizando o separador no Windows)
                abs_source = os.path.abspath(source)
                exif_map[abs_source] = item
                
        return exif_map
        
    except FileNotFoundError:
        # Exiftool não encontrado
        raise RuntimeError("exiftool não está instalado ou não está no PATH.")
    except json.JSONDecodeError:
        return {}
    except Exception:
        return {}
