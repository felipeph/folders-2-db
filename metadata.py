import os
import re
import hashlib
import subprocess
import json
from typing import List, Dict, Any, Tuple, Optional

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

import warnings
from PIL import Image, ImageOps, ImageFile
ImageFile.LOAD_TRUNCATED_IMAGES = True
warnings.filterwarnings("ignore", category=UserWarning, module="PIL")

def compute_phash(filepath: str, hash_size: int = 8) -> str:
    """
    Calcula o Perceptual Hash (dHash) de 64 bits de uma imagem.
    Resiliente a redimensionamento, compressão JPEG e pequenas variações de cor.
    Retorna uma string hexadecimal de 16 caracteres, ou string vazia se falhar.
    """
    if not os.path.exists(filepath):
        return ""
    try:
        import numpy as np

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with Image.open(filepath) as img:
                try:
                    img = ImageOps.exif_transpose(img)
                except Exception:
                    pass
                
                # Converte para escala de cinza e redimensiona para (hash_size + 1, hash_size)
                # O algoritmo dHash compara os gradientes horizontais adjacentes
                img = img.convert('L').resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
                pixels = np.asarray(img, dtype=np.int16)
                
                # Compara pixel esquerdo com pixel direito: shape (hash_size, hash_size) -> 64 booleanos
                diff = pixels[:, 1:] > pixels[:, :-1]
                
                # Empacota os 64 booleanos em um inteiro de 64 bits
                hash_int = 0
                for bit in diff.flatten():
                    hash_int = (hash_int << 1) | int(bit)
                    
                return f"{hash_int:016x}"
    except Exception:
        return ""

def hamming_distance(hash1: str, hash2: str) -> int:
    """
    Calcula a distância de Hamming entre dois hashes hexadecimais de 64 bits.
    Distância <= 5 indica praticamente a mesma imagem com redimensionamento/compressão.
    Distâncias maiores indicam imagens diferentes ou sequências com alteração de cena.
    """
    if not hash1 or not hash2 or len(hash1) != 16 or len(hash2) != 16:
        return 999
    try:
        val1 = int(hash1, 16)
        val2 = int(hash2, 16)
        return bin(val1 ^ val2).count('1')
    except ValueError:
        return 999

def extract_core_exif(exif_data: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[int], Optional[int]]:
    """
    Extrai e normaliza campos estruturados a partir do dicionário retornado pelo exiftool:
    (exif_date, camera_model, img_width, img_height).
    """
    if not exif_data or not isinstance(exif_data, dict):
        return None, None, None, None

    # 1. Data de captura original
    date_candidates = [
        exif_data.get('EXIF:DateTimeOriginal'),
        exif_data.get('EXIF:CreateDate'),
        exif_data.get('QuickTime:CreateDate'),
        exif_data.get('XMP:DateCreated'),
        exif_data.get('DateTimeOriginal'),
        exif_data.get('CreateDate')
    ]
    exif_date = None
    for d in date_candidates:
        if d and isinstance(d, str):
            clean_d = d.strip().split('+')[0].split('-')[0].strip() if ('+' in d or (d.count('-') > 2)) else d.strip()
            # Formato padrão EXIF: 'YYYY:MM:DD HH:MM:SS'
            if len(clean_d) >= 19:
                exif_date = clean_d[:19]
                break

    # 2. Modelo / Fabricante da Câmera
    model = exif_data.get('EXIF:Model') or exif_data.get('Model')
    make = exif_data.get('EXIF:Make') or exif_data.get('Make')
    camera_model = None
    if model and make:
        m_str, mk_str = str(model).strip(), str(make).strip()
        camera_model = m_str if mk_str.lower() in m_str.lower() else f"{mk_str} {m_str}"
    elif model:
        camera_model = str(model).strip()
    elif make:
        camera_model = str(make).strip()

    # 3. Largura e Altura
    w = (
        exif_data.get('File:ImageWidth')
        or exif_data.get('EXIF:ExifImageWidth')
        or exif_data.get('Composite:ImageWidth')
        or exif_data.get('ImageWidth')
    )
    h = (
        exif_data.get('File:ImageHeight')
        or exif_data.get('EXIF:ExifImageHeight')
        or exif_data.get('Composite:ImageHeight')
        or exif_data.get('ImageHeight')
    )
    
    img_width, img_height = None, None
    try:
        if w is not None:
            img_width = int(w)
        if h is not None:
            img_height = int(h)
    except (ValueError, TypeError):
        pass

    if (not img_width or not img_height) and 'Composite:ImageSize' in exif_data:
        size_str = str(exif_data['Composite:ImageSize'])
        if 'x' in size_str:
            parts = size_str.split('x')
            try:
                img_width, img_height = int(parts[0]), int(parts[1])
            except Exception:
                pass

    return exif_date, camera_model, img_width, img_height

