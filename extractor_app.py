"""
批量解压工具 v15.0
新增：配置记忆（目标目录、添加文件/文件夹初始目录）
其余功能与 v14.2 相同。
"""

import sys
import os
import shutil
import subprocess
import zipfile
import tarfile
import json
from pathlib import Path
import re

from PySide6.QtCore import Qt, QThread, Signal, QDir
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
    name = path.name.lower()
    m = re.search(r'\.part(\d+)\.(rar|zip|7z)$', name)
    if m and int(m.group(1)) > 1:
        return True
    m = re.search(r'\.(zip|7z|rar)\.(\d{3})$', name)
    if m and int(m.group(2)) > 1:
        return True
    return False

def is_split_main(path: Path) -> bool:
    name = path.name.lower()
    m = re.search(r'\.part1\.(rar|zip|7z)$', name)
    if m:
        return True
    m = re.search(r'\.(zip|7z|rar)\.001$', name)
    if m:
        return True
    return False

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

# ---------- 任务模型 ----------
class ExtractJob:
    def __init__(self, archive_path: Path, relative_parent: Path = Path('.'), is_intermediate=False):
        self.archive_path = archive_path
        self.relative_parent = relative_parent
        self.is_intermediate = is_intermediate

# ---------- 扫描函数（模块级） ----------
def scan_archives_in_folder(folder_path: Path):
    jobs = []
    base = folder_path.resolve()
    for root, dirs, files in os.walk(base):
        root_path = Path(root)
        for f in files:
            fp = root_path / f
            if is_archive(fp) and not is_split_volume(fp):
                rel_parent = root_path.relative_to(base)
                jobs.append(ExtractJob(fp, rel_parent, is_intermediate=False))
    return jobs

def scan_archives_with_magic(folder_path: Path, base_for_rel: Path):
    jobs = []
    try:
        entries = list(folder_path.iterdir())
    except Exception:
        return jobs
    base_resolved = Path(base_for_rel).resolve()
    for entry in entries:
        if entry.is_file():
            fp = entry
            if is_archive(fp) and not is_split_volume(fp):
                rel_parent = entry.parent.relative_to(base_resolved)
                jobs.append(ExtractJob(fp, rel_parent, is_intermediate=False))
                continue
            magic_type = detect_archive_type_by_magic(fp)
            if magic_type and not is_split_volume(fp):
                rel_parent = entry.parent.relative_to(base_resolved)
                job = ExtractJob(fp, rel_parent, is_intermediate=False)
                job.magic_type = magic_type
                job.is_disguised = True
                jobs.append(job)
    return jobs

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
        text, ok = QInputDialog.getText(self, "添加密码", "请输入密码（明文存储）:")
        if ok and text.strip():
            self.list_widget.addItem(text.strip())

    def remove_password(self):
        for item in self.list_widget.selectedItems():
            self.list_widget.takeItem(self.list_widget.row(item))

    def get_passwords(self):
        return [self.list_widget.item(i).text() for i in range(self.list_widget.count())]

