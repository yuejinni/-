"""
打包脚本（Python 版）
在 Windows 上运行：python build.py
"""
import subprocess
import sys
import os
import shutil
import json

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.join(HERE, '..')


def run(cmd, **kw):
    r = subprocess.run(cmd, **kw)
    if r.returncode != 0:
        print('\n❌ 命令失败，退出')
        input('按 Enter 关闭...')
        sys.exit(1)


def write_bat(path, content):
    """写 GBK 编码的 bat 文件，避免中文字节 0x85 被 CMD 误判为换行符"""
    with open(path, 'w', encoding='gbk', errors='replace') as f:
        f.write(content)


# ── 安装依赖 ──────────────────────────────────────────────────
print('安装依赖（首次较慢）...')
run([sys.executable, '-m', 'pip', 'install',
     'pyinstaller', 'flask', 'flask-cors', 'openpyxl', 'xlrd',
     'pdfplumber', 'certifi', 'lxml', 'waitress',
     '-i', 'https://mirrors.aliyun.com/pypi/simple/',
     '--trusted-host', 'mirrors.aliyun.com',
     '--quiet'])

# ── 只读模板文件（内嵌进 exe）──────────────────────────────────
# 注意：BASE_DATA（基础资料）和 ai_config.json 是可写文件，不内嵌
readonly_templates = [
    os.path.join(PROJ, '出口报关单.xlsx'),
    os.path.join(PROJ, 'Proforma Invoice 形式发票.xlsx'),
    os.path.join(PROJ, '装箱单 Packing List.xlsx'),
    os.path.join(PROJ, 'QIHANG销售合同.xlsx'),
    os.path.join(PROJ, '工厂采购合同用.xlsx'),
    os.path.join(PROJ, '发票开具项目信息导入模板.xlsx'),
    os.path.join(PROJ, '副本金华市凌航国际贸易有限发票模板excel.xlsx'),
]

dist_tmp  = os.path.join(HERE, 'dist_tmp')
build_tmp = os.path.join(HERE, 'build_tmp')

cmd = [
    sys.executable, '-m', 'PyInstaller',
    '--onefile',
    '--noconsole',
    '--name', 'server',
    '--hidden-import', 'flask',
    '--hidden-import', 'flask_cors',
    '--hidden-import', 'openpyxl',
    '--hidden-import', 'openpyxl.styles',
    '--hidden-import', 'openpyxl.utils',
    '--hidden-import', 'xlrd',
    '--hidden-import', 'pdfplumber',
    '--hidden-import', 'pdfminer',
    '--hidden-import', 'pdfminer.high_level',
    '--hidden-import', 'pdfminer.layout',
    '--hidden-import', 'certifi',
    '--hidden-import', 'lxml',
    '--hidden-import', 'lxml.etree',
    '--hidden-import', 'waitress',
    '--hidden-import', 'waitress.server',
    '--distpath', dist_tmp,
    '--workpath', build_tmp,
    '--specpath', HERE,
]

# 只读模板内嵌
for f in readonly_templates:
    if os.path.exists(f):
        cmd += ['--add-data', f'{f};.']
    else:
        print(f'  ⚠️  模板文件不存在，跳过：{os.path.basename(f)}')

# templates 目录（jdy.html 等前端页面）
tpl_dir = os.path.join(HERE, 'templates')
if os.path.exists(tpl_dir):
    cmd += ['--add-data', f'{tpl_dir};templates']

cmd.append(os.path.join(HERE, 'server.py'))

print('\n开始打包 server.exe ...')
run(cmd, cwd=HERE)

# ── 组装发行包 ────────────────────────────────────────────────
dist = os.path.join(PROJ, '发行包_Windows')
print(f'\n组装发行包 → {dist}')

if os.path.exists(dist):
    shutil.rmtree(dist)
os.makedirs(os.path.join(dist, '服务端'),    exist_ok=True)
os.makedirs(os.path.join(dist, 'Chrome扩展'), exist_ok=True)

# server.exe
shutil.copy(
    os.path.join(dist_tmp, 'server.exe'),
    os.path.join(dist, '服务端', 'server.exe'),
)

# 可写文件（放 exe 旁边，用户可随时替换）
writeable_files = {
    '报关产品基础资料（智谱）.xlsx': '报关产品基础资料（智谱）.xlsx',
}
for src_name, dst_name in writeable_files.items():
    src = os.path.join(PROJ, src_name)
    dst = os.path.join(dist, '服务端', dst_name)
    if os.path.exists(src):
        shutil.copy(src, dst)
        print(f'  已复制 {src_name}')
    else:
        print(f'  ⚠️  未找到 {src_name}，请手动放到 服务端/ 目录')

