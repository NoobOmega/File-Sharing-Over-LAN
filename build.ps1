# 使用 PyInstaller 打包 Windows 可执行文件
# 依赖：pip install pyinstaller

Set-Location -LiteralPath $PSScriptRoot

# --noconsole: GUI 程序不弹控制台窗口
# --onefile: 生成单文件 exe（启动会慢一点）
py -3 -m PyInstaller --noconsole --onefile --name lan-file-transfer .\app_v2.py

Write-Host "Build done. See dist\\lan-file-transfer.exe"
