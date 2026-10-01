"""
批量解压工具 v2.1.1
- v2.1.1 修复：顶层目录名再去掉“分卷”类标记（C200999_分卷.part1.rar -> C200999；
  同时支持 -分卷2 / （分卷） / _第1卷 / _卷12 / _part2 / _分割；连写无分隔符的不动）
- v2.1.0 新功能：「冲突处理」改为「目标已存在时」并真正生效
  （保留原目录另建副本 / 合并·同名保留旧的 / 合并·同名自动改名 / 覆盖·用新的替换）
- v2.1.0 界面精简：删除 4 个原本不生效的控件（递归模式、递归解压、智能提升单文件夹、
  全局去除冗余嵌套）；目录策略由 A/B/C 精简为「保留源文件夹名 / 不保留」两项
- v2.1.0 修复：伪装包顶层目录名去掉多余后缀（AZA788666.rar.jpg -> AZA788666）
- v2.0.1 修复：分卷输出目录名归一化、分卷包接入密码表撞库、口令经 redact_secrets 脱敏、
  打包内嵌 7z.dll（此前只内嵌 7z.exe，分卷/RAR 解压全部失败）
- 重构解压引擎：解压与目录整理分离
- 支持旧式 ZIP 分卷、RAR/7z 分卷组（.part1.rar 等）
- 包装目录折叠 + 同名嵌套合并
- 密码 DPAPI 加密存储
"""

import sys
import os
import shutil
import subprocess
import zipfile
import tarfile
import json
import base64
import ctypes
from pathlib import Path
import re

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QTableWidget, QTableWidgetItem, QTextEdit,
    QProgressBar, QLabel, QFileDialog, QMessageBox, QMenu, QHeaderView,
    QStyleFactory, QAbstractItemView, QCheckBox, QStyle, QDialog,
    QListWidget, QInputDialog, QDialogButtonBox, QTreeView, QFileSystemModel,
    QListView, QComboBox
)

try:
    import py7zr
    HAS_PY7ZR = True
except ImportError:
    HAS_PY7ZR = False

try:
    from send2trash import send2trash
    HAS_SEND2TRASH = True
except ImportError:
    HAS_SEND2TRASH = False