def is_same_capture_time(dt1: str, dt2: str, max_hour_shift: int = 4) -> Tuple[bool, int]:
    """
    Verifica se duas datas/horas de captura representam o mesmo momento,
    suportando igualdade exata OU deslocamento de horas inteiras (ex: fuso horário, horário de verão).
    Retorna (is_match, hour_offset).
    """
    if not dt1 or not dt2:
        return False, 0
    if dt1 == dt2:
        return True, 0

    try:
        if len(dt1) < 19 or len(dt2) < 19:
            return False, 0

        # Normaliza formato YYYY:MM:DD HH:MM:SS
        s1 = dt1[:19].replace('-', ':')
        s2 = dt2[:19].replace('-', ':')

        # Minuto e segundo devem ser idênticos (:MM:SS)
        if s1[14:19] != s2[14:19]:
            return False, 0

        # Caminho ultra-rápido: mesmo dia (YYYY:MM:DD)
        if s1[:10] == s2[:10]:
            h1 = int(s1[11:13])
            h2 = int(s2[11:13])
            shift = h2 - h1
            if abs(shift) <= max_hour_shift:
                return True, shift
            return False, 0

        # Fallback para transição de meia-noite (raro)
        from datetime import datetime
        t1 = datetime.strptime(s1, "%Y:%m:%d %H:%M:%S")
        t2 = datetime.strptime(s2, "%Y:%m:%d %H:%M:%S")
        diff_seconds = int((t2 - t1).total_seconds())
        if diff_seconds % 3600 == 0:
            shift = diff_seconds // 3600
            if abs(shift) <= max_hour_shift:
                return True, shift

        return False, 0
    except Exception:
        return False, 0

def get_image_dimensions(filepath: str) -> Tuple[Optional[int], Optional[int]]:
    """Lê as dimensões diretamente da imagem via Pillow caso não estejam no EXIF."""
    if not os.path.exists(filepath):
        return None, None
    try:
        from PIL import Image
        with Image.open(filepath) as img:
            return img.size # (width, height)
    except Exception:
        return None, None

def parse_filename_identity(filename: str) -> Tuple[str, Optional[str], Optional[str], str]:
    """
    Retorna uma tupla (tipo, chave_de_identidade, id_sequencial, copy_clean_name):
      tipo: 'datetime' | 'camera_seq' | 'generic'
    """
    base = os.path.splitext(filename)[0]
    
    # 1. Verifica primeiro se possui timestamp completo (YYYY[-_]MM[-_]DD e HH[-_]MM[-_]SS ou HH[-_]MM)
    dt_match = re.search(r'(\d{4})[-_](\d{2})[-_](\d{2})[-_ ]+(\d{2})[-_](\d{2})(?:[-_](\d{2}))?', base)
    if dt_match:
        g = dt_match.groups()
        sec = g[5] or "00"
        dt_key = f"{g[0]}:{g[1]}:{g[2]} {g[3]}:{g[4]}:{sec}"
        rest = base[dt_match.end():]
        rest = re.sub(r'[-_]?\b\d{3,4}x\d{3,4}\b', '', rest)
        rest = re.sub(r'[\s_\-]*[\(\[]\d+[\)\]]$', '', rest)
        seq_match = re.findall(r'\d{3,}', rest)
        seq_id = seq_match[-1] if seq_match else None
        return ('datetime', dt_key, seq_id, base.lower())

    # 2. Limpa sufixos de cópia clássicos no final do nome para outros formatos
    clean = re.sub(r'[\s_\-]*[\(\[]\d+[\)\]]$', '', base)
    clean = re.sub(r'[-_](?:copy|copia|edit|edited|whatsapp|\d{1,2})$', '', clean, flags=re.IGNORECASE)
    clean_lower = clean.lower()

    # 3. Verifica se é nome com prefixo de ano e sequencial (ex: 2016-1706)
    year_seq_match = re.match(r'^(\d{4})[-_](\d{3,})$', clean)
    if year_seq_match:
        return ('camera_seq', None, year_seq_match.group(2), clean_lower)

    # 4. Verifica se é nome padrão de câmera (ex: IMG_1234, DSC_0567, 100_0001, SAM_1234)
    cam_match = re.search(r'(?:img|dsc|sam|pic|p|mov|mvi|cimg)[-_]?(\d{3,})', clean, flags=re.IGNORECASE)
    if cam_match:
        return ('camera_seq', None, cam_match.group(1), clean_lower)
        
    digits = re.findall(r'\d{3,}', clean)
    seq_id = digits[-1] if digits else None
    return ('generic', None, seq_id, clean_lower)