# ---------- 后台工作线程 ----------
class ExtractWorker(QThread):
    log_signal = Signal(str)
    status_signal = Signal(int, str)
    progress_signal = Signal(int)
    finished_signal = Signal()

    def __init__(self, jobs, target_root, passwords, strategy='B',
                 recursive=False, smart_flatten=True, delete_intermediate=True,
                 global_flatten=True, max_depth=10, parent=None):
        super().__init__(parent)
        self.jobs = jobs
        self.target_root = target_root
        self.passwords = passwords
        self.strategy = strategy
        self.recursive = recursive
        self.smart_flatten = (strategy == 'B') and smart_flatten
        self.delete_intermediate = delete_intermediate
        self.global_flatten = (strategy == 'B') and global_flatten
        self.max_depth = max_depth
        self._is_cancelled = False
        self.seen_archives = set()
        self.force_try_files = set()
        self.failed_archives = set()

    def cancel(self):
        self._is_cancelled = True

    # ---------- 目录操作核心 ----------
    def _move_content(self, src_dir: Path, dst_dir: Path):
        if not src_dir.exists():
            return
        dst_dir.mkdir(parents=True, exist_ok=True)

        for item in list(src_dir.iterdir()):
            target = dst_dir / item.name
            try:
                if not target.exists():
                    shutil.move(str(item), str(target))
                    self.log_signal.emit(f"移动: {item.name}")
                else:
                    if item.is_dir() and target.is_dir():
                        self._move_content(item, target)
                    else:
                        self.log_signal.emit(f"冲突跳过（保留目标）: {item.name}")
            except Exception as e:
                self.log_signal.emit(f"移动失败 {item.name}: {e}")

        try:
            if not any(src_dir.iterdir()):
                src_dir.rmdir()
                self.log_signal.emit(f"已删除空目录: {src_dir}")
            else:
                self.log_signal.emit(f"警告：源目录未清空，保留: {src_dir}")
        except OSError as e:
            self.log_signal.emit(f"删除源目录失败 {src_dir}: {e}")

    def _smart_flatten_inner(self, dir_path: Path):
        if not dir_path.exists():
            return
        changed = True
        while changed:
            changed = False
            try:
                entries = list(dir_path.iterdir())
            except Exception as e:
                self.log_signal.emit(f"读取目录失败 {dir_path}: {e}")
                return
            if len(entries) == 1 and entries[0].is_dir():
                sub_dir = entries[0]
                self._move_content(sub_dir, dir_path)
                if not sub_dir.exists():
                    changed = True
                else:
                    break

    def _smart_flatten_outer(self, dest_dir: Path):
        try:
            entries = list(dest_dir.iterdir())
        except Exception as e:
            self.log_signal.emit(f"读取目录失败 {dest_dir}: {e}")
            return dest_dir

        if len(entries) == 1 and entries[0].is_dir():
            sub_dir = entries[0]
            parent = dest_dir.parent
            target = parent / sub_dir.name

            self.log_signal.emit(f"智能提升：{sub_dir.name} -> {target}")

            try:
                if not target.exists():
                    shutil.move(str(sub_dir), str(target))
                    self.log_signal.emit(f"已移动文件夹: {sub_dir.name}")
                else:
                    self._move_content(sub_dir, target)

                if not any(dest_dir.iterdir()):
                    dest_dir.rmdir()
                    self.log_signal.emit(f"已删除空目录: {dest_dir}")
                else:
                    self.log_signal.emit(f"dest_dir 未清空，保留: {dest_dir}")

                self._smart_flatten_inner(target)
                return target
            except Exception as e:
                self.log_signal.emit(f"智能提升失败: {e}")
                return dest_dir
        else:
            self._smart_flatten_inner(dest_dir)
            return dest_dir

    def _promote_if_single_child(self, dir_path: Path, stop_dir: Path = None):
        if not dir_path.exists():
            return None
        if stop_dir is not None and dir_path.resolve() == stop_dir.resolve():
            return None
        try:
            entries = list(dir_path.iterdir())
        except Exception as e:
            self.log_signal.emit(f"读取目录失败 {dir_path}: {e}")
            return None
        if len(entries) == 1 and entries[0].is_dir():
            sub_dir = entries[0]
            parent = dir_path.parent
            target = parent / sub_dir.name
            self.log_signal.emit(f"目录折叠：{dir_path.name} -> {target}")
            try:
                if not target.exists():
                    shutil.move(str(sub_dir), str(target))
                else:
                    self._move_content(sub_dir, target)
                if not any(dir_path.iterdir()):
                    dir_path.rmdir()
                    self.log_signal.emit(f"已删除空目录: {dir_path}")
                    return parent
                else:
                    self.log_signal.emit(f"目录未清空: {dir_path}")
                    return None
            except Exception as e:
                self.log_signal.emit(f"提升失败: {e}")
                return None
        return None

    def _global_flatten_single_child_dirs(self, root: Path):
        if not root.exists():
            return
        try:
            for child in list(root.iterdir()):
                if child.is_symlink():
                    continue
                if child.is_dir():
                    self._global_flatten_single_child_dirs(child)
            for child in list(root.iterdir()):
                if child.is_dir() and not child.is_symlink():
                    self._promote_if_single_child(child, stop_dir=root)
        except Exception as e:
            self.log_signal.emit(f"全局去嵌套失败: {e}")

    def _should_continue_recursion(self, dir_path: Path) -> bool:
        if not dir_path.exists():
            return False
        entries = list(dir_path.iterdir())
        if not entries:
            return False
        if len(entries) == 1 and entries[0].is_file():
            return True
        for entry in entries:
            if entry.is_dir():
                return False
            if not (is_archive(entry) or detect_archive_type_by_magic(entry)):
                return False
        return True

    def _scan_archives_with_magic(self, folder_path: Path, base_for_rel: Path):
        jobs = []
        try:
            entries = list(folder_path.iterdir())
        except Exception:
            return jobs
        base_resolved = Path(base_for_rel).resolve()
        if len(entries) == 1 and entries[0].is_file():
            fp = entries[0]
            rel_parent = fp.parent.relative_to(base_resolved)
            job = ExtractJob(fp, rel_parent, is_intermediate=True)
            job.is_disguised = True
            jobs.append(job)
            self.force_try_files.add(fp.resolve())
            return jobs
        for entry in entries:
            if entry.is_file():
                fp = entry
                if is_archive(fp) and not is_split_volume(fp):
                    rel_parent = entry.parent.relative_to(base_resolved)
                    jobs.append(ExtractJob(fp, rel_parent, is_intermediate=True))
                    continue
                magic_type = detect_archive_type_by_magic(fp)
                if magic_type and not is_split_volume(fp):
                    rel_parent = entry.parent.relative_to(base_resolved)
                    job = ExtractJob(fp, rel_parent, is_intermediate=True)
                    job.magic_type = magic_type
                    job.is_disguised = True
                    jobs.append(job)
        return jobs

    def _prepare_split_archive(self, archive_path: Path, magic_type: str):
        parent = archive_path.parent
        stem = archive_path.name
        m = re.match(r'(.+\.part)(\d+)\..+$', stem, re.IGNORECASE)
        if not m:
            return archive_path, None
        base = m.group(1)
        index = m.group(2)
        pattern = re.compile(re.escape(base) + r'(\d+)\..+', re.IGNORECASE)
        related_files = []
        for f in parent.iterdir():
            if f.is_file():
                fm = pattern.match(f.name)
                if fm:
                    mt = detect_archive_type_by_magic(f)
                    if mt:
                        related_files.append((f, fm.group(1), mt))
        if not related_files:
            return archive_path, None

        first_magic = related_files[0][2]
        ext_map = {
            'zip': '.zip',
            'rar': '.rar',
            '7z': '.7z',
            'gzip': '.gz',
            'bzip2': '.bz2',
            'xz': '.xz',
            'tar': '.tar'
        }
        ext = ext_map.get(first_magic, '.zip')

        main_part_path = None
        for file, idx, mt in related_files:
            new_name = f"{base}{idx}{ext}"
            new_path = file.parent / new_name
            if new_path.exists() and new_path != file:
                self.log_signal.emit(f"目标文件名已存在，跳过重命名: {new_name}")
                continue
            try:
                file.rename(new_path)
                self.log_signal.emit(f"已重命名: {file.name} -> {new_name}")
            except Exception as e:
                self.log_signal.emit(f"重命名失败 {file.name}: {e}")
                continue
            if idx == '1':
                main_part_path = new_path
        if main_part_path is None and related_files:
            first_file, first_idx, _ = related_files[0]
            new_name = f"{base}{first_idx}{ext}"
            candidate = first_file.parent / new_name
            if candidate.exists():
                main_part_path = candidate
        return main_part_path, None

    def _process_archive(self, archive_path: Path, dest_dir: Path, depth: int):
        if self._is_cancelled or depth > self.max_depth:
            return
        abs_path = archive_path.resolve()
        if abs_path in self.seen_archives:
            return
        self.seen_archives.add(abs_path)

        try:
            actual_archive_path = archive_path
            magic_type = detect_archive_type_by_magic(archive_path)
            force_try = abs_path in self.force_try_files

            if not is_archive(archive_path) and (magic_type or force_try):
                if '.part' in archive_path.name.lower():
                    actual_archive_path, _ = self._prepare_split_archive(archive_path, magic_type)
                    if actual_archive_path is None:
                        self.log_signal.emit(f"无法处理分包文件: {archive_path.name}")
                        self.failed_archives.add(abs_path)
                        return
            self.log_signal.emit(f"正在解压: {archive_path.name} -> {dest_dir}")
            extract_archive_advanced(actual_archive_path, dest_dir, passwords=self.passwords)

            if not dest_dir.exists() or not any(dest_dir.iterdir()):
                self.log_signal.emit(f"解压结果为空目录: {archive_path.name}")
                try:
                    if dest_dir.exists():
                        dest_dir.rmdir()
                except OSError:
                    pass
                self.failed_archives.add(abs_path)
                return

            actual_content_dir = dest_dir
            if self.smart_flatten:
                actual_content_dir = self._smart_flatten_outer(dest_dir)

            continue_recursion = self._should_continue_recursion(actual_content_dir)

            if self.recursive and continue_recursion:
                new_jobs = self._scan_archives_with_magic(actual_content_dir, actual_content_dir)
                for job in new_jobs:
                    if job.archive_path.resolve() in self.seen_archives:
                        continue
                    job.is_intermediate = True
                    rel_parent = job.archive_path.parent.relative_to(actual_content_dir)
                    new_dest = unique_dir(actual_content_dir / rel_parent / job.archive_path.stem)
                    self._process_archive(job.archive_path, new_dest, depth + 1)

            if self.delete_intermediate and depth > 0:
                try:
                    if archive_path.exists():
                        archive_path.unlink()
                        self.log_signal.emit(f"已删除中间文件: {archive_path.name}")
                except Exception as e:
                    self.log_signal.emit(f"删除中间文件失败 {archive_path.name}: {e}")

                parent_dir = archive_path.parent
                target_root = Path(self.target_root).resolve()
                current = parent_dir
                while True:
                    new_parent = self._promote_if_single_child(current, stop_dir=target_root)
                    if new_parent is None:
                        break
                    current = new_parent

        except Exception as e:
            self.log_signal.emit(f"处理过程中发生异常: {e}")
            self.failed_archives.add(abs_path)

    def run(self):
        try:
            total_initial = len(self.jobs)
            processed_count = 0
            for job, row in self.jobs:
                if self._is_cancelled:
                    self.log_signal.emit("任务已取消")
                    if row >= 0:
                        self.status_signal.emit(row, "已取消")
                    break
                rel_parent = job.relative_parent
                dest_base = Path(self.target_root) / rel_parent / job.archive_path.stem
                dest = unique_dir(dest_base)
                self.log_signal.emit(f"处理初始任务: {job.archive_path.name}")
                if row >= 0:
                    self.status_signal.emit(row, "解压中")
                self._process_archive(job.archive_path, dest, depth=0)

                if row >= 0:
                    abs_path = job.archive_path.resolve()
                    if abs_path in self.failed_archives:
                        self.status_signal.emit(row, "失败")
                    else:
                        self.status_signal.emit(row, "成功")
                processed_count += 1
                self.progress_signal.emit(int(processed_count / total_initial * 100))
                self.log_signal.emit(f"已完成 {processed_count}/{total_initial} 个初始任务")

            if self.global_flatten:
                self.log_signal.emit("正在全局优化目录结构（去除冗余嵌套）...")
                self._global_flatten_single_child_dirs(Path(self.target_root))
        except Exception as e:
            self.log_signal.emit(f"工作线程异常终止: {e}")
        finally:
            self.progress_signal.emit(100)
            self.finished_signal.emit()

