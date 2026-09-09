# 批量解压工具

一个基于 Python + PySide6 的批量解压工具，支持递归解压、伪装文件识别、智能目录提升、密码撞库等功能。

## 功能特性
- 拖拽添加多个压缩包/文件夹
- 支持 ZIP、7z、RAR、TAR 等常见格式（依赖 7-Zip 等外部工具）
- 递归解压嵌套压缩包（含伪装扩展名）
- 智能去除冗余目录层级
- 密码管理与自动撞库
- 三种目录命名策略
- 冲突处理选项（跳过/重命名/覆盖）
- 深色主题界面

## 安装与运行
1. 安装 Python 3.10+
2. 安装依赖：
   Bash
   pip install -r requirements.txt
3. 将 7z.exe 或 7za.exe 放在程序目录（可选，用于 RAR 等格式）
4. 运行：
   Bash
   python extractor_app.py

## 打包为 exe
Bash
pip install pyinstaller
pyinstaller --onefile --windowed --add-binary "7z.exe;." --name "BatchExtractor" extractor_app.py

## 许可证
本项目代码采用 MIT 许可证（见 LICENSE 文件）。
7-Zip 组件采用 LGPL 许可证，详见 THIRD_PARTY_NOTICES.md。

## 第三方组件
本仓库可能包含 7-Zip 的命令行工具，用于解压 RAR、7z 等格式。
7-Zip 依据 GNU LGPL v2.1 发布，版权归 Igor Pavlov 所有。
完整许可证文本见 licenses/7zip-LGPL.txt（或 THIRD_PARTY_NOTICES.md 中的链接）。