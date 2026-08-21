@echo off
setlocal
rem mcnb GUI ランチャー
rem  ダブルクリックで起動できます。

cd /d "%~dp0"

echo.
echo   mcnb - 音符ブロック編曲
echo   ----------------------------------------
echo.

where uv >nul 2>&1
if errorlevel 1 (
  echo   [エラー] uv が見つかりません。
  echo   https://docs.astral.sh/uv/ からインストールしてください。
  echo.
  pause
  exit /b 1
)

echo   依存を確認しています...
uv sync --extra gui --extra audio --extra measure
if errorlevel 1 (
  echo.
  echo   [エラー] 依存の準備に失敗しました。
  echo   Minecraft や別の mcnb が起動中だと失敗することがあります。
  echo   閉じてからもう一度試してください。
  echo.
  pause
  exit /b 1
)

echo.
echo   ブラウザを開きます。閉じるには、この黒い窓で Ctrl+C を押すか窓を閉じてください。
echo.

uv run mcnb gui
set RC=%errorlevel%

if not "%RC%"=="0" (
  echo.
  echo   [エラー] 終了コード %RC%
  pause
)
endlocal