# ---------- 密码加密（Windows DPAPI - Crypt32.dll） ----------
def _dpapi_encrypt(data: bytes) -> bytes:
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [('cbData', ctypes.c_uint32),
                    ('pbData', ctypes.POINTER(ctypes.c_ubyte))]
    crypt32 = ctypes.windll.crypt32
    data_buffer = ctypes.create_string_buffer(data, len(data))
    in_blob = DATA_BLOB(len(data), ctypes.cast(data_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    out_blob = DATA_BLOB()
    ok = crypt32.CryptProtectData(ctypes.byref(in_blob), "BatchExtractor",
                                  None, None, None, 0, ctypes.byref(out_blob))
    if not ok:
        raise RuntimeError("CryptProtectData failed")
    try:
        result = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    return result

def _dpapi_decrypt(data: bytes) -> bytes:
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [('cbData', ctypes.c_uint32),
                    ('pbData', ctypes.POINTER(ctypes.c_ubyte))]
    crypt32 = ctypes.windll.crypt32
    data_buffer = ctypes.create_string_buffer(data, len(data))
    in_blob = DATA_BLOB(len(data), ctypes.cast(data_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    out_blob = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(ctypes.byref(in_blob), "BatchExtractor",
                                    None, None, None, 0, ctypes.byref(out_blob))
    if not ok:
        raise RuntimeError("CryptUnprotectData failed")
    try:
        result = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    return result

def encrypt_text(text: str) -> str:
    if sys.platform == 'win32':
        encrypted = _dpapi_encrypt(text.encode('utf-8'))
        return base64.b64encode(encrypted).decode('ascii')
    else:
        return base64.b64encode(text.encode('utf-8')).decode('ascii')

def decrypt_text(encoded: str) -> str:
    raw = base64.b64decode(encoded)
    if sys.platform == 'win32':
        decrypted = _dpapi_decrypt(raw)
        return decrypted.decode('utf-8')
    else:
        return raw.decode('utf-8')

# ---------- 文件类型识别 ----------
def detect_archive_type_by_magic(file_path: Path):
    try:
        with open(file_path, 'rb') as f:
            magic = f.read(8)
    except Exception:
        return None
    if len(magic) < 2:
        return None
    if magic[0:2] == b'PK':
        return 'zip'
    if magic[0:4] == b'Rar!':
        return 'rar'
    if magic[0:6] == b'7z\xBC\xAF\x27\x1C':
        return '7z'
    try:
        with open(file_path, 'rb') as f:
            f.seek(257)
            tar_magic = f.read(5)
            if tar_magic == b'ustar':
                return 'tar'
    except Exception:
        pass
    if magic[0:2] == b'\x1f\x8b':
        return 'gzip'
    if magic[0:3] == b'BZh':
        return 'bzip2'
    if magic[0:6] == b'\xFD7zXZ\x00':
        return 'xz'
    return None

def is_archive(path: Path) -> bool:
    name = path.name.lower()
    if name.endswith(('.zip', '.7z', '.rar', '.tar', '.tar.gz', '.tgz', '.bz2', '.xz')):
        return True
    if re.search(r'\.part\d+\.(rar|zip|7z)$', name):
        return True
    if re.search(r'\.(zip|7z|rar)\.\d{3}$', name):
        return True
    return False

def is_split_volume(path: Path) -> bool:
    """判断是否为分卷的后续卷（非主卷）"""
    name = path.name.lower()
    m = re.search(r'\.part(\d+)\.(rar|zip|7z)$', name)
    if m and int(m.group(1)) > 1:
        return True
    m = re.search(r'\.(zip|7z|rar)\.(\d{3})$', name)
    if m and int(m.group(2)) > 1:
        return True
    return False

def is_split_main(path: Path) -> bool:
    """判断是否为分卷的主卷（第一个分卷）"""
    name = path.name.lower()
    m = re.search(r'\.part1\.(rar|zip|7z)$', name)
    if m:
        return True
    m = re.search(r'\.(zip|7z|rar)\.001$', name)
    if m:
        return True
    return False

def is_zip_split_volume(path: Path) -> bool:
    """判断是否为旧式 ZIP 分卷的后续卷（.z01 等）"""
    name = path.name.lower()
    return re.search(r'\.z\d{2}$', name) is not None

def archive_base_name(path: Path) -> str:
    """
    输出目录名归一化：剥掉分卷标记、复合扩展名与伪装扩展名，返回压缩包的“基名”。

    历史缺陷：目标子目录曾直接用 Path.stem，而 stem 只去掉最后一段扩展名，
    于是分卷输入会把分卷标记留在目录名里——
        19284.part1.rar  ->  19284.part1    （应为 19284）
        PC10009.7z.001   ->  PC10009.7z     （应为 PC10009）
    旧版靠“目录折叠”顺手折掉残尾（设计史 L19411），v1.1.0 起折叠只处理
    “仅含单个子目录”的目录，残尾便留了下来。这里统一在命名处归一，
    不再依赖任何会改名/移动目录的折叠逻辑。

    伪装扩展名不在这里处理——那属于"解压完成后清理顶层目录名"的一步
    （见 `_strip_top_dir_suffix` / `strip_junk_suffix`），命名阶段只负责分卷与复合扩展名。
    """
    name = path.name
    # xxx.partN.rar / .zip / .7z
    m = re.match(r'^(.*)\.part\d+\.(rar|zip|7z)$', name, re.IGNORECASE)
    if m:
        return m.group(1)
    # xxx.rar.001 / xxx.zip.002 / xxx.7z.001
    m = re.match(r'^(.*)\.(rar|zip|7z)\.\d{3}$', name, re.IGNORECASE)
    if m:
        return m.group(1)
    # 旧式 ZIP 分卷后续卷 xxx.z01（通常不作为任务，兜底同样归一）
    m = re.match(r'^(.*)\.z\d{2}$', name, re.IGNORECASE)
    if m:
        return m.group(1)
    # xxx.tar.gz / xxx.tar.bz2 / xxx.tar.xz
    m = re.match(r'^(.*)\.tar\.(gz|bz2|xz)$', name, re.IGNORECASE)
    if m:
        return m.group(1)
    return path.stem

# 顶层目录名尾部的“不需要的后缀”：压缩包后缀 + 常见伪装后缀。
# 例：AZA788666.rar.jpg 解压出的顶层目录是 AZA788666.rar → 去掉 .rar → AZA788666。
JUNK_SUFFIXES = {
    '.rar', '.zip', '.7z', '.tar', '.gz', '.bz2', '.xz', '.tgz', '.tbz2', '.txz',
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.txt', '.apk', '.iso',
}

# 顶层目录名尾部的“分卷”类标记：C200999_分卷.part1.rar 解出的顶层目录是 C200999_分卷
# （`.part1.rar` 已在 archive_base_name 里剥掉）→ 再去掉 `_分卷` → C200999。
# 括号形式（（分卷）、(分卷)）一并支持。
#
# 刻意保守，避免误伤标题：
#   · 必须有分隔符或左括号（`C200999分卷` 这种连写的**不动**）；
#   · `卷` / `part` 必须带数字（`Game_part`、`最终卷` 不动）；`分卷` / `分割` 本身已是强信号，可无数字。
VOLUME_MARKER_RE = re.compile(
    r'^(?P<base>.+?)'
    r'[\s._\-—·~\(（\[\【]+'
    r'(?:第?\s*\d*\s*分卷\s*\d*'   # _分卷 / _分卷1 / _2分卷 / _第1分卷 / _第1分卷2
    r'|分割\s*\d*'               # _分割 / _分割1
    r'|第\s*\d+\s*卷'             # _第1卷
    r'|卷\s*\d+'                 # _卷1 / _卷12
    r'|part\s*\d+'               # _part1 / _part12
    r')'
    r'\s*[\)）\]\】]?$',
    re.IGNORECASE,
)

def strip_junk_suffix(name: str) -> str:
    """
    去掉名字尾部所有不需要的“后缀”，可连续多段，循环剥到稳定：

    1. 压缩包后缀与常见伪装后缀（`JUNK_SUFFIXES`，形如 `.rar.jpg`）；
    2. “分卷”类标记（`VOLUME_MARKER_RE`，形如 `_分卷` / `-分卷2` / `（分卷）` / `_第1卷` / `_part1`）。

    两段共用一个循环，才能一次剥净 `AZA788666_分卷.rar.jpg` 这种组合。
    """
    stem = name
    while True:
        base, ext = os.path.splitext(stem)
        if base and ext and ext.lower() in JUNK_SUFFIXES:
            stem = base
            continue
        m = VOLUME_MARKER_RE.match(stem)
        if m:
            stem = m.group('base')
            continue
        return stem

# “目标已存在时”的四种处理方式（顺序即界面下拉框顺序）
CONFLICT_ORDER = ('keep', 'merge_skip', 'merge_rename', 'overwrite')
CONFLICT_LABELS = {
    'keep': '保留原目录（另建副本）',
    'merge_skip': '合并（同名保留旧的）',
    'merge_rename': '合并（同名自动改名）',
    'overwrite': '覆盖（用新的替换）',
}

def find_zip_volume_files(main_zip_path: Path) -> list:
    """返回与主 zip 文件相关的所有分卷文件（不区分大小写）"""
    parent = main_zip_path.parent
    stem = main_zip_path.stem.lower()
    volumes = []
    for f in parent.iterdir():
        if f.is_file():
            fn = f.name.lower()
            if fn == stem + '.zip' or re.match(re.escape(stem) + r'\.z\d{2}$', fn):
                volumes.append(f)
    return volumes

def find_split_volume_files(main_file: Path) -> list:
    """
    返回与主分卷文件（如 xxx.part1.rar）同目录下的所有分卷文件。
    支持 xxx.part1.rar、xxx.part2.rar 等命名，或 xxx.rar.001 等。
    """
    parent = main_file.parent
    name = main_file.name.lower()
    # 尝试 .partN.ext 模式
    m = re.match(r'^(.*)\.part(\d+)\.(rar|zip|7z)$', name)
    if m:
        base = m.group(1)
        ext = '.' + m.group(3)
        pattern = re.compile(re.escape(base) + r'\.part\d+' + re.escape(ext) + r'$', re.IGNORECASE)
        volumes = []
        for f in parent.iterdir():
            if f.is_file() and pattern.match(f.name):
                volumes.append(f)
        volumes.sort(key=lambda x: x.name.lower())
        return volumes
    # 尝试 .001/.002 模式
    m2 = re.match(r'^(.*)\.(zip|7z|rar)\.(\d{3})$', name)
    if m2:
        base = m2.group(1)
        ext = '.' + m2.group(2)
        pattern = re.compile(re.escape(base) + r'\.' + re.escape(m2.group(2)) + r'\.\d{3}$', re.IGNORECASE)
        volumes = []
        for f in parent.iterdir():
            if f.is_file() and pattern.match(f.name):
                volumes.append(f)
        volumes.sort(key=lambda x: x.name.lower())
        return volumes
    return [main_file]

# ---------- 查找外部工具 ----------
def find_7z():
    candidates = ['7z.exe', '7za.exe', '7z', '7za']
    app_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    for name in candidates:
        p = app_dir / name
        if p.exists():
            return str(p)
    if hasattr(sys, '_MEIPASS'):
        meipass = Path(sys._MEIPASS)
        for name in candidates:
            p = meipass / name
            if p.exists():
                return str(p)
    for name in candidates:
        p = Path.cwd() / name
        if p.exists():
            return str(p)
    for name in candidates:
        found = shutil.which(name)
        if found:
            return found
    return None

def find_unrar():
    candidates = ['UnRAR.exe', 'unrar', 'UnRAR']
    app_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    for name in candidates:
        p = app_dir / name
        if p.exists():
            return str(p)
    if hasattr(sys, '_MEIPASS'):
        meipass = Path(sys._MEIPASS)
        for name in candidates:
            p = meipass / name
            if p.exists():
                return str(p)
    common = [
        Path("C:/Program Files/WinRAR/UnRAR.exe"),
        Path("C:/Program Files (x86)/WinRAR/UnRAR.exe"),
        Path("C:/Program Files/WinRAR/WinRAR.exe"),
        Path("C:/Program Files (x86)/WinRAR/WinRAR.exe"),
    ]
    for p in common:
        if p.exists():
            return str(p)
    for name in candidates:
        found = shutil.which(name)
        if found:
            return found
    return None

def find_bandizip():
    candidates = ['bz.exe', 'Bandizip.exe', 'bz']
    app_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    for name in candidates:
        p = app_dir / name
        if p.exists() and (p.parent / 'ark.x64.dll').exists():
            return str(p)
    if hasattr(sys, '_MEIPASS'):
        meipass = Path(sys._MEIPASS)
        for name in candidates:
            p = meipass / name
            if p.exists() and (p.parent / 'ark.x64.dll').exists():
                return str(p)
    common = [
        Path("C:/Program Files/Bandizip/bz.exe"),
        Path("C:/Program Files (x86)/Bandizip/bz.exe"),
        Path("C:/Program Files/Bandizip/Bandizip.exe"),
        Path("C:/Program Files (x86)/Bandizip/Bandizip.exe"),
    ]
    for p in common:
        if p.exists() and (p.parent / 'ark.x64.dll').exists():
            return str(p)
    for name in candidates:
        found = shutil.which(name)
        if found:
            p = Path(found)
            if (p.parent / 'ark.x64.dll').exists():
                return found
    return None

# ---------- 解压核心 ----------
EXTERNAL_TIMEOUT = 300

def run_hidden(cmd, timeout=EXTERNAL_TIMEOUT):
    creationflags = 0
    if os.name == 'nt':
        creationflags = subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True,
                              creationflags=creationflags, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"外部命令超时（{timeout}秒）: {' '.join(cmd)}")

def redact_secrets(text) -> str:
    """
    抹掉文本里可能出现的密码，避免口令进入日志与"完成汇总"。

    必要性：subprocess 的 CalledProcessError 会把**完整命令行**带进异常字符串，
    而 7z 的密码是以 `-p<password>` 形式出现在命令行里的；7z 自身的 stderr 也可能回显。
    """
    s = str(text if text is not None else '')
    # -p<password>：前面可能是空格、引号、括号或逗号（CalledProcessError 会把每个参数
    # 用引号包起来），但**不能**把 `D:\-pics` 这类路径误伤，所以排除路径字符。
    s = re.sub(r"""(?<![A-Za-z0-9_:\.\\])-p\S+""", '-p***', s)
    s = re.sub(r'(?i)(password\s*[=:]\s*)\S+', r'\1***', s)
    return s

def extract_with_7z(archive_path: Path, dest_dir: Path, password: str = None):
    seven_zip = find_7z()
    if not seven_zip:
        raise RuntimeError("未找到 7-Zip")
    cmd = [seven_zip, 'x', str(archive_path), f'-o{dest_dir}', '-y']
    if password:
        cmd.append(f'-p{password}')
    run_hidden(cmd)

def extract_with_unrar(archive_path: Path, dest_dir: Path, password: str = None):
    unrar = find_unrar()
    if not unrar:
        raise RuntimeError("未找到 UnRAR")
    cmd = [unrar, 'x', '-y']
    if password:
        cmd.append(f'-p{password}')
    cmd += [str(archive_path), str(dest_dir) + os.sep]
    run_hidden(cmd)

def extract_with_bandizip(archive_path: Path, dest_dir: Path, password: str = None):
    bz = find_bandizip()
    if not bz:
        raise RuntimeError("未找到可用的 Bandizip（缺少依赖 DLL）")
    cmd = [bz, 'x', f'-o:{dest_dir}']
    if password:
        cmd.append(f'-p:{password}')
    cmd.append(str(archive_path))
    run_hidden(cmd)

def safe_extract_zip(zip_path: Path, dest_dir: Path, password: str = None):
    dest = dest_dir.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            member_path = (dest / member.filename).resolve()
            if dest != member_path and dest not in member_path.parents:
                raise ValueError(f"检测到非法路径: {member.filename}")
        if password:
            pwd = password.encode('utf-8')
            zf.extractall(dest_dir, pwd=pwd)
        else:
            zf.extractall(dest_dir)

def extract_with_py7zr(archive_path: Path, dest_dir: Path, password: str = None):
    with py7zr.SevenZipFile(archive_path, mode='r', password=password) as z:
        z.extractall(path=dest_dir)

def extract_archive_advanced(archive_path: Path, dest_dir: Path, passwords: list = None):
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = archive_path.name.lower()
    # 1. 内置库尝试（无密码）
    try:
        if name.endswith(('.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar')):
            with tarfile.open(archive_path) as tf:
                for member in tf.getmembers():
                    member_path = (dest_dir / member.name).resolve()
                    if dest_dir.resolve() != member_path and dest_dir.resolve() not in member_path.parents:
                        raise ValueError(f"检测到非法路径: {member.name}")
                tf.extractall(dest_dir)
            return
        elif name.endswith('.zip'):
            safe_extract_zip(archive_path, dest_dir)
            return
        elif name.endswith('.7z') and HAS_PY7ZR:
            extract_with_py7zr(archive_path, dest_dir)
            return
    except Exception:
        pass

    # 2. 外部工具尝试（无密码）
    last_error = None
    if find_7z():
        try:
            extract_with_7z(archive_path, dest_dir)
            return
        except Exception as e:
            last_error = e
    if find_unrar():
        try:
            extract_with_unrar(archive_path, dest_dir)
            return
        except Exception as e:
            if last_error is None:
                last_error = e
    if find_bandizip():
        try:
            extract_with_bandizip(archive_path, dest_dir)
            return
        except Exception as e:
            if last_error is None:
                last_error = e

    # 3. 密码尝试
    if passwords:
        for pwd in passwords:
            try:
                if name.endswith('.zip'):
                    safe_extract_zip(archive_path, dest_dir, password=pwd)
                    return
                elif name.endswith('.7z') and HAS_PY7ZR:
                    extract_with_py7zr(archive_path, dest_dir, password=pwd)
                    return
                elif find_7z():
                    extract_with_7z(archive_path, dest_dir, password=pwd)
                    return
                elif find_unrar():
                    extract_with_unrar(archive_path, dest_dir, password=pwd)
                    return
                elif find_bandizip():
                    extract_with_bandizip(archive_path, dest_dir, password=pwd)
                    return
            except Exception:
                continue
        raise RuntimeError("所有密码尝试均失败，文件可能密码错误或损坏")
    else:
        if last_error:
            raise last_error
        raise RuntimeError("解压失败，可能文件损坏或需要密码（未提供密码）")

def unique_dir(base_path: Path) -> Path:
    if not base_path.exists():
        return base_path
    parent = base_path.parent
    stem = base_path.name
    counter = 1
    while True:
        candidate = parent / f"{stem}({counter})"
        if not candidate.exists():
            return candidate
        counter += 1

def safe_delete(file_path: Path, use_trash: bool = True):
    if use_trash and HAS_SEND2TRASH:
        send2trash(str(file_path))
    else:
        if file_path.is_file():
            file_path.unlink()
        elif file_path.is_dir():
            shutil.rmtree(file_path)

# ---------- 任务模型 ----------
class ArchiveTask:
    def __init__(self, main_file: Path, relative_parent: Path = Path('.'), volumes: list = None):
        self.main_file = main_file
        self.relative_parent = relative_parent
        self.volumes = volumes if volumes is not None else [main_file]
        self.is_split = len(self.volumes) > 1

# ---------- 扫描函数 ----------
def scan_archives_recursive(root: Path, base_for_rel: Path = None) -> list:
    """递归扫描 root 下所有压缩包，返回 ArchiveTask 列表，跳过分卷后续卷并构建分卷组"""
    if base_for_rel is None:
        base_for_rel = root
    root_res = root.resolve()
    base_res = base_for_rel.resolve()
    tasks = []
    for dirpath, dirs, files in os.walk(root):
        dir_path = Path(dirpath)
        for f in files:
            fp = dir_path / f
            # 跳过旧式 ZIP 分卷后续卷
            if is_zip_split_volume(fp):
                continue
            # 跳过其它分卷的后续卷（如 .part2.rar）
            if is_split_volume(fp):
                continue
            # 判断是否为压缩包
            if not (is_archive(fp) or detect_archive_type_by_magic(fp)):
                continue

            try:
                rel_parent = fp.parent.relative_to(base_res)
            except ValueError:
                rel_parent = Path('.')

            # 处理旧式 ZIP 分卷
            if fp.name.lower().endswith('.zip'):
                vols = find_zip_volume_files(fp)
                if len(vols) > 1:
                    tasks.append(ArchiveTask(fp, rel_parent, vols))
                else:
                    tasks.append(ArchiveTask(fp, rel_parent, [fp]))
                continue

            # 处理 RAR/7z 分卷主文件
            if is_split_main(fp):
                vols = find_split_volume_files(fp)
                if len(vols) > 1:
                    tasks.append(ArchiveTask(fp, rel_parent, vols))
                else:
                    tasks.append(ArchiveTask(fp, rel_parent, [fp]))
                continue

            # 普通压缩包
            tasks.append(ArchiveTask(fp, rel_parent, [fp]))
    return tasks

# ---------- 密码管理对话框 ----------
class PasswordManagerDialog(QDialog):
    def __init__(self, password_list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("密码管理")
        self.setMinimumSize(400, 300)
        layout = QVBoxLayout(self)
        self.list_widget = QListWidget()
        for pwd in password_list:
            self.list_widget.addItem(pwd)
        layout.addWidget(self.list_widget)

        btn_layout = QHBoxLayout()
        add_btn = QPushButton("添加密码")
        add_btn.clicked.connect(self.add_password)
        remove_btn = QPushButton("删除选中")
        remove_btn.clicked.connect(self.remove_password)
        btn_layout.addWidget(add_btn)
        btn_layout.addWidget(remove_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def add_password(self):
        text, ok = QInputDialog.getText(self, "添加密码", "请输入密码（加密存储）:")
        if ok and text.strip():
            self.list_widget.addItem(text.strip())

    def remove_password(self):
        for item in self.list_widget.selectedItems():
            self.list_widget.takeItem(self.list_widget.row(item))

    def get_passwords(self):
        return [self.list_widget.item(i).text() for i in range(self.list_widget.count())]

# ---------- 工作线程 ----------
class ExtractWorker(QThread):
    log_signal = Signal(str)
    status_signal = Signal(int, str)
    progress_signal = Signal(int)
    finished_signal = Signal()
    summary_signal = Signal(dict)

    def __init__(self, jobs, target_root, passwords, strategy='B',
                 delete_intermediate=True, max_depth=10, conflict_policy='keep',
                 use_trash=True, parent=None):
        super().__init__(parent)
        # jobs 是 (task, row) 元组列表
        valid_jobs = []
        for item in jobs:
            if isinstance(item, (tuple, list)) and len(item) == 2 \
                    and isinstance(item[0], ArchiveTask) and isinstance(item[1], int):
                valid_jobs.append((item[0], item[1]))
        self.jobs = valid_jobs
        self.tasks = [job for job, _ in valid_jobs]

        self.target_root = Path(target_root)
        self.passwords = passwords
        self.strategy = strategy
        self.delete_intermediate = delete_intermediate
        self.max_depth = max_depth
        self.conflict_policy = conflict_policy
        self.use_trash = use_trash
        self._is_cancelled = False
        self.seen_archives = set()
        self.failed_archives = {}
        self.initial_top_dirs = set()

    def cancel(self):
        self._is_cancelled = True

    # ---------- 目录合并辅助 ----------
    def _move_all_contents(self, src_dir: Path, dst_dir: Path):
        for item in list(src_dir.iterdir()):
            target = dst_dir / item.name
            if not target.exists():
                try:
                    shutil.move(str(item), str(target))
                except Exception as e:
                    self.log_signal.emit(f"移动失败 {item.name}: {e}")
            else:
                if item.is_dir() and target.is_dir():
                    self._move_all_contents(item, target)
                else:
                    if self.conflict_policy == 'merge_skip':
                        try:
                            safe_delete(item, use_trash=self.use_trash)
                        except Exception as e:
                            self.log_signal.emit(f"丢弃同名失败 {item.name}: {e}")
                    elif self.conflict_policy == 'merge_rename':
                        new_target = self._rename_target(dst_dir, item.name)
                        try:
                            shutil.move(str(item), str(new_target))
                        except Exception as e:
                            self.log_signal.emit(f"重命名移动失败 {item.name}: {e}")
                    elif self.conflict_policy == 'overwrite':
                        try:
                            if target.is_dir():
                                shutil.rmtree(target)
                            else:
                                target.unlink()
                            shutil.move(str(item), str(target))
                        except Exception as e:
                            self.log_signal.emit(f"覆盖移动失败 {item.name}: {e}")
                    # 'keep' 策略：保留目标，源不动

    def _rename_target(self, dst_dir: Path, filename: str) -> Path:
        stem = Path(filename).stem
        suffix = Path(filename).suffix
        counter = 1
        while True:
            new_name = f"{stem}({counter}){suffix}"
            candidate = dst_dir / new_name
            if not candidate.exists():
                return candidate
            counter += 1

    # ---------- 包装目录折叠（排除初始任务顶层目录） ----------
    def _fold_wrapper_dirs(self, root: Path):
        if not root.exists():
            return

        changed = True
        while changed:
            changed = False
            all_dirs = []
            for dirpath, dirnames, filenames in os.walk(root):
                for d in dirnames:
                    all_dirs.append(Path(dirpath) / d)
            all_dirs.sort(key=lambda p: len(p.parts), reverse=True)

            for dir_path in all_dirs:
                if not dir_path.exists():
                    continue
                if dir_path == self.target_root:
                    continue
                if dir_path in self.initial_top_dirs:
                    continue

                entries = list(dir_path.iterdir())
                if len(entries) != 1 or not entries[0].is_dir():
                    continue
                sub_dir = entries[0]
                if sub_dir in self.initial_top_dirs:
                    continue

                self.log_signal.emit(f"折叠包装目录: {sub_dir.name} -> {dir_path.name}")
                self._move_all_contents(sub_dir, dir_path)

                if not any(sub_dir.iterdir()):
                    try:
                        sub_dir.rmdir()
                    except OSError as e:
                        self.log_signal.emit(f"删除空目录失败 {sub_dir}: {e}")
                        continue
                    changed = True
                else:
                    self.log_signal.emit(f"包装目录折叠后仍有内容，保留: {sub_dir}")

    # ---------- 同名嵌套合并 ----------
    def _flatten_same_name_nested(self, root: Path):
        if not root.exists():
            return

        for child in list(root.iterdir()):
            if child.is_dir() and not child.is_symlink():
                self._flatten_same_name_nested(child)

        changed = True
        while changed:
            changed = False
            entries = list(root.iterdir())
            if len(entries) != 1 or not entries[0].is_dir():
                break
            sub_dir = entries[0]
            if sub_dir.name.lower() != root.name.lower():
                break

            self.log_signal.emit(f"同名嵌套合并: {sub_dir.name} -> {root.name}")
            self._move_all_contents(sub_dir, root)
            if not any(sub_dir.iterdir()):
                sub_dir.rmdir()
                changed = True
            else:
                self.log_signal.emit(f"同名合并后源目录仍有内容，停止: {sub_dir}")
                break

    # ---------- 解压阶段 ----------
    def _flatten_and_recurse(self, current_dir: Path, depth: int):
        if depth > self.max_depth:
            return
        try:
            entries = list(current_dir.iterdir())
        except FileNotFoundError:
            return
        if not entries:
            return

        # 情况1：只有一个文件且是压缩包（或分卷组主文件）
        if len(entries) == 1 and entries[0].is_file():
            fp = entries[0]
            if fp.name.lower().endswith('.zip'):
                vols = find_zip_volume_files(fp)
                is_split = len(vols) > 1
            else:
                vols = [fp]
                is_split = False

            if is_archive(fp) or detect_archive_type_by_magic(fp) or is_split:
                self.log_signal.emit(f"正在解压: {fp.name} -> {current_dir}")
                try:
                    if is_split:
                        self._extract_split_group(fp, current_dir)
                    else:
                        extract_archive_advanced(fp, current_dir, passwords=self.passwords)
                except Exception as e:
                    self.log_signal.emit(f"解压失败 {fp.name}: {redact_secrets(e)}")
                    self.failed_archives[fp.resolve()] = redact_secrets(e)
                    return
                if self.delete_intermediate:
                    self._delete_file_or_split(ArchiveTask(fp, Path('.'), vols))
                self._flatten_and_recurse(current_dir, depth + 1)
                return

        # 情况2：多个条目
        for entry in list(current_dir.iterdir()):
            if entry.is_dir():
                self._flatten_and_recurse(entry, depth + 1)
            elif entry.is_file():
                if is_zip_split_volume(entry):
                    continue
                if is_archive(entry) or detect_archive_type_by_magic(entry):
                    sub_dest = unique_dir(current_dir / archive_base_name(entry))
                    vols = find_zip_volume_files(entry) if entry.name.lower().endswith('.zip') else [entry]
                    # 如果是分卷主文件，构建完整分卷组
                    if is_split_main(entry):
                        vols = find_split_volume_files(entry)
                    tmp_task = ArchiveTask(entry, Path('.'), vols)
                    self._process_task(tmp_task, sub_dest, depth + 1)

    def _extract_split_group(self, main_file: Path, dest_dir: Path):
        """
        分卷组整组解压：先用无密码尝试，失败后按密码表逐个撞库（缺陷 D-3）。

        历史行为：分卷分支硬编码 `password=None`，带密码的分卷包必然失败。
        每次重试前清空目标目录，避免上一次失败留下的半成品与新结果混在一起；
        日志与失败原因都经 redact_secrets 处理，口令不会进入日志/汇总。
        """
        passwords = list(self.passwords or [])
        attempts = [None] + passwords
        last_error = None
        for index, pwd in enumerate(attempts):
            if self._is_cancelled:
                raise RuntimeError("任务已取消")
            if index > 0 and dest_dir.exists():
                shutil.rmtree(dest_dir, ignore_errors=True)
            try:
                extract_with_7z(main_file, dest_dir, password=pwd)
                if index > 0:
                    self.log_signal.emit(
                        f"分卷包密码命中（第 {index}/{len(passwords)} 个）: {main_file.name}")
                return
            except Exception as e:
                last_error = e
                if index == 0:
                    if not passwords:
                        raise
                    self.log_signal.emit(f"分卷包无密码尝试失败，开始按密码表撞库: {main_file.name}")
                else:
                    self.log_signal.emit(
                        f"分卷包密码尝试 {index}/{len(passwords)} 失败: {main_file.name}")
        raise RuntimeError(
            f"分卷包解压失败（已尝试无密码 + {len(passwords)} 个密码）: {redact_secrets(last_error)}")

    def _prepare_top_dir(self, base: Path):
        """
        决定本次任务的落点，并让"目标已存在时"的冲突策略真正生效（P-003，2026-09-27）。

        返回 (最终目录, 暂存目录)：
        · 目标不存在             -> (base, None)，直接解压到 base
        · 目标存在且选“保留原目录”  -> (unique_dir(base), None)，即原来的 `data(1)` 行为
        · 目标存在且选合并/覆盖    -> (base, staging)：先解压到暂存目录，再按策略并入 base

        必须走暂存目录：解压器自己会直接覆盖同名文件，若直接解压进已存在的目录，
        "同名保留旧的/自动改名" 这两种策略就失去意义了。
        """
        if not base.exists():
            return base, None
        if not base.is_dir():
            return unique_dir(base), None
        if self.conflict_policy == 'keep':
            self.log_signal.emit(f"目标已存在，另建副本: {base.name}")
            return unique_dir(base), None
        staging = base.with_name(base.name + '.__incoming__')
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        label = CONFLICT_LABELS.get(self.conflict_policy, self.conflict_policy)
        self.log_signal.emit(f"目标已存在，按“{label}”并入: {base.name}")
        return base, staging

    def _collapse_self_named_child(self, dest: Path):
        """
        若 `dest` 里唯一的子目录与 `dest` 同名，把它的内容提上来。

        规则与整理阶段的"同名嵌套合并"完全一致，只是提前到并入之前执行：
        同一次运行里两个同名包时，第一个包的产物还没被整理过（还是 `data/data/`），
        若不先折叠，第二个包并入时就"看不见"真正的 `data/`，冲突策略便不会触发。
        """
        try:
            items = list(dest.iterdir())
        except OSError:
            return
        if len(items) != 1 or not items[0].is_dir():
            return
        sub = items[0]
        if sub.name.lower() != dest.name.lower():
            return
        self._move_all_contents(sub, dest)
        if not any(sub.iterdir()):
            try:
                sub.rmdir()
            except OSError:
                pass

    def _finish_staging(self, staging: Path, dest: Path):
        """
        把暂存目录的内容按冲突策略并入已存在的目标目录，然后清掉暂存目录。

        若暂存目录里只有一个“与目标同名”的子目录（`data.zip` 解出同名子目录 `data` 的情形），
        先剥掉这一层再并入——这与正常路径上"同名嵌套合并"的结果保持一致，
        否则会变成 `data/data/`。
        """
        try:
            if staging.exists():
                self._collapse_self_named_child(dest)
                items = list(staging.iterdir())
                src = staging
                if len(items) == 1 and items[0].is_dir() \
                        and items[0].name.lower() == dest.name.lower():
                    src = items[0]
                if any(src.iterdir()):
                    self._move_all_contents(src, dest)
        except Exception as e:
            self.log_signal.emit(f"并入目标目录失败 {dest.name}: {e}")
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _strip_top_dir_suffix(self, dest: Path) -> Path:
        """
        解压完成后清理本次任务的顶层目录（D-5）：

        只去掉尾部多余的后缀：`AZA788666.rar` -> `AZA788666`（伪装后缀与压缩包后缀都不需要）。
        **不动目录内部的层级**——`保留原目录` 另建的 `data(1)` 里保持原有嵌套（用户 2026-09-27 明确）。

        改名后必须同步更新 `initial_top_dirs`，否则整理阶段会把这层目录当包装目录折叠掉。

        `AZA788666.rar.jpg` 这类伪装包解出来是 `AZA788666.rar`——伪装后缀与压缩包
        后缀都不需要，所以直接改顶层目录名。改名后必须同步更新 `initial_top_dirs`，
        否则整理阶段会把这层目录也当成包装目录折叠掉。
        """
        if not dest.exists() or not dest.is_dir():
            return dest
        # 1) 去掉尾部多余后缀
        nice = strip_junk_suffix(dest.name)
        if nice and nice != dest.name:
            target = unique_dir(dest.with_name(nice))
            try:
                shutil.move(str(dest), str(target))
                self.log_signal.emit(f"顶层目录改名: {dest.name} -> {target.name}")
                self.initial_top_dirs.discard(dest)
                self.initial_top_dirs.add(target)
                dest = target
            except Exception as e:
                self.log_signal.emit(f"顶层目录改名失败 {dest.name}: {e}")
        return dest

    def _delete_file_or_split(self, task: ArchiveTask):
        if task.is_split:
            for vol in task.volumes:
                if vol.exists():
                    try:
                        safe_delete(vol, use_trash=self.use_trash)
                        self.log_signal.emit(f"已删除分卷文件: {vol.name}")
                    except Exception as e:
                        self.log_signal.emit(f"删除分卷文件失败 {vol.name}: {e}")
        else:
            if task.main_file.exists():
                try:
                    safe_delete(task.main_file, use_trash=self.use_trash)
                    self.log_signal.emit(f"已删除中间文件: {task.main_file.name}")
                except Exception as e:
                    self.log_signal.emit(f"删除中间文件失败 {task.main_file.name}: {e}")

    def _process_task(self, task: ArchiveTask, dest_dir: Path, depth: int):
        if self._is_cancelled or depth > self.max_depth:
            return
        key = task.main_file.resolve()
        if key in self.seen_archives:
            return
        self.seen_archives.add(key)

        try:
            self.log_signal.emit(f"正在解压: {task.main_file.name} -> {dest_dir}")
            if task.is_split:
                self._extract_split_group(task.main_file, dest_dir)
            else:
                extract_archive_advanced(task.main_file, dest_dir, passwords=self.passwords)

            if not dest_dir.exists() or not any(dest_dir.iterdir()):
                self.log_signal.emit(f"解压结果为空目录: {task.main_file.name}")
                try:
                    if dest_dir.exists():
                        dest_dir.rmdir()
                except OSError:
                    pass
                self.failed_archives[key] = "解压结果为空目录"
                return

            if self.delete_intermediate and depth > 0:
                self._delete_file_or_split(task)

            self._flatten_and_recurse(dest_dir, depth)

        except Exception as e:
            self.log_signal.emit(f"处理异常: {redact_secrets(e)}")
            self.failed_archives[key] = redact_secrets(e)

    def run(self):
        total = len(self.jobs)
        self.initial_top_dirs.clear()
        try:
            self.log_signal.emit(f"开始解压任务，共 {total} 个")
            for index, (task, row) in enumerate(self.jobs, 1):
                if self._is_cancelled:
                    self.log_signal.emit("任务已取消")
                    for remaining_task, remaining_row in self.jobs[index-1:]:
                        self.status_signal.emit(remaining_row, "已取消")
                    break

                base = self.target_root / task.relative_parent / archive_base_name(task.main_file)
                dest, staging = self._prepare_top_dir(base)
                self.initial_top_dirs.add(dest)
                self.log_signal.emit(f"处理初始任务: {task.main_file.name}")

                self._process_task(task, staging if staging is not None else dest, depth=0)
                if staging is not None:
                    self._finish_staging(staging, dest)
                dest = self._strip_top_dir_suffix(dest)

                if task.main_file.resolve() in self.failed_archives:
                    self.status_signal.emit(row, "失败")
                else:
                    self.status_signal.emit(row, "成功")

                self.progress_signal.emit(int(index / total * 100))
                self.log_signal.emit(f"已完成 {index}/{total} 个初始任务")

            # ===== 所有解压完成后，开始目录整理 =====
            if not self._is_cancelled:
                self.log_signal.emit("开始折叠包装目录...")
                self._fold_wrapper_dirs(self.target_root)
                self.log_signal.emit("开始同名嵌套合并...")
                self._flatten_same_name_nested(self.target_root)

            success_count = sum(1 for task, _ in self.jobs
                                if task.main_file.resolve() not in self.failed_archives)
            self.summary_signal.emit({
                'total': total,
                'success': success_count,
                'failed': len(self.failed_archives),
                'details': {str(k): v for k, v in self.failed_archives.items()}
            })
        except Exception as e:
            self.log_signal.emit(f"工作线程发生严重异常: {e}")
            for task, row in self.jobs:
                if task.main_file.resolve() not in self.failed_archives:
                    self.status_signal.emit(row, "失败")
            self.summary_signal.emit({
                'total': total,
                'success': 0,
                'failed': total,
                'details': {'worker_error': str(e)}
            })
        finally:
            self.progress_signal.emit(100)
            self.finished_signal.emit()

# ---------- 主窗口 ----------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("批量解压工具 v2.1.1 - 深色主题")
        self.setMinimumSize(1200, 850)
        self.tasks = []
        self.row_to_task = {}
        self.worker = None
        self.setAcceptDrops(True)

        self.passwords = self.load_passwords()
        self.config = self.load_config()

        self.apply_dark_theme()

        central = QWidget()
        layout = QVBoxLayout(central)

        # 目标文件夹
        target_layout = QHBoxLayout()
        target_layout.addWidget(QLabel("目标文件夹:"))
        self.target_edit = QLineEdit(self.config.get('target_dir', str(Path.home() / "Downloads")))
        target_layout.addWidget(self.target_edit)
        browse_btn = QPushButton("浏览...")
        browse_btn.clicked.connect(self.browse_target)
        target_layout.addWidget(browse_btn)
        open_btn = QPushButton("打开")
        open_btn.clicked.connect(self.open_target)
        target_layout.addWidget(open_btn)
        layout.addLayout(target_layout)

        # 添加按钮、密码管理、目录策略、递归模式、冲突策略
        add_layout = QHBoxLayout()
        self.add_file_btn = QPushButton("添加文件")
        self.add_file_btn.clicked.connect(self.add_files_dialog)
        self.add_folder_btn = QPushButton("添加文件夹")
        self.add_folder_btn.clicked.connect(self.add_folder_dialog)
        self.password_btn = QPushButton("密码管理")
        self.password_btn.clicked.connect(self.manage_passwords)

        add_layout.addWidget(self.add_file_btn)
        add_layout.addWidget(self.add_folder_btn)
        add_layout.addWidget(self.password_btn)
        add_layout.addStretch()

        add_layout.addWidget(QLabel("目录策略:"))
        self.strategy_combo = QComboBox()
        self.strategy_combo.addItem("保留源文件夹名")
        self.strategy_combo.addItem("不保留源文件夹名")
        self.strategy_combo.setCurrentIndex(1)
        add_layout.addWidget(self.strategy_combo)

        add_layout.addWidget(QLabel("目标已存在时:"))
        self.conflict_combo = QComboBox()
        for key in CONFLICT_ORDER:
            self.conflict_combo.addItem(CONFLICT_LABELS[key])
        add_layout.addWidget(self.conflict_combo)

        layout.addLayout(add_layout)

        # 选项复选框（只保留真实生效的两项）
        option_layout = QHBoxLayout()
        self.delete_intermediate_checkbox = QCheckBox("删除中间层压缩包文件")
        self.delete_intermediate_checkbox.setChecked(True)
        self.use_trash_checkbox = QCheckBox("删除文件时移入回收站")
        self.use_trash_checkbox.setChecked(True)
        option_layout.addWidget(self.delete_intermediate_checkbox)
        option_layout.addWidget(self.use_trash_checkbox)
        option_layout.addStretch()
        layout.addLayout(option_layout)

        # 表格
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["文件名", "原始路径", "输出子目录", "状态", "备注"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)
        layout.addWidget(self.table)

        # 进度条
        self.progress = QProgressBar()
        layout.addWidget(self.progress)

        # 日志
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(150)
        layout.addWidget(self.log_text)

        # 底部按钮
        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("开始解压")
        self.start_btn.clicked.connect(self.start_extract)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_extract)
        self.clear_btn = QPushButton("清空列表")
        self.clear_btn.clicked.connect(self.clear_queue)
        self.remove_done_btn = QPushButton("移除已完成")
        self.remove_done_btn.clicked.connect(self.remove_finished)
        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.cancel_btn)
        btn_layout.addWidget(self.clear_btn)
        btn_layout.addWidget(self.remove_done_btn)
        layout.addLayout(btn_layout)

        self.setCentralWidget(central)

    # ---------- 配置管理 ----------
    def config_path(self):
        app_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
        return app_dir / "app_config.json"

    def load_config(self):
        try:
            with open(self.config_path(), 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def save_config(self):
        config = {
            'target_dir': self.target_edit.text().strip(),
            'last_add_file_dir': self.config.get('last_add_file_dir', ''),
            'last_add_folder_dir': self.config.get('last_add_folder_dir', ''),
            'strategy': self.strategy_combo.currentIndex(),
            'conflict_policy': self.conflict_combo.currentIndex(),
            'use_trash': self.use_trash_checkbox.isChecked()
        }
        with open(self.config_path(), 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

    def closeEvent(self, event):
        self.save_config()
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(2000)
        event.accept()

    # ---------- 密码管理 ----------
    def load_passwords(self):
        try:
            app_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
            file_path = app_dir / "passwords.json"
            if file_path.exists():
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list) and data:
                        return [decrypt_text(item) for item in data]
        except Exception:
            pass
        return []

    def save_passwords(self):
        try:
            app_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
            file_path = app_dir / "passwords.json"
            encrypted = [encrypt_text(pwd) for pwd in self.passwords]
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(encrypted, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"保存密码失败: {e}")

    def manage_passwords(self):
        dialog = PasswordManagerDialog(self.passwords, self)
        if dialog.exec() == QDialog.Accepted:
            self.passwords = dialog.get_passwords()
            self.save_passwords()
            self.log(f"密码列表已更新（当前 {len(self.passwords)} 条，加密存储）")

    # ---------- 深色主题 ----------
    def apply_dark_theme(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #2b2b2b; }
            QWidget { color: #dddddd; font-size: 10pt; }
            QLabel { color: #cccccc; }
            QLineEdit, QTextEdit, QTableWidget, QListWidget, QComboBox {
                background-color: #3c3c3c;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 4px;
                color: #eeeeee;
            }
            QLineEdit:focus, QTextEdit:focus { border-color: #4CAF50; }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                padding: 6px 14px;
                border-radius: 4px;
                font-weight: bold;
            }
            QPushButton:hover { background-color: #45a049; }
            QPushButton:disabled { background-color: #666; color: #aaa; }
            QPushButton#danger { background-color: #d32f2f; }
            QPushButton#danger:hover { background-color: #b71c1c; }
            QPushButton#secondary { background-color: #555; }
            QPushButton#secondary:hover { background-color: #666; }
            QProgressBar {
                border: 1px solid #555;
                border-radius: 4px;
                text-align: center;
                background-color: #3c3c3c;
                color: #eee;
            }
            QProgressBar::chunk { background-color: #4CAF50; border-radius: 3px; }
            QTableWidget {
                gridline-color: #555;
                selection-background-color: #4CAF50;
                alternate-background-color: #353535;
            }
            QHeaderView::section {
                background-color: #444;
                padding: 4px;
                border: none;
                border-bottom: 1px solid #555;
                font-weight: bold;
                color: #eee;
            }
            QCheckBox { color: #ddd; spacing: 5px; }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border: 1px solid #777;
                background-color: #3c3c3c;
            }
            QCheckBox::indicator:checked { background-color: #4CAF50; }
            QMenu {
                background-color: #3c3c3c;
                color: #eee;
                border: 1px solid #555;
            }
            QMenu::item:selected { background-color: #4CAF50; }
            QComboBox QAbstractItemView {
                background-color: #3c3c3c;
                color: #eee;
                selection-background-color: #4CAF50;
            }
        """)

    def get_icon(self, filename, fallback_pixmap):
        try:
            app_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
            icon_path = app_dir / "icons" / filename
            if icon_path.exists():
                return QIcon(str(icon_path))
        except Exception:
            pass
        return self.style().standardIcon(fallback_pixmap)

    # ---------- 界面逻辑 ----------
    def browse_target(self):
        start_dir = self.target_edit.text().strip() or self.config.get('target_dir', str(Path.home() / "Downloads"))
        directory = QFileDialog.getExistingDirectory(self, "选择目标文件夹", start_dir)
        if directory:
            self.target_edit.setText(directory)
            self.save_config()
            self.log(f"目标文件夹已设置为: {directory}")

    def open_target(self):
        target = self.target_edit.text().strip()
        if Path(target).exists():
            os.startfile(target) if os.name == 'nt' else subprocess.Popen(['xdg-open', target])
        else:
            QMessageBox.warning(self, "提示", "目标文件夹不存在")

    def add_files_dialog(self):
        start_dir = self.config.get('last_add_file_dir', '')
        if not start_dir or not Path(start_dir).exists():
            start_dir = self.target_edit.text().strip()
        if not start_dir or not Path(start_dir).exists():
            start_dir = str(Path.home() / "Downloads")

        files, _ = QFileDialog.getOpenFileNames(
            self, "选择压缩包文件", start_dir,
            "压缩包 (*.zip *.7z *.rar *.tar *.tar.gz *.tgz *.bz2 *.xz *.001 *.part1.rar);;所有文件 (*)"
        )
        if files:
            self.config['last_add_file_dir'] = str(Path(files[-1]).parent)
            self.save_config()
        for f in files:
            self.add_single_file(Path(f))

    def add_folder_dialog(self):
        start_dir = self.config.get('last_add_folder_dir', '')
        if not start_dir or not Path(start_dir).exists():
            start_dir = self.target_edit.text().strip()
        if not start_dir or not Path(start_dir).exists():
            start_dir = str(Path.home() / "Downloads")

        dialog = QFileDialog(self)
        dialog.setFileMode(QFileDialog.Directory)
        dialog.setOption(QFileDialog.ShowDirsOnly, True)
        dialog.setOption(QFileDialog.DontUseNativeDialog, True)
        dialog.setWindowTitle("选择文件夹（支持 Ctrl/Shift 多选）")
        dialog.setDirectory(start_dir)
        for view in dialog.findChildren(QListView):
            view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        for view in dialog.findChildren(QTreeView):
            view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        if dialog.exec():
            folders = dialog.selectedFiles()
            if folders:
                self.config['last_add_folder_dir'] = str(Path(folders[-1]).parent)
                self.save_config()
            for f in folders:
                if Path(f).is_dir():
                    self.add_folder(Path(f))

    def add_single_file(self, file_path: Path):
        if is_zip_split_volume(file_path):
            self.log(f"跳过旧式 ZIP 分卷后续卷: {file_path.name}（请选择主文件 .zip）")
            return
        if is_split_volume(file_path):
            self.log(f"跳过分卷后续卷: {file_path.name}（请选择第一个分卷，如 .part1.rar）")
            return
        if not is_archive(file_path):
            magic_type = detect_archive_type_by_magic(file_path)
            if magic_type:
                self.log(f"检测到伪装压缩包: {file_path.name}")
            else:
                self.log(f"跳过非压缩包文件: {file_path.name}")
                return

        # 检测分卷组
        vols = []
        if file_path.name.lower().endswith('.zip'):
            vols = find_zip_volume_files(file_path)
        elif is_split_main(file_path):
            vols = find_split_volume_files(file_path)
        if not vols:
            vols = [file_path]

        task = ArchiveTask(file_path, Path('.'), vols)
        self._add_task(task)

    def add_folder(self, folder_path: Path):
        tasks = scan_archives_recursive(folder_path, folder_path)
        if not tasks:
            self.log(f"文件夹中未发现压缩包: {folder_path}")
            return
        strategy_index = self.strategy_combo.currentIndex()
        for task in tasks:
            if strategy_index == 0:  # 策略A保留源结构
                task.relative_parent = Path(folder_path.name) / task.relative_parent
            self._add_task(task)

    def _add_task(self, task: ArchiveTask):
        for existing in self.tasks:
            if existing.main_file.resolve() == task.main_file.resolve():
                self.log(f"已存在，跳过: {task.main_file.name}")
                return
        self.tasks.append(task)
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(task.main_file.name))
        self.table.setItem(row, 1, QTableWidgetItem(str(task.main_file.parent)))
        output_subdir = (task.relative_parent / strip_junk_suffix(archive_base_name(task.main_file))).as_posix()
        self.table.setItem(row, 2, QTableWidgetItem(output_subdir))
        self.table.setItem(row, 3, QTableWidgetItem("等待"))
        self.table.setItem(row, 4, QTableWidgetItem(""))
        self.row_to_task[row] = task
        self.log(f"添加: {task.main_file.name}")

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if not path:
                continue
            p = Path(path)
            if p.is_dir():
                self.add_folder(p)
            elif p.is_file():
                self.add_single_file(p)
        event.acceptProposedAction()

    def show_context_menu(self, pos):
        menu = QMenu()
        remove_action = QAction("移除选中项", self)
        remove_action.triggered.connect(self.remove_selected)
        menu.addAction(remove_action)
        clear_done_action = QAction("移除已完成项", self)
        clear_done_action.triggered.connect(self.remove_finished)
        menu.addAction(clear_done_action)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def remove_selected(self):
        rows = sorted(set(index.row() for index in self.table.selectedIndexes()), reverse=True)
        for row in rows:
            self.table.removeRow(row)
            if row in self.row_to_task:
                task = self.row_to_task.pop(row)
                if task in self.tasks:
                    self.tasks.remove(task)
        self._rebuild_row_mapping()
        if rows:
            self.log(f"已移除 {len(rows)} 个项目")

    def remove_finished(self):
        rows_to_remove = []
        for row in range(self.table.rowCount()):
            status_item = self.table.item(row, 3)
            if status_item and status_item.text() in ("成功", "失败", "已取消"):
                rows_to_remove.append(row)
        for row in reversed(rows_to_remove):
            self.table.removeRow(row)
            if row in self.row_to_task:
                task = self.row_to_task.pop(row)
                if task in self.tasks:
                    self.tasks.remove(task)
        self._rebuild_row_mapping()
        if rows_to_remove:
            self.log(f"已移除 {len(rows_to_remove)} 个已完成项目")

    def _rebuild_row_mapping(self):
        self.row_to_task.clear()
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 0)
            path_item = self.table.item(row, 1)
            if name_item and path_item:
                archive_path = Path(path_item.text()) / name_item.text()
                for task in self.tasks:
                    if task.main_file == archive_path:
                        self.row_to_task[row] = task
                        break

    def start_extract(self):
        if self.worker and self.worker.isRunning():
            self.log("已有解压任务在进行中")
            return
        if not self.tasks:
            QMessageBox.warning(self, "提示", "请先添加压缩包")
            return
        target = self.target_edit.text().strip()
        if not Path(target).exists():
            QMessageBox.warning(self, "提示", "目标文件夹不存在，请重新选择")
            return

        self.save_config()

        delete_intermediate = self.delete_intermediate_checkbox.isChecked()
        use_trash = self.use_trash_checkbox.isChecked()
        strategy_index = self.strategy_combo.currentIndex()
        strategy = 'A' if strategy_index == 0 else 'B'
        conflict_policy = CONFLICT_ORDER[self.conflict_combo.currentIndex()]

        extract_queue = []
        for row, task in self.row_to_task.items():
            if task in self.tasks:
                extract_queue.append((task, row))

        if not extract_queue:
            QMessageBox.warning(self, "提示", "队列为空")
            return

        self.log("正在创建解压线程...")
        try:
            self.worker = ExtractWorker(
                extract_queue, target, self.passwords,
                strategy=strategy,
                delete_intermediate=delete_intermediate,
                conflict_policy=conflict_policy,
                use_trash=use_trash
            )
        except Exception as e:
            self.log(f"创建解压线程失败: {e}")
            QMessageBox.critical(self, "错误", f"创建解压线程失败:\n{e}")
            return

        self.worker.log_signal.connect(self.log)
        self.worker.status_signal.connect(self.update_status)
        self.worker.progress_signal.connect(self.progress.setValue)
        self.worker.summary_signal.connect(self.show_summary)
        self.worker.finished_signal.connect(self.on_worker_finished)
        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.log("解压线程已启动")
        self.worker.start()

    def update_status(self, row, status):
        if row >= 0 and row < self.table.rowCount():
            item = self.table.item(row, 3)
            if item:
                item.setText(status)

    def show_summary(self, summary):
        msg = f"解压完成\n成功: {summary['success']}\n失败: {summary['failed']}\n\n失败详情:\n"
        for path, reason in summary['details'].items():
            msg += f"- {Path(path).name}: {reason}\n"
        QMessageBox.information(self, "处理结果", msg)

    def cancel_extract(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.log("正在取消...")
            self.cancel_btn.setEnabled(False)

    def on_worker_finished(self):
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.worker = None
        self.log("所有任务处理完毕")

    def clear_queue(self):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "提示", "请先取消当前解压任务")
            return
        self.tasks.clear()
        self.table.setRowCount(0)
        self.row_to_task.clear()
        self.progress.setValue(0)
        self.log("已清空列表")

    def log(self, message):
        self.log_text.append(message)

if __name__ == "__main__":
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setStyle(QStyleFactory.create("Fusion"))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())