def are_same_photo_or_variant(fn_a: str, fn_b: str, exif_a: Optional[dict] = None, exif_b: Optional[dict] = None) -> Tuple[bool, str]:
    """
    Verifica se dois nomes de arquivos representam a mesma foto (duplicata/variante) ou fotos diferentes (burst/rajada).
    Retorna (is_match, reason).
    """
    # Verificação de rajada gravada no EXIF (SequenceNumber / SubSecTime)
    if exif_a and exif_b:
        for k in ['MakerNotes:SequenceNumber', 'SequenceNumber', 'MakerNotes:ShotNumber']:
            if k in exif_a and k in exif_b:
                try:
                    s_a, s_b = int(exif_a[k]), int(exif_b[k])
                    if s_a != s_b and s_a > 0 and s_b > 0:
                        return False, f"burst_by_exif_sequence ({s_a} vs {s_b})"
                except (ValueError, TypeError):
                    pass
        for k in ['EXIF:SubSecTimeOriginal', 'EXIF:SubSecTime', 'Composite:SubSecDateTimeOriginal']:
            if k in exif_a and k in exif_b:
                v_a, v_b = str(exif_a[k]).strip(), str(exif_b[k]).strip()
                if v_a and v_b and v_a != v_b:
                    return False, f"burst_by_exif_subsecond ({v_a} vs {v_b})"

    t_a, dt_a, seq_a, cl_a = parse_filename_identity(fn_a)
    t_b, dt_b, seq_b, cl_b = parse_filename_identity(fn_b)

    # 1. Nomes limpos de sufixos de cópia idênticos (ex: IMG_2482 vs IMG_2482_1)
    if cl_a and cl_b and cl_a == cl_b:
        return True, "identical_clean_name"

    # 2. Ambos possuem timestamps no nome (ex: 2004-05-08_12-55-02 vs 2004-05-09_21-59-20)
    if t_a == 'datetime' and t_b == 'datetime':
        if dt_a != dt_b:
            return False, f"different_named_timestamps ({dt_a} vs {dt_b})"
        if seq_a and seq_b:
            if seq_a == seq_b:
                return True, "same_datetime_and_seq"
            else:
                return False, f"burst_different_frame_ids ({seq_a} vs {seq_b})"
        return True, "same_datetime_key"

    # 3. Ambos possuem sequencial de câmera (ex: IMG_1706 vs 2016-1706, ou IMG_0001 vs IMG_0002)
    if seq_a and seq_b:
        if seq_a == seq_b:
            return True, f"same_camera_seq_id ({seq_a})"
        else:
            return False, f"different_camera_seq_ids ({seq_a} vs {seq_b})"

    # 4. Um nome contém o outro (ex: 'IMG_1234' in 'IMG_1234_copia')
    if cl_a in cl_b or cl_b in cl_a:
        return True, "contains_name"

    return False, "no_relation"