# ai_config.json（空 Key 模板，用户自填）
ai_cfg_dst = os.path.join(dist, '服务端', 'ai_config.json')
with open(ai_cfg_dst, 'w', encoding='utf-8') as f:
    json.dump({
        'qianwen_api_key': '',
        'qianwen_model':   'qwen-vl-max',
        'excel_path':      '',
    }, f, ensure_ascii=False, indent=2)

# ── 生成 bat 文件（GBK 编码，避免 0x85 截断问题）─────────────────
write_bat(os.path.join(dist, '服务端', '一键安装.bat'), (
    '@echo off\r\n'
    '\r\n'
    'set "SRV=%~dp0server.exe"\r\n'
    'set "KEY=HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run"\r\n'
    '\r\n'
    'echo 正在安装报关助手服务...\r\n'
    '\r\n'
    'REM 注册开机自启\r\n'
    '%SystemRoot%\\System32\\reg.exe add "%KEY%" /v "BaoGuanServer" /t REG_SZ /d "\\"%SRV%\\"" /f >nul\r\n'
    '\r\n'
    'REM 立即启动服务\r\n'
    'start "" "%SRV%"\r\n'
    '\r\n'
    'echo.\r\n'
    'echo [OK] 安装完成！服务已在后台启动。\r\n'
    'echo.\r\n'
    'echo 接下来安装 Chrome 扩展：\r\n'
    'echo   1. 打开 Chrome，访问 chrome://extensions\r\n'
    'echo   2. 开启右上角"开发者模式"\r\n'
    'echo   3. 点击"加载已解压的扩展程序"\r\n'
    'echo   4. 选择本包的 Chrome扩展 文件夹\r\n'
    'echo.\r\n'
    'echo 服务已设置为开机自动启动，以后无需再次运行此文件。\r\n'
    'echo.\r\n'
    'pause\r\n'
))

write_bat(os.path.join(dist, '服务端', '启动服务.bat'), (
    '@echo off\r\n'
    'start "" "%~dp0server.exe"\r\n'
))

write_bat(os.path.join(dist, '服务端', '安装开机自启.bat'), (
    '@echo off\r\n'
    '\r\n'
    'set "EXE=%~dp0server.exe"\r\n'
    '\r\n'
    '%SystemRoot%\\System32\\reg.exe add '
    '"HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" '
    '/v "BaoGuanServer" /t REG_SZ /d "\\"%EXE%\\"" /f >nul\r\n'
    '\r\n'
    'if %errorlevel% equ 0 (\r\n'
    '    echo.\r\n'
    '    echo [OK] 已设置开机自动启动\r\n'
    '    echo      下次开机后服务将在后台自动运行，无需手动启动\r\n'
    '    echo.\r\n'
    ') else (\r\n'
    '    echo.\r\n'
    '    echo [ERROR] 设置失败，请右键以管理员身份运行\r\n'
    '    echo.\r\n'
    ')\r\n'
    'pause\r\n'
))

write_bat(os.path.join(dist, '服务端', '卸载开机自启.bat'), (
    '@echo off\r\n'
    '\r\n'
    '%SystemRoot%\\System32\\reg.exe delete '
    '"HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" '
    '/v "BaoGuanServer" /f >nul 2>&1\r\n'
    '\r\n'
    'echo.\r\n'
    'echo [OK] 已取消开机自动启动\r\n'
    'echo.\r\n'
    'pause\r\n'
))

print('  已生成 bat 文件（GBK 编码）')

# Chrome 扩展
ext_src = os.path.join(PROJ, 'customs_extension')
if os.path.exists(ext_src):
    shutil.copytree(ext_src, os.path.join(dist, 'Chrome扩展'), dirs_exist_ok=True)

readme = os.path.join(PROJ, '使用说明.md')
if os.path.exists(readme):
    shutil.copy(readme, os.path.join(dist, '使用说明.md'))

# ── 清理临时文件 ──────────────────────────────────────────────
shutil.rmtree(dist_tmp,  ignore_errors=True)
shutil.rmtree(build_tmp, ignore_errors=True)
spec = os.path.join(HERE, 'server.spec')
if os.path.exists(spec):
    os.remove(spec)

print(f'\n✅ 完成！发行包位置：{dist}')
print('\n发行包结构：')
print('  发行包_Windows/')
print('  ├── 服务端/')
print('  │   ├── server.exe            ← 主程序（后台无窗口）')
print('  │   ├── 一键安装.bat          ← 首次安装用这个')
print('  │   ├── 报关产品基础资料（智谱）.xlsx  ← 可直接替换更新')
print('  │   ├── ai_config.json        ← 填入千问 API Key')
print('  │   ├── 启动服务.bat')
print('  │   ├── 安装开机自启.bat')
print('  │   └── 卸载开机自启.bat')
print('  ├── Chrome扩展/')
print('  └── 使用说明.md')
input('\n按 Enter 关闭...')
