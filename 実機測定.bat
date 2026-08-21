@echo off
setlocal
rem 実機測定リグを起動します。
rem  起動後、Minecraft ランチャーで「mcnb (音ブロック)」を選んで起動し、
rem  マルチプレイ -> 直接接続 -> localhost:25566 に繋いでください。

cd /d "%~dp0"

echo.
echo   mcnb - 実機測定
echo   ----------------------------------------
echo   1. このまま待つとサーバが立ちます
echo   2. Minecraft ランチャーで「mcnb (音ブロック)」を起動
echo   3. マルチプレイ -^> 直接接続 -^> localhost:25566
echo   4. 接続したら放置でOK（位置合わせは自動です）
echo.

uv sync --extra measure --extra audio >nul
uv run mcnb measure
pause
endlocal
