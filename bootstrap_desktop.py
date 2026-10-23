"""Prepare a desktop environment using only Python's standard library."""
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parent


class SetupError(Exception):
    pass


def venv_python(environment):
    return environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def pip_healthy(python):
    if not python.is_file():
        return False
    try:
        version = subprocess.run([str(python), "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        if version.returncode:
            return False
        result = subprocess.run([str(python), "-m", "pip", "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_venv(project, base_python=sys.executable, emit=print):
    """Preserve an unhealthy environment, recreate once at its final path."""
    environment = Path(project).resolve() / ".venv"
    if environment.is_symlink():
        raise SetupError(".venv 是符号链接，未自动修改。请先确认它指向的环境，再使用项目内的独立虚拟环境。")
    if environment.exists() and not environment.is_dir():
        raise SetupError("项目中有名为 .venv 的文件，无法创建环境；请保留并重命名该文件后重试。")
    python = venv_python(environment)
    if pip_healthy(python):
        emit("已复用通过 pip 检查的项目虚拟环境。")
        return python
    backup = None
    if environment.exists():
        # Avoid moving an interpreter currently running this setup process.
        if Path(sys.prefix).resolve() == environment.resolve():
            raise SetupError("当前终端正在使用这个 .venv。请关闭助手及已激活该环境的终端，用系统 Python 重新双击启动。")
        suffix = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        backup = environment.with_name(".venv.backup-" + suffix)
        try:
            environment.rename(backup)
        except OSError:
            raise SetupError("无法备份原虚拟环境。请关闭其他助手窗口和正在使用该 .venv 的终端，再重试。原文件未删除。") from None
        emit(f"原虚拟环境未通过 pip 检查，已完整保留到：{backup.name}")
    emit("正在创建新的项目虚拟环境…")
    try:
        subprocess.run([str(base_python), "-m", "venv", str(environment)], check=True, stdin=subprocess.DEVNULL, timeout=120)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        extra = f" 原环境仍保存在 {backup.name}。" if backup else ""
        raise SetupError("创建虚拟环境失败。请运行 python.org 官方 Python 安装程序，选择 Modify/Repair，确认包含 pip、venv 和 Tcl/Tk。" + extra) from None
    if not pip_healthy(python):
        extra = f" 原环境仍保存在 {backup.name}。" if backup else ""
        raise SetupError("新建环境的 pip 仍然不能运行，已停止自动修复。请通过 python.org 官方安装程序修复或重新安装 Python，再重试；不要关闭 TLS 或包校验。" + extra)
    emit("虚拟环境及 pip 检查通过。")
    return python


def dependencies_ready(python):
    try:
        result = subprocess.run([str(python), "-c", "import tkinter; from PIL import Image, ImageGrab, ImageTk, __version__; assert __version__ == '12.0.0'"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def prepare_desktop(project=ROOT, base_python=sys.executable, emit=print):
    python = ensure_venv(project, base_python, emit)
    if dependencies_ready(python):
        emit("桌面依赖检查通过。")
        return python
    emit("正在安装固定版本的桌面依赖；请保持网络连接…")
    try:
        # Use the repository's exact version requirement and pip's normal TLS
        # and package-integrity checks. Network failures do not rebuild a venv.
        subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "-r", str(Path(project) / "requirements-desktop.txt")], check=True, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        raise SetupError("依赖安装失败。请查看上方错误，确认能访问 pypi.org 和 files.pythonhosted.org 后重试。现有环境已保留，未反复重建。") from None
    if not dependencies_ready(python):
        raise SetupError("依赖安装后仍缺少 Pillow 或 Tcl/Tk。请通过官方 Python 安装程序修复 Tcl/Tk 组件，再重试。")
    return python


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv not in ([], ["--setup-only"]):
        print("参数错误。请双击 start_windows.bat，或使用 --setup-only 只准备依赖。")
        return 2
    if sys.version_info < (3, 10):
        print("需要 Python 3.10 或更新版本，推荐安装官方 Python 3.13。")
        return 1
    try:
        python = prepare_desktop()
        if argv == ["--setup-only"]:
            return 0
        print("正在打开接话桌面助手…", flush=True)
        return subprocess.call([str(python), str(ROOT / "desktop.py")], cwd=ROOT)
    except SetupError as error:
        print("\n启动未完成：" + str(error), flush=True)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。现有文件已保留。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