# ---------- 主窗口 ----------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("批量解压工具 v15.0 - 深色主题")
        self.setMinimumSize(1100, 800)
        self.jobs = []
        self.row_to_job = {}
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
        self.target_edit.setPlaceholderText("选择或输入输出目录")
        target_layout.addWidget(self.target_edit)
        browse_btn = QPushButton("浏览...")
        browse_btn.setIcon(self.get_icon("browse.png", QStyle.StandardPixmap.SP_DirOpenIcon))
        browse_btn.clicked.connect(self.browse_target)
        target_layout.addWidget(browse_btn)
        open_btn = QPushButton("打开")
        open_btn.setObjectName("secondary")
        open_btn.setIcon(self.get_icon("open.png", QStyle.StandardPixmap.SP_DialogOpenButton))
        open_btn.clicked.connect(self.open_target)
        target_layout.addWidget(open_btn)
        layout.addLayout(target_layout)

        # 添加按钮、密码管理、目录策略
        add_layout = QHBoxLayout()
        add_file_btn = QPushButton("添加文件")
        add_file_btn.setIcon(self.get_icon("add_file.png", QStyle.StandardPixmap.SP_FileIcon))
        add_file_btn.clicked.connect(self.add_files_dialog)
        add_folder_btn = QPushButton("添加文件夹")
        add_folder_btn.setIcon(self.get_icon("add_folder.png", QStyle.StandardPixmap.SP_DirIcon))
        add_folder_btn.clicked.connect(self.add_folder_dialog)
        self.password_btn = QPushButton("密码管理")
        self.password_btn.setObjectName("secondary")
        self.password_btn.clicked.connect(self.manage_passwords)
        add_layout.addWidget(add_file_btn)
        add_layout.addWidget(add_folder_btn)
        add_layout.addWidget(self.password_btn)
        add_layout.addStretch()

        add_layout.addWidget(QLabel("目录策略:"))
        self.strategy_combo = QComboBox()
        self.strategy_combo.addItem("策略A：保留源文件夹结构")
        self.strategy_combo.addItem("策略B：智能提升+全局去嵌套")
        self.strategy_combo.addItem("策略C：保留压缩包目录，不提升")
        self.strategy_combo.setCurrentIndex(1)   # 默认 B
        add_layout.addWidget(self.strategy_combo)
        layout.addLayout(add_layout)

        # 选项复选框
        option_layout = QHBoxLayout()
        self.recursive_checkbox = QCheckBox("递归解压压缩包内的压缩包")
        self.recursive_checkbox.setChecked(True)
        self.smart_flatten_checkbox = QCheckBox("智能提升单文件夹")
        self.smart_flatten_checkbox.setChecked(True)
        self.global_flatten_checkbox = QCheckBox("全局去除冗余嵌套")
        self.global_flatten_checkbox.setChecked(True)
        self.delete_intermediate_checkbox = QCheckBox("删除中间层压缩包文件（不删除源文件）")
        self.delete_intermediate_checkbox.setChecked(True)
        option_layout.addWidget(self.recursive_checkbox)
        option_layout.addWidget(self.smart_flatten_checkbox)
        option_layout.addWidget(self.global_flatten_checkbox)
        option_layout.addWidget(self.delete_intermediate_checkbox)
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

        # 按钮布局
        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("开始解压")
        self.start_btn.setIcon(self.get_icon("start.png", QStyle.StandardPixmap.SP_MediaPlay))
        self.start_btn.clicked.connect(self.start_extract)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setObjectName("danger")
        self.cancel_btn.setIcon(self.get_icon("cancel.png", QStyle.StandardPixmap.SP_MediaStop))
        self.cancel_btn.clicked.connect(self.cancel_extract)
        self.cancel_btn.setEnabled(False)
        self.clear_btn = QPushButton("清空列表")
        self.clear_btn.setObjectName("secondary")
        self.clear_btn.setIcon(self.get_icon("clear.png", QStyle.StandardPixmap.SP_TrashIcon))
        self.clear_btn.clicked.connect(self.clear_queue)
        self.remove_done_btn = QPushButton("移除已完成")
        self.remove_done_btn.setObjectName("secondary")
        self.remove_done_btn.setIcon(self.get_icon("remove_done.png", QStyle.StandardPixmap.SP_DialogResetButton))
        self.remove_done_btn.clicked.connect(self.remove_finished)
        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.cancel_btn)
        btn_layout.addWidget(self.clear_btn)
        btn_layout.addWidget(self.remove_done_btn)
        layout.addLayout(btn_layout)

        self.setCentralWidget(central)

        # 连接策略变化信号
        self.strategy_combo.currentIndexChanged.connect(self.on_strategy_changed)
        self.on_strategy_changed(self.strategy_combo.currentIndex())

    def on_strategy_changed(self, index):
        if index == 0:   # 策略A
            self.smart_flatten_checkbox.setEnabled(False)
            self.global_flatten_checkbox.setEnabled(False)
            self.smart_flatten_checkbox.setChecked(False)
            self.global_flatten_checkbox.setChecked(False)
        elif index == 1: # 策略B
            self.smart_flatten_checkbox.setEnabled(True)
            self.global_flatten_checkbox.setEnabled(True)
        elif index == 2: # 策略C
            self.smart_flatten_checkbox.setEnabled(False)
            self.global_flatten_checkbox.setEnabled(False)
            self.smart_flatten_checkbox.setChecked(False)
            self.global_flatten_checkbox.setChecked(False)

    # ---------- 配置管理 ----------
    def config_path(self):
        app_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
        return app_dir / "app_config.json"

    def load_config(self):
        try:
            with open(self.config_path(), 'r', encoding='utf-8') as f:
                config = json.load(f)
                if isinstance(config, dict):
                    return config
        except Exception:
            pass
        return {}

    def save_config(self):
        config = {
            'target_dir': self.target_edit.text().strip(),
            'last_add_file_dir': self.config.get('last_add_file_dir', ''),
            'last_add_folder_dir': self.config.get('last_add_folder_dir', ''),
            'strategy': self.strategy_combo.currentIndex()
        }
        try:
            with open(self.config_path(), 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"保存配置失败: {e}")

    def update_config_from_state(self):
        """在关键操作后更新配置并保存"""
        self.save_config()

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
                    if isinstance(data, list):
                        return data
        except Exception:
            pass
        return []

    def save_passwords(self):
        try:
            app_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
            file_path = app_dir / "passwords.json"
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(self.passwords, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"保存密码列表失败: {e}")

    def manage_passwords(self):
        dialog = PasswordManagerDialog(self.passwords, self)
        if dialog.exec() == QDialog.Accepted:
            self.passwords = dialog.get_passwords()
            self.save_passwords()
            self.log(f"密码列表已更新（当前 {len(self.passwords)} 条）")

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

    def get_icon(self, filename: str, fallback_pixmap):
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
        # 确定初始目录：优先 last_add_file_dir，否则目标文件夹
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
            # 保存最后选择文件的父目录
            last_file = Path(files[-1])
            self.config['last_add_file_dir'] = str(last_file.parent)
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
        dialog.setDirectory(start_dir)   # 设置初始目录
        for view in dialog.findChildren(QListView):
            view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        for view in dialog.findChildren(QTreeView):
            view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        if dialog.exec():
            folders = dialog.selectedFiles()
            if folders:
                # 保存最后一个所选文件夹的父目录
                last_folder = Path(folders[-1])
                self.config['last_add_folder_dir'] = str(last_folder.parent)
                self.save_config()
            for f in folders:
                if Path(f).is_dir():
                    self.add_folder(Path(f))

    def add_single_file(self, file_path: Path):
        if not is_archive(file_path):
            magic_type = detect_archive_type_by_magic(file_path)
            if magic_type:
                self.log(f"检测到伪装压缩包: {file_path.name} (实际为 {magic_type})")
            else:
                self.log(f"跳过非压缩包文件: {file_path.name}")
                return
        job = ExtractJob(file_path, Path('.'), is_intermediate=False)
        self._add_job(job)

    def add_folder(self, folder_path: Path):
        jobs = scan_archives_in_folder(folder_path)
        if not jobs:
            magic_jobs = scan_archives_with_magic(folder_path, folder_path)
            if magic_jobs:
                jobs = magic_jobs
                self.log("常规扫描未发现压缩包，但通过内容检测发现以下文件:")
                for j in jobs:
                    self.log(f"  {j.archive_path.name}")
            else:
                self.log(f"文件夹中未发现压缩包: {folder_path}")
                return

        strategy_index = self.strategy_combo.currentIndex()
        for job in jobs:
            job.is_intermediate = False
            if strategy_index == 0:  # 策略A：保留源文件夹结构
                job.relative_parent = Path(folder_path.name) / job.relative_parent
            self._add_job(job)

    def _add_job(self, job: ExtractJob):
        for existing in self.jobs:
            if existing.archive_path == job.archive_path:
                self.log(f"已存在，跳过: {job.archive_path.name}")
                return
        self.jobs.append(job)
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(job.archive_path.name))
        self.table.setItem(row, 1, QTableWidgetItem(str(job.archive_path.parent)))
        output_subdir = (job.relative_parent / job.archive_path.stem).as_posix()
        self.table.setItem(row, 2, QTableWidgetItem(output_subdir))
        self.table.setItem(row, 3, QTableWidgetItem("等待"))
        self.table.setItem(row, 4, QTableWidgetItem(""))
        self.row_to_job[row] = job
        self.log(f"添加: {job.archive_path.name}")

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
            if row in self.row_to_job:
                job = self.row_to_job.pop(row)
                if job in self.jobs:
                    self.jobs.remove(job)
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
            if row in self.row_to_job:
                job = self.row_to_job.pop(row)
                if job in self.jobs:
                    self.jobs.remove(job)
        self._rebuild_row_mapping()
        if rows_to_remove:
            self.log(f"已移除 {len(rows_to_remove)} 个已完成项目")

    def _rebuild_row_mapping(self):
        self.row_to_job.clear()
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 0)
            path_item = self.table.item(row, 1)
            if name_item and path_item:
                archive_path = Path(path_item.text()) / name_item.text()
                for job in self.jobs:
                    if job.archive_path == archive_path:
                        self.row_to_job[row] = job
                        break

    def start_extract(self):
        if self.worker and self.worker.isRunning():
            self.log("已有解压任务在进行中")
            return
        if not self.jobs:
            QMessageBox.warning(self, "提示", "请先添加压缩包")
            return
        target = self.target_edit.text().strip()
        if not Path(target).exists():
            QMessageBox.warning(self, "提示", "目标文件夹不存在，请重新选择")
            return

        # 保存当前配置
        self.save_config()

        extract_queue = []
        for row, job in self.row_to_job.items():
            if job in self.jobs:
                extract_queue.append((job, row))

        if not extract_queue:
            QMessageBox.warning(self, "提示", "队列为空")
            return

        recursive = self.recursive_checkbox.isChecked()
        smart_flatten = self.smart_flatten_checkbox.isChecked()
        global_flatten = self.global_flatten_checkbox.isChecked()
        delete_intermediate = self.delete_intermediate_checkbox.isChecked()
        strategy_index = self.strategy_combo.currentIndex()
        strategy = 'A' if strategy_index == 0 else ('B' if strategy_index == 1 else 'C')

        self.worker = ExtractWorker(
            extract_queue, target, self.passwords,
            strategy=strategy,
            recursive=recursive,
            smart_flatten=smart_flatten,
            global_flatten=global_flatten,
            delete_intermediate=delete_intermediate
        )
        self.worker.log_signal.connect(self.log)
        self.worker.status_signal.connect(self.update_status)
        self.worker.progress_signal.connect(self.progress.setValue)
        self.worker.finished_signal.connect(self.on_worker_finished)
        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.worker.start()

    def update_status(self, row, status):
        if row >= 0 and row < self.table.rowCount():
            item = self.table.item(row, 3)
            if item:
                item.setText(status)

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
        self.jobs.clear()
        self.table.setRowCount(0)
        self.row_to_job.clear()
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