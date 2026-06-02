# 以窗口方式运行（双击即可）
# 如果你的系统 python 命令不是指向 Python 3，请改成 py -3

Set-Location -LiteralPath $PSScriptRoot

try {
  python .\app_v2.py
} catch {
  py -3 .\app_v2.py
}
