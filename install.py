"""Interactive and failure-visible runtime setup wizard for WeChat Bridge v3.

The bootstrap itself uses only the Python standard library.  Detection never
implies consent: selecting a runtime, creating an isolated environment, and
running pip are separate decisions with an explicit confirmation boundary.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import subprocess
import sys
import traceback
from collections.abc import Callable, Sequence
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REQUIREMENTS = ROOT / "requirements.txt"
VENV_DIR = ROOT / ".venv"
RUNTIME_FILE = ROOT / ".runtime-python.txt"
OFFLINE_PACKAGE_DIR = ROOT / "offline-packages"
OFFLINE_PACKAGE_MANIFEST = "wheelhouse-manifest.json"
OFFLINE_PACKAGE_KIND = "wechat-bridge-v3-wheelhouse"
OFFLINE_PACKAGE_FORMAT = 1
MINIMUM_PYTHON = (3, 10)
RUNTIME_IMPORT_CHECK = (
    "import re, cv2, numpy, win32api, comtypes, PIL, requests, websockets; "
    "V=lambda value: tuple(int(x) for x in re.findall(r'\\d+', str(value))[:3]); "
    "assert (4,10) <= V(cv2.__version__) < (5,); "
    "assert (1,23,5) <= V(numpy.__version__) < (3,); "
    "assert (10,) <= V(PIL.__version__) < (13,); "
    "assert (2,28) <= V(requests.__version__) < (3,); "
    "assert (12,) <= V(websockets.__version__) < (18,)"
)
WINDOWS_INVALID_PATH_CHARS = frozenset('<>:"|?*')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}

FLOW_SUCCESS = "success"
FLOW_MAIN_MENU = "main_menu"
FLOW_EXIT = "exit"
FLOW_SELECT_PYTHON = "select_python"


class SetupCancelled(Exception):
    """Raised when the interactive input stream is cancelled or closed."""


class RuntimeInspection:
    """Read-only inspection result for one Python executable."""

    def __init__(
        self,
        python: Path,
        *,
        can_start: bool,
        version: str = "未知",
        version_info: tuple[int, int, int] = (0, 0, 0),
        dependencies_ready: bool = False,
        error: str = "",
    ) -> None:
        self.python = Path(python).resolve()
        self.can_start = bool(can_start)
        self.version = str(version or "未知")
        self.version_info = tuple(version_info)
        self.dependencies_ready = bool(dependencies_ready)
        self.error = str(error or "")

    @property
    def supported(self) -> bool:
        return self.can_start and self.version_info >= (*MINIMUM_PYTHON, 0)

    @property
    def ready(self) -> bool:
        return self.supported and self.dependencies_ready


class OfflinePackageSet:
    """A hash-verified wheelhouse that can satisfy requirements without a network."""

    def __init__(
        self,
        directory: Path,
        *,
        files: Sequence[Path],
        supported_python: Sequence[str],
        requirements_sha256: str,
    ) -> None:
        self.directory = Path(directory).resolve()
        self.files = tuple(Path(path).resolve() for path in files)
        self.supported_python = tuple(str(value) for value in supported_python)
        self.requirements_sha256 = str(requirements_sha256)

    def supports(self, version_info: Sequence[int]) -> bool:
        if len(version_info) < 2:
            return False
        return f"{int(version_info[0])}.{int(version_info[1])}" in self.supported_python


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_offline_packages(
    directory: Path = OFFLINE_PACKAGE_DIR,
    *,
    requirements: Path = REQUIREMENTS,
) -> OfflinePackageSet | None:
    """Validate the optional offline wheelhouse before it can influence pip."""

    directory = Path(directory).resolve()
    if not directory.exists():
        return None
    if not directory.is_dir():
        raise NotADirectoryError(f"离线依赖位置不是文件夹：{directory}")
    manifest_path = directory / OFFLINE_PACKAGE_MANIFEST
    if not manifest_path.is_file():
        if any(directory.iterdir()):
            raise RuntimeError(
                f"离线依赖目录不完整，缺少校验清单：{manifest_path}"
            )
        return None
    if not requirements.is_file():
        raise FileNotFoundError(f"依赖清单不存在：{requirements}")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"离线依赖校验清单无法读取：{exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("离线依赖校验清单必须是 JSON 对象。")
    if payload.get("format_version") != OFFLINE_PACKAGE_FORMAT:
        raise RuntimeError("离线依赖校验清单版本不受支持。")
    if payload.get("kind") != OFFLINE_PACKAGE_KIND:
        raise RuntimeError("离线依赖包类型不正确。")
    expected_requirements = _sha256_file(requirements)
    if payload.get("requirements_sha256") != expected_requirements:
        raise RuntimeError(
            "离线依赖包与当前 requirements.txt 不匹配，请下载同一版本的依赖包。"
        )
    supported_python = payload.get("supported_python")
    if (
        not isinstance(supported_python, list)
        or not supported_python
        or any(not re.fullmatch(r"3\.\d{1,2}", str(value)) for value in supported_python)
    ):
        raise RuntimeError("离线依赖包没有有效的 Python 版本范围。")
    entries = payload.get("files")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("离线依赖包没有列出任何 wheel 文件。")
    files: list[Path] = []
    expected_names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("离线依赖包文件清单格式不正确。")
        name = entry.get("name")
        sha256 = entry.get("sha256")
        size = entry.get("size")
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not name.casefold().endswith(".whl")
            or not isinstance(sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", sha256)
            or not isinstance(size, int)
            or size <= 0
        ):
            raise RuntimeError("离线依赖包文件清单包含无效记录。")
        folded = name.casefold()
        if folded in expected_names:
            raise RuntimeError(f"离线依赖包重复列出文件：{name}")
        expected_names.add(folded)
        path = directory / name
        if not path.is_file():
            raise RuntimeError(f"离线依赖包缺少文件：{name}")
        if path.stat().st_size != size:
            raise RuntimeError(f"离线依赖文件大小校验失败：{name}")
        if _sha256_file(path) != sha256:
            raise RuntimeError(f"离线依赖文件 SHA-256 校验失败：{name}")
        files.append(path)
    actual_names = {
        path.name.casefold() for path in directory.glob("*.whl") if path.is_file()
    }
    extras = sorted(actual_names - expected_names)
    if extras:
        raise RuntimeError("离线依赖目录包含未登记 wheel：" + ", ".join(extras))
    return OfflinePackageSet(
        directory,
        files=files,
        supported_python=supported_python,
        requirements_sha256=expected_requirements,
    )


def virtual_environment_python(venv_dir: Path = VENV_DIR) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _console_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")


def _run_command(command: Sequence[str], *, cwd: Path) -> int:
    completed = subprocess.run(list(command), cwd=str(cwd), check=False)
    return int(completed.returncode)


def _runtime_ready(python: Path) -> bool:
    try:
        completed = subprocess.run(
            (str(python), "-c", RUNTIME_IMPORT_CHECK),
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _inspect_python(python: Path) -> RuntimeInspection:
    resolved = Path(python).resolve()
    if not resolved.is_file():
        return RuntimeInspection(
            resolved,
            can_start=False,
            error="文件不存在或不是文件",
        )
    command = (
        str(resolved),
        "-c",
        "import sys; print('.'.join(map(str, sys.version_info[:3])))",
    )
    try:
        completed = subprocess.run(
            command,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return RuntimeInspection(
            resolved,
            can_start=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "无错误详情").strip()
        return RuntimeInspection(
            resolved,
            can_start=False,
            error=f"退出码 {completed.returncode}：{detail}",
        )
    version = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else "未知"
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        return RuntimeInspection(
            resolved,
            can_start=False,
            version=version,
            error="无法解析 Python 版本",
        )
    version_info = tuple(int(part) for part in match.groups())
    return RuntimeInspection(
        resolved,
        can_start=True,
        version=version,
        version_info=version_info,
        dependencies_ready=_runtime_ready(resolved),
    )


def _write_runtime(python: Path, runtime_file: Path = RUNTIME_FILE) -> None:
    resolved = python.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"运行解释器不存在：{resolved}")
    runtime_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = runtime_file.with_suffix(runtime_file.suffix + ".tmp")
    temporary.write_text(str(resolved), encoding="utf-8")
    temporary.replace(runtime_file)


def _read_answer(prompt: str, input_func: Callable[[str], str]) -> str:
    try:
        return str(input_func(prompt) or "").strip()
    except (EOFError, KeyboardInterrupt) as exc:
        raise SetupCancelled from exc


def _menu(
    title: str,
    choices: Sequence[tuple[str, str]],
    input_func: Callable[[str], str],
) -> str:
    print(f"\n{title}")
    valid = {key for key, _label in choices}
    for key, label in choices:
        print(f"  [{key}] {label}")
    while True:
        answer = _read_answer("请输入编号（直接回车不会执行）：", input_func)
        if answer in valid:
            return answer
        if not answer:
            print("尚未选择任何操作；不会自动继续。请输入上方编号。")
        else:
            print("无效选项；不会执行任何操作。请重新输入。")


def _strip_wrapping_quotes(value: str) -> str:
    cleaned = str(value or "").strip()
    while (
        len(cleaned) >= 2
        and cleaned[0] == cleaned[-1]
        and cleaned[0] in {'"', "'"}
    ):
        cleaned = cleaned[1:-1].strip()
    return cleaned


def _windows_path_issue(value: str) -> str:
    if "\x00" in value:
        return "路径包含空字符"
    if sys.platform != "win32":
        return ""

    drive, tail = ntpath.splitdrive(value)
    if drive and not (
        re.fullmatch(r"[A-Za-z]:", drive)
        or drive.startswith("\\\\")
    ):
        return f"盘符格式不正确：{drive}"
    for component in re.split(r"[\\/]+", tail):
        if component in {"", ".", ".."}:
            continue
        invalid = sorted(set(component) & WINDOWS_INVALID_PATH_CHARS)
        if invalid:
            return "目录名包含 Windows 不允许的字符：" + " ".join(invalid)
        if component.endswith((" ", ".")):
            return "目录名不能以空格或句点结尾"
        reserved = component.split(".", 1)[0].upper()
        if reserved in WINDOWS_RESERVED_NAMES:
            return f"目录名不能使用 Windows 保留名称：{reserved}"
    return ""


def _resolve_user_path(value: str, *, default: Path) -> Path:
    cleaned = _strip_wrapping_quotes(value) if value else str(default)
    issue = _windows_path_issue(cleaned)
    if issue:
        raise ValueError(issue)
    selected = Path(cleaned).expanduser()
    if not selected.is_absolute():
        selected = ROOT / selected
    return selected.resolve()


def _validate_venv_directory(venv_dir: Path) -> Path:
    issue = _windows_path_issue(str(venv_dir))
    if issue:
        raise ValueError(issue)
    resolved = Path(venv_dir).resolve()
    if resolved.exists() and not resolved.is_dir():
        raise NotADirectoryError(f"独立环境位置不是文件夹：{resolved}")
    if not resolved.exists() and resolved.suffix.casefold() == ".exe":
        raise NotADirectoryError(
            f"独立环境位置不能是 Python 文件路径：{resolved}"
        )
    return resolved


def _default_venv_directory() -> Path:
    configured = _strip_wrapping_quotes(str(os.environ.get("WECHAT_VENV_DIR") or ""))
    default = Path(configured).expanduser() if configured else VENV_DIR
    if not default.is_absolute():
        default = ROOT / default
    return default.resolve()


def _python_candidates_in_directory(directory: Path) -> list[Path]:
    """Return supported interpreter locations inside a user-selected folder."""

    relative_candidates = (
        Path("python.exe"),
        Path("Scripts") / "python.exe",
        Path("bin") / "python",
    )
    found: list[Path] = []
    for relative in relative_candidates:
        candidate = (directory / relative).resolve()
        if candidate.is_file() and candidate not in found:
            found.append(candidate)
    return found


def _direct_python_in_directory(directory: Path) -> Path | None:
    for name in ("python.exe", "python"):
        candidate = (directory / name).resolve()
        if candidate.is_file():
            return candidate
    return None


def _dependency_names(requirements: Path) -> list[str]:
    names: list[str] = []
    for line in requirements.read_text(encoding="utf-8").splitlines():
        cleaned = line.split("#", 1)[0].strip()
        if cleaned:
            names.append(re.split(r"[<>=!~\[]", cleaned, maxsplit=1)[0])
    return names


def _show_runtime_report(label: str, status: RuntimeInspection) -> None:
    print(f"\n{label}")
    print(f"  路径：{status.python}")
    if not status.can_start:
        print("  启动：失败")
        print(f"  原因：{status.error or '未知错误'}")
        return
    print("  启动：正常")
    print(f"  版本：Python {status.version}")
    print(f"  版本要求：{'满足' if status.supported else '不满足'}（需要 Python 3.10 或更高版本）")
    print(f"  v3 依赖：{'完整，可以直接运行' if status.dependencies_ready else '不完整，不能直接运行'}")


def _remember_runtime(status: RuntimeInspection, runtime_file: Path) -> int:
    print("\n执行计划")
    print(f"  将选中：{status.python}")
    print("  不运行 pip")
    print("  不创建或修改虚拟环境")
    print(f"  只把选择结果写入：{runtime_file}")
    return 0


def execute(
    runner: Callable[..., int] = _run_command,
    *,
    requirements: Path = REQUIREMENTS,
    venv_dir: Path = VENV_DIR,
    bootstrap_python: Path | None = None,
    offline_packages: OfflinePackageSet | None = None,
) -> int:
    """Create/repair an isolated environment and preserve every failure."""

    try:
        if not requirements.is_file():
            raise FileNotFoundError(f"依赖清单不存在：{requirements}")

        bootstrap = Path(bootstrap_python or sys.executable).resolve()
        if not bootstrap.is_file():
            raise FileNotFoundError(f"引导 Python 不存在：{bootstrap}")
        venv_dir = _validate_venv_directory(Path(venv_dir))
        venv_python = virtual_environment_python(venv_dir)
        print(f"引导 Python：{bootstrap}")
        print(f"v3 独立环境：{venv_dir}")
        print(f"依赖清单：{requirements}")
        if offline_packages is not None:
            print(f"离线依赖：{offline_packages.directory}")

        if not venv_python.is_file():
            print("\n正在创建 v3 独立 Python 环境……")
            create_code = int(
                runner(
                    (str(bootstrap), "-m", "venv", str(venv_dir)),
                    cwd=ROOT,
                )
            )
            if create_code:
                print(
                    f"\n虚拟环境创建失败，退出码：{create_code}",
                    file=sys.stderr,
                )
                return create_code

        print("\n正在向 v3 独立环境安装依赖……")
        install_command = [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
        ]
        if offline_packages is not None:
            install_command.extend(
                ("--no-index", "--find-links", str(offline_packages.directory))
            )
        install_command.extend(("-r", str(requirements)))
        install_code = int(runner(tuple(install_command), cwd=ROOT))
        if install_code:
            print(f"\npip 安装失败，退出码：{install_code}", file=sys.stderr)
            return install_code

        print("\n正在验证关键依赖……")
        validation_code = int(
            runner(
                (
                    str(venv_python),
                    "-c",
                    RUNTIME_IMPORT_CHECK
                    + "; print('v3 依赖验证通过：OpenCV ' + cv2.__version__)",
                ),
                cwd=ROOT,
            )
        )
        if validation_code:
            print(
                f"\n依赖安装完成，但导入验证失败，退出码：{validation_code}",
                file=sys.stderr,
            )
            return validation_code

        print("\nv3 独立环境和依赖安装完成。")
        print(f"运行解释器：{venv_python}")
        return 0
    except KeyboardInterrupt:
        print("\n已收到 Ctrl+C，依赖安装已停止。")
        return 130
    except BaseException as exc:
        print(
            f"\n无法创建环境或执行安装命令：{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        traceback.print_exc()
        return 1


def _isolated_environment_flow(
    bootstrap: RuntimeInspection,
    *,
    input_func: Callable[[str], str],
    installer: Callable[..., int],
    runtime_file: Path,
    requirements: Path,
    offline_packages: OfflinePackageSet | None,
) -> tuple[str, Path | None]:
    if not bootstrap.supported:
        print("该 Python 不能作为引导解释器：需要可正常启动的 Python 3.10 或更高版本。")
        return FLOW_MAIN_MENU, None

    default = _default_venv_directory()
    while True:
        print("\n创建或修复独立环境")
        print("  这里必须填写文件夹，不能填写 python.exe。")
        print(f"  直接回车只会选择默认目录，仍需确认：{default}")
        answer = _read_answer("请输入文件夹路径：", input_func)
        try:
            selected = _resolve_user_path(answer, default=default)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"路径无效：{exc}。尚未创建任何内容，请重新输入。")
            continue

        existing_python = selected if selected.is_file() else None
        if selected.is_dir():
            existing_python = _direct_python_in_directory(selected)
        if existing_python is not None:
            choice = _menu(
                (
                    "检测到已有 Python，而不是适合创建独立环境的目标目录："
                    f"\n  输入：{selected}\n  Python：{existing_python}"
                ),
                (
                    ("1", "转到“选中已有 Python”流程"),
                    ("2", "重新输入环境目录"),
                    ("3", "返回主菜单"),
                    ("4", "退出，不做任何修改"),
                ),
                input_func,
            )
            if choice == "1":
                return FLOW_SELECT_PYTHON, existing_python
            if choice == "2":
                continue
            if choice == "3":
                return FLOW_MAIN_MENU, None
            return FLOW_EXIT, None

        if (
            not selected.exists()
            and (
                selected.suffix.casefold() == ".exe"
                or selected.name.casefold().startswith("python")
            )
        ):
            print(f"找不到 Python 文件：{selected}。尚未创建任何内容，请重新输入。")
            continue
        try:
            selected = _validate_venv_directory(selected)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"此目录不能作为独立环境目标：{exc}。尚未创建任何内容，请重新输入。")
            continue

        venv_python = virtual_environment_python(selected)
        if venv_python.is_file():
            action = "修复现有独立环境"
        elif selected.exists():
            action = "在现有文件夹中创建独立环境"
        else:
            action = "新建独立环境"
        dependencies = _dependency_names(requirements)
        usable_offline = (
            offline_packages
            if offline_packages is not None
            and offline_packages.supports(bootstrap.version_info)
            else None
        )
        print("\n最终执行计划")
        print(f"  引导 Python：{bootstrap.python}（Python {bootstrap.version}）")
        print(f"  目标目录：{selected}")
        print(f"  操作：{action}")
        print(f"  pip 将安装或修复 {len(dependencies)} 项依赖：{', '.join(dependencies)}")
        if usable_offline is not None:
            print(f"  安装来源：本地完整依赖包（{len(usable_offline.files)} 个 wheel）")
            print("  网络：本次 pip 安装禁用在线索引，不会下载 Python 包")
        elif offline_packages is not None:
            print(
                "  离线包不支持当前 Python "
                f"{bootstrap.version_info[0]}.{bootstrap.version_info[1]}，将联网安装"
            )
        else:
            print("  安装来源：在线 Python 软件源（未检测到完整离线依赖包）")
        print("  不会向引导 Python 安装依赖，也不会修改它。")
        choice = _menu(
            "请决定是否执行上述计划",
            (
                ("1", "确认执行"),
                ("2", "修改环境目录"),
                ("3", "返回主菜单"),
                ("4", "退出，不做任何修改"),
            ),
            input_func,
        )
        if choice == "2":
            continue
        if choice == "3":
            return FLOW_MAIN_MENU, None
        if choice == "4":
            return FLOW_EXIT, None

        installer_arguments = {
            "venv_dir": selected,
            "bootstrap_python": bootstrap.python,
        }
        if usable_offline is not None:
            installer_arguments["offline_packages"] = usable_offline
        exit_code = int(installer(**installer_arguments))
        if exit_code:
            return str(exit_code), None
        runtime_python = virtual_environment_python(selected)
        _write_runtime(runtime_python, runtime_file)
        print(f"\n已记住运行环境：{runtime_python.resolve()}")
        print("以后直接运行 start.bat；如需更换环境，可重新运行 first.bat。")
        return FLOW_SUCCESS, runtime_python


def _python_selection_flow(
    initial_python: Path | None,
    *,
    input_func: Callable[[str], str],
    inspector: Callable[[Path], RuntimeInspection],
    installer: Callable[..., int],
    runtime_file: Path,
    requirements: Path,
    offline_packages: OfflinePackageSet | None,
) -> str:
    candidate = Path(initial_python).resolve() if initial_python is not None else None
    while True:
        if candidate is None:
            print("\n选中另一套已有 Python")
            print("  可以输入 Python 所在文件夹，也可以输入完整的 python.exe 路径。")
            print("  路径两侧可保留英文双引号。")
            answer = _read_answer("Python 文件夹或文件路径：", input_func)
            if not answer:
                print("路径为空；尚未选择或修改任何运行环境。")
                continue
            try:
                selected = _resolve_user_path(answer, default=Path(sys.executable))
            except (OSError, RuntimeError, ValueError) as exc:
                print(f"路径无效：{exc}。请重新输入。")
                candidate = None
                continue
            if selected.is_file():
                candidate = selected
            elif selected.is_dir():
                candidates = _python_candidates_in_directory(selected)
                if not candidates:
                    print(
                        f"该文件夹中没有找到 Python：{selected}\n"
                        "已检查 python.exe、Scripts\\python.exe 和 bin\\python。请重新输入。"
                    )
                    candidate = None
                    continue
                if len(candidates) == 1:
                    candidate = candidates[0]
                    print(f"已从文件夹解析到 Python：{candidate}")
                else:
                    choices = [
                        (str(index), f"选中 {python}")
                        for index, python in enumerate(candidates, start=1)
                    ]
                    reselect_key = str(len(choices) + 1)
                    main_key = str(len(choices) + 2)
                    exit_key = str(len(choices) + 3)
                    choices.extend(
                        (
                            (reselect_key, "重新输入路径"),
                            (main_key, "返回主菜单"),
                            (exit_key, "退出，不做任何修改"),
                        )
                    )
                    selected_choice = _menu(
                        "该文件夹中检测到多套 Python，请明确选中一套",
                        choices,
                        input_func,
                    )
                    if selected_choice == reselect_key:
                        candidate = None
                        continue
                    if selected_choice == main_key:
                        return FLOW_MAIN_MENU
                    if selected_choice == exit_key:
                        return FLOW_EXIT
                    candidate = candidates[int(selected_choice) - 1]
            else:
                print(f"找不到 Python 文件或文件夹：{selected}。请重新输入。")
                candidate = None
                continue

        print(f"\n已选中候选 Python：{candidate}")
        print("正在检测版本和 v3 依赖……")
        status = inspector(candidate)
        _show_runtime_report("Python 检测结果", status)
        if status.ready:
            _remember_runtime(status, runtime_file)
            choice = _menu(
                "检测已经完成，请由你决定下一步",
                (
                    ("1", "确认选中这套 Python"),
                    ("2", "重新选中另一套 Python"),
                    ("3", "返回主菜单"),
                    ("4", "退出，不做任何修改"),
                ),
                input_func,
            )
            if choice == "1":
                _write_runtime(status.python, runtime_file)
                print(f"\n已记住运行环境：{status.python}")
                print("以后直接运行 start.bat；如需更换环境，可重新运行 first.bat。")
                return FLOW_SUCCESS
            if choice == "2":
                candidate = None
                continue
            if choice == "3":
                return FLOW_MAIN_MENU
            return FLOW_EXIT

        if status.can_start and status.supported:
            choice = _menu(
                "这套 Python 缺少 v3 依赖，不会被自动修改",
                (
                    ("1", "用它作为引导 Python，创建或修复独立环境"),
                    ("2", "重新选中另一套 Python"),
                    ("3", "返回主菜单"),
                    ("4", "退出，不做任何修改"),
                ),
                input_func,
            )
            if choice == "1":
                result, transferred = _isolated_environment_flow(
                    status,
                    input_func=input_func,
                    installer=installer,
                    runtime_file=runtime_file,
                    requirements=requirements,
                    offline_packages=offline_packages,
                )
                if result == FLOW_SELECT_PYTHON:
                    candidate = transferred
                    continue
                return result
            if choice == "2":
                candidate = None
                continue
            if choice == "3":
                return FLOW_MAIN_MENU
            return FLOW_EXIT

        choice = _menu(
            "这套 Python 不能被选为 v3 运行环境",
            (
                ("1", "重新选中另一套 Python"),
                ("2", "返回主菜单"),
                ("3", "退出，不做任何修改"),
            ),
            input_func,
        )
        if choice == "1":
            candidate = None
            continue
        if choice == "2":
            return FLOW_MAIN_MENU
        return FLOW_EXIT


def configure(
    *,
    input_func: Callable[[str], str] = input,
    inspector: Callable[[Path], RuntimeInspection] = _inspect_python,
    installer: Callable[..., int] = execute,
    runtime_file: Path = RUNTIME_FILE,
    requirements: Path = REQUIREMENTS,
    offline_package_dir: Path = OFFLINE_PACKAGE_DIR,
) -> int:
    """Run the explicit setup wizard and remember only a confirmed choice."""

    try:
        if not requirements.is_file():
            raise FileNotFoundError(f"依赖清单不存在：{requirements}")
        offline_packages = _load_offline_packages(
            offline_package_dir,
            requirements=requirements,
        )
        bootstrap = Path(sys.executable).resolve()
        while True:
            print("\n========================================")
            print(" WeChat Bridge v3 运行环境配置向导")
            print("========================================")
            print("\n当前找到的 Python（尚未检测）")
            print(f"  路径：{bootstrap}")
            choice = _menu(
                "请选择要做的事",
                (
                    ("1", "选中当前 Python"),
                    ("2", "选中另一套已有 Python"),
                    ("3", "创建或修复独立环境"),
                    ("4", "退出，不做任何修改"),
                ),
                input_func,
            )
            if choice in {"1", "2"}:
                result = _python_selection_flow(
                    bootstrap if choice == "1" else None,
                    input_func=input_func,
                    inspector=inspector,
                    installer=installer,
                    runtime_file=runtime_file,
                    requirements=requirements,
                    offline_packages=offline_packages,
                )
            elif choice == "3":
                print(f"\n已选中候选引导 Python：{bootstrap}")
                print("正在检测版本和 v3 依赖……")
                bootstrap_status = inspector(bootstrap)
                _show_runtime_report("引导 Python 检测结果", bootstrap_status)
                result, transferred = _isolated_environment_flow(
                    bootstrap_status,
                    input_func=input_func,
                    installer=installer,
                    runtime_file=runtime_file,
                    requirements=requirements,
                    offline_packages=offline_packages,
                )
                if result == FLOW_SELECT_PYTHON:
                    result = _python_selection_flow(
                        transferred,
                        input_func=input_func,
                        inspector=inspector,
                        installer=installer,
                        runtime_file=runtime_file,
                        requirements=requirements,
                        offline_packages=offline_packages,
                    )
            else:
                result = FLOW_EXIT

            if result == FLOW_SUCCESS:
                return 0
            if result == FLOW_MAIN_MENU:
                continue
            if result == FLOW_EXIT:
                print("\n已取消环境配置；没有保存新的运行环境。")
                return 130
            return int(result)
    except SetupCancelled:
        print("\n输入已取消或关闭；没有执行尚未确认的操作，也没有保存新的运行环境。")
        return 130
    except KeyboardInterrupt:
        print("\n已收到 Ctrl+C；没有执行尚未确认的操作，也没有保存新的运行环境。")
        return 130
    except BaseException as exc:
        print(
            f"\n环境配置失败：{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        traceback.print_exc()
        return 1


def wait_for_exit() -> None:
    try:
        input("\n按 Enter 关闭此窗口……")
    except (EOFError, KeyboardInterrupt):
        pass


def main(*, wait: bool = True) -> int:
    _console_utf8()
    exit_code = configure()
    if wait:
        wait_for_exit()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(wait="--no-wait" not in sys.argv[1:]))
