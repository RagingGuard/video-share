@echo off
chcp 65001 >nul
echo ========================================
echo 视频服务器 - EXE打包工具
echo ========================================
echo.

echo [1/4] 清理旧构建文件...
if exist "dist" rmdir /s /q "dist"
if exist "build" rmdir /s /q "build"
if exist "*.spec" del /q "*.spec"
echo 清理完成
echo.

echo [2/4] 检查并安装依赖...
python -m pip install --upgrade pip
python -m pip install pyinstaller
python -m pip install -r requirements.txt
echo 依赖检查完成
echo.

echo [3/4] 生成图标文件...
python create_icon.py
echo.

echo [4/4] 打包EXE（需要几分钟）...
pyinstaller --clean ^
  --onefile ^
  --noconsole ^
  --name VideoWebServer ^
  --icon=icon.ico ^
  --hidden-import=waitress ^
  --hidden-import=psutil ^
  --hidden-import=pystray ^
  --hidden-import=PIL ^
  --hidden-import=PIL.Image ^
  --hidden-import=PIL.ImageDraw ^
  --add-data "static;static" ^
  super_badass_videos_web_server.py

if errorlevel 1 (
    echo.
    echo ❌ 打包失败，请检查错误信息
    pause
    exit /b 1
)

echo 打包完成
echo.

echo [5/5] 整理输出文件...

echo.
echo ========================================
echo ✅ 打包成功
echo ========================================
echo.
echo 输出位置: dist\VideoWebServer.exe
echo.
echo 使用说明:
echo 1. 首次运行自动创建config.ini配置文件
echo 2. 将视频文件放入share和secret文件夹
echo 3. 双击VideoWebServer.exe启动服务器
echo 4. 程序会最小化到系统托盘
echo.
echo 提示: 可将dist文件夹复制到任何位置使用
echo.
pause
