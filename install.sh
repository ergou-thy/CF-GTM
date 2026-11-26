#!/usr/bin/env bash
# CF-GTM 主控安装器；只替换受管程序文件，独立 Ping API 需另外运行其安装器。
set +x
set -Eeuo pipefail
umask 077

REPO='ergou-thy/CF-GTM'
BRANCH='main'
UPDATE=0
WORK=''
LOCK=''
MUTATED=0
COMMITTED=0
SERVICE_MUTATED=0
SERVICE_ACTIVE=''
SERVICE_ENABLED=''
SERVICE_STATUS=()
CONFIG_CREATED=0
REQUIRED_FILES=(app.py requirements.txt config.example.json templates/index.html templates/login.html
    static/app.css static/app.js static/login.js static/favicon.svg
    tools/install-ping-api.sh tools/ping_api.py cf-gtm-ping-api.sh)

info() { printf '[CF-GTM] %s\n' "$*"; }
fail() { printf '[CF-GTM] 错误：%s\n' "$*" >&2; exit 1; }
usage() {
    cat <<'EOF'
CF-GTM 主控安装器（root，Python >= 3.9；普通 Linux 使用 systemd，宝塔使用 Supervisor）
用法：bash install.sh [--update] [--help]
  --update   更新已有安装；保留 config.json、state.json、日志和现有 venv
  --help     显示帮助
环境变量：
  CFGTM_DIR  安装目录，默认 /opt/cf-gtm；宝塔默认 /www/wwwroot/cf-gtm
  CFGTM_PORT 新建 config.json 的端口，默认 8787；更新始终沿用原配置端口
示例：CFGTM_DIR=/opt/cf-gtm CFGTM_PORT=8787 bash install.sh
      CFGTM_DIR=/opt/cf-gtm bash install.sh --update
仅从 GitHub 官方归档下载。失败自动恢复受管程序和原服务文件、注册及启停状态；不回退 venv 内已安装的依赖。
首装失败保留 config.json 与 venv 供排障；再次安装沿用该配置。
不会自动安装/启动国内 Ping API；可另行运行 tools/install-ping-api.sh。
EOF
}
while (($#)); do
    case "$1" in
        --update) UPDATE=1 ;;
        --help|-h) usage; exit 0 ;;
        *) fail "未知参数：$1（使用 --help 查看帮助）" ;;
    esac
    shift
done
((EUID == 0)) || fail '请使用 root 运行安装器'
BT=0
if [[ -d /www/server/panel ]]; then BT=1; fi
if ((BT)); then DIR=${CFGTM_DIR:-/www/wwwroot/cf-gtm}; else DIR=${CFGTM_DIR:-/opt/cf-gtm}; fi
# 限制服务配置可用字符，并在运行包管理器前拒绝明显危险目录。
[[ "$DIR" =~ ^/([A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+$ && "$DIR" != *'/../'* && "$DIR" != */.. && "$DIR" != *'/./'* && "$DIR" != */. ]] || fail 'CFGTM_DIR 必须是无空格、无相对段的绝对目录'
case "$DIR" in
    /|/opt|/srv|/var|/var/lib|/var/www|/www|/www/wwwroot|/home|/root|/tmp|/run|/etc|/usr|/bin|/sbin|/lib|/lib64|/dev|/proc|/sys|/etc/*|/usr/*|/bin/*|/sbin/*|/lib/*|/lib64/*|/dev/*|/proc/*|/sys/*|/run/*) fail 'CFGTM_DIR 不能是系统目录或共享根目录' ;;
esac
if [[ -e "$DIR/app.py" && "$UPDATE" == 0 ]]; then fail '检测到已有安装，请显式使用 --update'; fi
if ((UPDATE)); then [[ -f "$DIR/app.py" && -f "$DIR/config.json" ]] || fail '--update 需要已有 app.py 和 config.json'; fi

# 归档处理与文件事务集中在内嵌 Python 中，便于不安装系统服务的隔离测试。
bundle() {
    "${HELPER_PYTHON:-python3}" - "$@" <<'PY_HELPER'
import ast
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import sys
import tarfile
import tempfile
from unittest import mock

REQUIRED = (
    'app.py', 'requirements.txt', 'config.example.json',
    'templates/index.html', 'templates/login.html',
    'static/app.css', 'static/app.js', 'static/login.js', 'static/favicon.svg',
    'tools/install-ping-api.sh', 'tools/ping_api.py', 'cf-gtm-ping-api.sh',
)


def check_target(root):
    # 每一层都禁止符号链接；不允许 root 安装到其他用户可替换的父目录。
    for path in reversed((root, *root.parents)):
        if path.is_symlink():
            raise ValueError('安装路径含符号链接')
        if path.exists():
            mode = path.stat()
            if not path.is_dir():
                raise ValueError('安装路径不是目录')
            if os.name == 'posix' and (mode.st_uid not in (0, os.geteuid()) or (mode.st_mode & 0o022 and not mode.st_mode & stat.S_ISVTX)):
                raise ValueError('安装路径必须由 root 拥有，且不可被其他用户改写')
    for name in (*REQUIRED, 'config.json', 'state.json', 'venv'):
        target = root / name
        for path in (target, *target.parents):
            if path == root.parent:
                break
            if path.is_symlink():
                raise ValueError('受管路径含符号链接：' + name)
            if path.exists() and os.name == 'posix':
                mode = path.stat()
                if mode.st_uid not in (0, os.geteuid()) or mode.st_mode & 0o022:
                    raise ValueError('受管路径可被其他用户改写：' + name)
        if target.exists() and ((name == 'venv' and not target.is_dir()) or (name != 'venv' and not target.is_file())):
            raise ValueError('受管路径类型不正确：' + name)


def unpack(archive, destination):
    selected = {}
    seen = set()
    roots = set()
    total = 0
    with tarfile.open(archive, 'r:gz') as source:
        for index, member in enumerate(source):
            name = member.name
            parts = name.split('/')
            if index >= 4096 or not name or '//' in name or '\\' in name or name.startswith('/') or any(p in ('.', '..') for p in parts) or any(':' in p for p in parts):
                raise ValueError('归档路径不安全或文件过多')
            path = PurePosixPath(name)
            if not path.parts or path.is_absolute() or path.as_posix() in seen:
                raise ValueError('归档路径为空或重复')
            seen.add(path.as_posix())
            roots.add(path.parts[0])
            if not (member.isdir() or member.isreg()):
                raise ValueError('归档禁止符号链接、硬链接及特殊文件')
            total += member.size
            if member.size < 0 or member.size > 16 * 1024 * 1024 or total > 64 * 1024 * 1024:
                raise ValueError('归档文件过大')
            relative = '/'.join(path.parts[1:])
            if relative in REQUIRED:
                if not member.isreg() or not member.size:
                    raise ValueError('必需路径不是非空普通文件：' + relative)
                selected[relative] = member
        if len(roots) != 1 or set(selected) != set(REQUIRED):
            raise ValueError('归档缺少必需文件或顶层目录不唯一')
        # 所有成员通过校验后才写临时目录；不调用 tar.extract/extractall。
        for name in REQUIRED:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(selected[name]) as stream, target.open('xb') as out:
                shutil.copyfileobj(stream, out)
            target.chmod(0o700 if name.endswith('.sh') else 0o600)
    ast.parse((destination / 'app.py').read_text(encoding='utf-8'), feature_version=(3, 9))
    for name in ('app.py', 'tools/ping_api.py'):
        compile((destination / name).read_bytes(), name, 'exec')
    with (destination / 'config.example.json').open(encoding='utf-8') as stream:
        if not isinstance(json.load(stream), dict):
            raise ValueError('配置示例必须是 JSON 对象')


def copy_atomic(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name('.' + destination.name + '.cfgtm-new')
    # 不跟随意外出现的旧临时文件或符号链接。
    with temporary.open('xb') as out, source.open('rb') as stream:
        shutil.copyfileobj(stream, out)
    shutil.copystat(source, temporary)
    os.replace(temporary, destination)


def validate_program(source, root):
    # 只加载临时副本；load_config 的补字段、状态恢复及日志不得触碰线上文件。
    import requests
    from flask import Flask

    def blocked(*args, **kwargs):
        raise RuntimeError('安装预检禁止网络、后台线程和 Web 服务')

    with tempfile.TemporaryDirectory(prefix='cfgtm-preflight-') as directory, contextlib.ExitStack() as stack:
        temporary = Path(directory)
        shutil.copy2(root / 'config.json', temporary / 'config.json')
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        stack.enter_context(mock.patch.object(sys, 'dont_write_bytecode', True))
        for target in ('socket.create_connection', 'socket.getaddrinfo', 'socket.socket.connect',
                       'socket.socket.connect_ex', 'threading.Thread.start'):
            stack.enter_context(mock.patch(target, new=blocked))
        stack.enter_context(mock.patch.object(requests.sessions.Session, 'request', new=blocked))
        stack.enter_context(mock.patch.object(Flask, 'run', new=blocked))
        name = '_cfgtm_installer_preflight'
        spec = importlib.util.spec_from_file_location(name, source / 'app.py')
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {name: module}):
            spec.loader.exec_module(module)
            module.BASE_DIR = str(temporary)
            module.CONFIG_PATH = str(temporary / 'config.json')
            module.STATE_PATH = str(temporary / 'state.json')
            module.load_config()
            module.validate_config(module.config)


def service_links(roots, unit):
    links = {}
    for root in roots:
        for directory, folders, files in os.walk(root, followlinks=False):
            for name in (*folders, *files):
                path = Path(directory) / name
                if path.is_symlink():
                    target = os.readlink(path)
                    if name == unit or Path(target).name == unit:
                        links[str(path)] = target
    return links


def service_backup(path, backup, roots):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('服务配置必须是普通文件')
    backup.mkdir(mode=0o700)
    data = {'path': str(path), 'present': path.exists(), 'roots': list(map(str, roots)),
            'links': service_links(roots, path.name)}
    if data['present']:
        metadata = path.stat()
        data.update(mode=stat.S_IMODE(metadata.st_mode), uid=metadata.st_uid, gid=metadata.st_gid)
        shutil.copy2(path, backup / 'service-file')
    (backup / 'service.json').write_text(json.dumps(data), encoding='utf-8')


def service_restore(backup):
    data = json.loads((backup / 'service.json').read_text(encoding='utf-8'))
    path = Path(data['path'])
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('服务配置在安装期间被外部替换')
    if data['present']:
        copy_atomic(backup / 'service-file', path)
        if os.name == 'posix':
            os.chown(path, data['uid'], data['gid'])
        path.chmod(data['mode'])
    else:
        path.unlink(missing_ok=True)
    # 直接恢复精确链接集合，避免 systemctl disable 删除原有自定义/运行时链接。
    current = service_links(data['roots'], path.name)
    for name in set(current) | set(data['links']):
        link = Path(name)
        expected = data['links'].get(name)
        if link.is_symlink() and os.readlink(link) == expected:
            continue
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            raise ValueError('服务启用链接在安装期间被外部替换')
        if expected is not None:
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(expected)


def supervisor_state(path):
    lines = path.read_text(encoding='utf-8').strip().splitlines()
    if len(lines) == 1 and re.fullmatch(r'cf-gtm(?::\*)?: ERROR \(no such (?:group|process)\)', lines[0]):
        return ['0']
    running = []
    seen = set()
    if not lines:
        raise ValueError('无法读取 Supervisor 原状态')
    for line in lines:
        fields = line.split()
        if (len(fields) < 2 or not re.fullmatch(r'cf-gtm(?::[A-Za-z0-9_.-]+)?', fields[0])
                or fields[0] in seen or fields[1] not in ('RUNNING', 'STOPPED', 'EXITED', 'FATAL')):
            raise ValueError('Supervisor 状态不稳定或无法识别')
        seen.add(fields[0])
        if fields[1] == 'RUNNING':
            running.append(fields[0])
    return ['1', *sorted(running)]


def main():
    action, *args = sys.argv[1:]
    if action == 'paths':
        check_target(Path(args[0]))
    elif action == 'unpack':
        unpack(Path(args[0]), Path(args[1]))
    elif action == 'backup':
        root, backup = map(Path, args)
        backup.mkdir(mode=0o700)
        present = [name for name in REQUIRED if (root / name).exists()]
        for name in present:
            target = backup / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name, target)
        (backup / 'manifest.json').write_text(json.dumps(present), encoding='utf-8')
    elif action == 'deploy':
        root, source = map(Path, args)
        check_target(root)
        for name in REQUIRED:
            copy_atomic(source / name, root / name)
    elif action == 'rollback':
        root, backup = map(Path, args)
        present = json.loads((backup / 'manifest.json').read_text(encoding='utf-8'))
        for name in REQUIRED:
            temporary = (root / name).with_name('.' + Path(name).name + '.cfgtm-new')
            temporary.unlink(missing_ok=True)
            if name in present:
                copy_atomic(backup / name, root / name)
            else:
                (root / name).unlink(missing_ok=True)
    elif action == 'validate':
        validate_program(Path(args[0]), Path(args[1]))
    elif action == 'service-backup':
        service_backup(Path(args[0]), Path(args[1]), list(map(Path, args[2:])))
    elif action == 'service-restore':
        service_restore(Path(args[0]))
    elif action == 'supervisor-state':
        print('\n'.join(supervisor_state(Path(args[0]))))
    elif action == 'config':
        root = Path(args[0])
        target = root / 'config.json'
        if not target.exists():
            port = int(args[1])
            if not 1 <= port <= 65535:
                raise ValueError('CFGTM_PORT 必须在 1–65535 之间')
            source = Path(args[2]) if len(args) > 2 else root
            data = json.loads((source / 'config.example.json').read_text(encoding='utf-8'))
            data['auto_switch'] = False
            data['dry_run'] = True
            data.setdefault('web', {}).update(host='0.0.0.0', port=port,
                password=secrets.token_urlsafe(24), secret=secrets.token_urlsafe(32))
            with target.open('x', encoding='utf-8') as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
        target.chmod(0o600)
        # 只输出端口，原有密码、secret、Token 不进入安装日志。
        data = json.loads(target.read_text(encoding='utf-8'))
        port = data['web']['port']
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError('原配置 web.port 无效')
        print(port)
    else:
        raise ValueError('未知内部操作')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # 不输出 JSON 内容或异常详情，防止坏配置内的秘密进入日志。
        print('安装文件处理失败（' + type(exc).__name__ + '），请检查路径、配置和官方归档。', file=sys.stderr)
        sys.exit(1)
PY_HELPER
}

systemd_enabled_state() {
    local enabled load
    enabled=$(systemctl is-enabled cf-gtm 2>/dev/null) || true
    # 缺失单元在部分 systemd 版本只向 stderr 报错，没有 is-enabled 输出。
    # 仅独立确认 LoadState=not-found 后才视为初装，不能吞掉查询失败。
    if [[ -z "$enabled" ]]; then
        load=$(systemctl show --property=LoadState --value cf-gtm) || return 1
        [[ "$load" == not-found ]] || return 1
        enabled=not-found
    fi
    printf '%s\n' "$enabled"
}

service_snapshot() {
    if ((BT)); then
        "${CTL[@]}" status 'cf-gtm:*' >"$WORK/service-status" 2>/dev/null || true
        bundle supervisor-state "$WORK/service-status" >"$WORK/service-state"
        mapfile -t SERVICE_STATUS <"$WORK/service-state"
    else
        SERVICE_ACTIVE=$(systemctl show --property=ActiveState --value cf-gtm)
        case "$SERVICE_ACTIVE" in active|inactive|failed) ;; *) fail 'systemd 服务状态正在变化，请稍后重试' ;; esac
        SERVICE_ENABLED=$(systemd_enabled_state) || fail '无法读取 systemd 原启用状态'
        case "$SERVICE_ENABLED" in enabled|enabled-runtime|disabled|static|indirect|linked|linked-runtime|masked|masked-runtime|generated|transient|alias|not-found) ;; *) fail '无法读取 systemd 原启用状态' ;; esac
    fi
    bundle service-backup "$1" "$WORK/service-backup" "${@:2}"
}

service_stop() {
    if ((BT)); then
        "${CTL[@]}" stop 'cf-gtm:*' >/dev/null 2>&1 || {
            "${CTL[@]}" status 'cf-gtm:*' >"$WORK/stopped-status" 2>/dev/null || true
            local stopped
            stopped=$(bundle supervisor-state "$WORK/stopped-status") || return 1
            [[ "$stopped" == 0 || "$stopped" == 1 ]] || return 1
        }
        if [[ "${SERVICE_STATUS[0]}" == 0 ]]; then
            # 首装注册必须移除；update/reload 失败时也不能留下 autorestart 循环。
            "${CTL[@]}" remove cf-gtm >/dev/null 2>&1 || true
        fi
    else
        systemctl stop cf-gtm >/dev/null 2>&1 || {
            local stopped
            stopped=$(systemctl show --property=ActiveState --value cf-gtm) || return 1
            [[ "$stopped" == inactive || "$stopped" == failed ]] || return 1
        }
    fi
}

service_recover() {
    bundle service-restore "$WORK/service-backup" || return 1
    if ((BT)); then
        "${CTL[@]}" reread >/dev/null 2>&1 || return 1
        "${CTL[@]}" update cf-gtm >/dev/null 2>&1 || return 1
        # 恢复旧配置时 autostart 可能已启动进程，先停再逐个恢复原先在运行的进程。
        service_stop || return 1
        local process restored
        for process in "${SERVICE_STATUS[@]:1}"; do
            "${CTL[@]}" start "$process" >/dev/null 2>&1 || return 1
        done
        "${CTL[@]}" status 'cf-gtm:*' >"$WORK/restored-status" 2>/dev/null || true
        restored=$(bundle supervisor-state "$WORK/restored-status") || return 1
        [[ "$restored" == "$(<"$WORK/service-state")" ]] || return 1
    else
        systemctl daemon-reload >/dev/null 2>&1 || return 1
        if [[ "$SERVICE_ACTIVE" == active ]]; then
            systemctl start cf-gtm >/dev/null 2>&1 || return 1
        elif [[ "$SERVICE_ENABLED" == not-found ]]; then
            systemctl reset-failed cf-gtm >/dev/null 2>&1 || true
        fi
        local enabled active
        enabled=$(systemd_enabled_state) || return 1
        [[ "$enabled" == "$SERVICE_ENABLED" ]] || return 1
        active=$(systemctl show --property=ActiveState --value cf-gtm) || return 1
        if [[ "$SERVICE_ACTIVE" == active ]]; then [[ "$active" == active ]] || return 1
        else [[ "$active" == inactive || "$active" == failed ]] || return 1; fi
    fi
}

cleanup() {
    local status=$? recovery_failed=0
    trap - EXIT
    if ((status != 0 && !COMMITTED)); then
        if ((SERVICE_MUTATED)); then service_stop || recovery_failed=1; fi
        if ((MUTATED)); then
            info '安装失败，恢复更新前的受管程序文件…' >&2
            bundle rollback "$DIR" "$WORK/backup" || recovery_failed=1
        fi
        if ((SERVICE_MUTATED)); then service_recover || recovery_failed=1; fi
        if ((recovery_failed)); then
            info "回滚未完成，原文件与服务备份保留在：$WORK" >&2
            WORK=''
        fi
        if ((CONFIG_CREATED)) && [[ -f "$DIR/config.json" ]]; then
            info "初装配置保留供排障：$DIR/config.json（未输出凭据）；再次安装将沿用该配置。" >&2
        fi
    fi
    [[ -z "$WORK" ]] || rm -rf -- "$WORK"
    [[ -z "$LOCK" ]] || rmdir -- "$LOCK" 2>/dev/null || true
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# 只在缺少工具时安装系统依赖，不强制刷新已有主机的包索引。
if ! command -v python3 >/dev/null || ! command -v curl >/dev/null || ! python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then
    info '安装 Python 虚拟环境与下载依赖…'
    if command -v apt-get >/dev/null; then
        apt-get update -qq
        apt-get install -y -qq python3 python3-venv python3-pip curl >/dev/null
    elif command -v dnf >/dev/null; then dnf install -y -q python3 python3-pip curl >/dev/null
    elif command -v yum >/dev/null; then yum install -y -q python3 python3-pip curl >/dev/null
    else fail '请先安装 Python >= 3.9、venv、pip 和 curl'; fi
fi
python3 -c 'import venv, ensurepip' >/dev/null 2>&1 || fail 'Python 缺少 venv 或 ensurepip，请安装对应发行版的 Python 虚拟环境支持'
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || fail '主控要求 Python >= 3.9，请先升级系统 Python'
bundle paths "$DIR"
mkdir -p -- "$(dirname -- "$DIR")"
if mkdir -- "$DIR.install-lock" 2>/dev/null; then LOCK="$DIR.install-lock"; else fail '另一个安装器正在运行，或存在安装锁；确认无进程后再手动清理'; fi
# 获得锁后重新检查，避免两个首次安装并发通过前置检查。
bundle paths "$DIR"
if [[ -e "$DIR/app.py" && "$UPDATE" == 0 ]]; then fail '检测到已有安装，请显式使用 --update'; fi
if ((BT)); then
    CTL=(/www/server/panel/pyenv/bin/supervisorctl -c /etc/supervisor/supervisord.conf)
    [[ -x "${CTL[0]}" && -d /www/server/panel/plugin/supervisor/profile ]] || fail '请先在宝塔安装并配置 Supervisor 插件'
else
    command -v systemctl >/dev/null || fail '未找到 systemd'
    systemctl show-environment >/dev/null 2>&1 || fail 'systemd 未运行'
fi
WORK=$(mktemp -d)
info '下载 GitHub 官方归档…'
if ! curl --proto '=https' --proto-redir '=https' -fLsS --connect-timeout 15 --max-time 120 --max-filesize 33554432 \
    "https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz" -o "$WORK/source.tar.gz"; then
    curl --proto '=https' --proto-redir '=https' -fLsS --connect-timeout 15 --max-time 120 --max-filesize 33554432 \
        "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH" -o "$WORK/source.tar.gz"
fi
bundle unpack "$WORK/source.tar.gz" "$WORK/source"
mkdir -p -- "$DIR"
# 旧配置必须先能解析；更新时不使用 CFGTM_PORT，也不补写任何业务配置。
if ((UPDATE)); then PORT=$(bundle config "$DIR" 8787)
else PORT=${CFGTM_PORT:-8787}; [[ "$PORT" =~ ^[0-9]{1,5}$ ]] && ((10#$PORT >= 1 && 10#$PORT <= 65535)) || fail 'CFGTM_PORT 必须为 1–65535 的端口'; fi
if [[ ! -x "$DIR/venv/bin/python" ]]; then
    [[ ! -e "$DIR/venv" ]] || fail '已有 venv 不完整，请先手动修复；安装器不会重建旧环境'
    python3 -m venv "$DIR/venv"
fi
"$DIR/venv/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || fail '现有 venv 的 Python 低于 3.9，请手动迁移环境'
# venv 保留；pip 失败时尚未修改程序文件，依赖变更本身不在文件回滚范围。
"$DIR/venv/bin/python" -m pip install --disable-pip-version-check -q -r "$WORK/source/requirements.txt"
"$DIR/venv/bin/python" -m pip check >/dev/null
if [[ ! -e "$DIR/config.json" ]]; then CONFIG_CREATED=1; fi
PORT=$(bundle config "$DIR" "$PORT" "$WORK/source")
# 在覆盖程序、修改服务前，用新版本的真实加载器校验配置副本；禁止网络和后台启动。
HELPER_PYTHON="$DIR/venv/bin/python" bundle validate "$WORK/source" "$DIR"
bundle backup "$DIR" "$WORK/backup"
MUTATED=1
bundle deploy "$DIR" "$WORK/source"

info '配置并启动主控服务…'
if ((BT)); then
    INI=/www/server/panel/plugin/supervisor/profile/cf-gtm.ini
    LOGDIR=/www/server/panel/plugin/supervisor/log
    [[ ! -L "$INI" && ! -L "$LOGDIR" ]] || fail 'Supervisor 配置或日志目录不能是符号链接'
    mkdir -p -- "$LOGDIR"
    service_snapshot "$INI"
    SERVICE_MUTATED=1
    cat > "$INI" <<EOF
[program:cf-gtm]
command=$DIR/venv/bin/python $DIR/app.py
directory=$DIR
user=root
environment=PYTHONIOENCODING="utf-8",PYTHONDONTWRITEBYTECODE="1"
autostart=true
autorestart=true
startsecs=3
startretries=3
stopasgroup=true
killasgroup=true
redirect_stderr=true
stdout_logfile=$LOGDIR/cf-gtm.out.log
stdout_logfile_maxbytes=2MB
numprocs=1
process_name=%(program_name)s_%(process_num)02d
EOF
    "${CTL[@]}" reread >/dev/null
    "${CTL[@]}" update cf-gtm >/dev/null
    "${CTL[@]}" restart 'cf-gtm:*' >/dev/null || "${CTL[@]}" start 'cf-gtm:*' >/dev/null
    sleep 3
    "${CTL[@]}" status 'cf-gtm:*' | grep -q RUNNING || fail 'Supervisor 启动失败，请查看宝塔 Supervisor 日志'
else
    [[ ! -L /etc/systemd/system/cf-gtm.service ]] || fail 'systemd 服务文件不能是符号链接'
    service_snapshot /etc/systemd/system/cf-gtm.service /etc/systemd/system /run/systemd/system
    SERVICE_MUTATED=1
    cat > /etc/systemd/system/cf-gtm.service <<EOF
[Unit]
Description=CF-GTM (Cloudflare DNS failover panel)
After=network.target

[Service]
Type=simple
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python $DIR/app.py
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=always
RestartSec=5
User=root
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable cf-gtm >/dev/null 2>&1
    systemctl restart cf-gtm
    sleep 3
    systemctl is-active --quiet cf-gtm || fail '服务启动失败，请查看 journalctl -u cf-gtm -n 50'
fi
COMMITTED=1
info "安装完成。面板端口：$PORT；配置：$DIR/config.json"
if ((CONFIG_CREATED)); then
    # 仅确实新建配置且直接交互时显示密码，管道/测试/重定向日志中不输出秘密。
    if [[ -t 1 ]]; then
        "$DIR/venv/bin/python" - "$DIR/config.json" <<'PY_PASSWORD'
import json, sys
with open(sys.argv[1], encoding='utf-8') as stream:
    print('面板密码：' + json.load(stream)['web']['password'])
PY_PASSWORD
    else info '面板随机密码已写入仅 root 可读的 config.json，请在本机安全查看。'; fi
    info '自动切换已关闭（auto_switch=false，dry_run=true），请先完成业务配置。'
else
    info '已沿用原 config.json，自动切换与 dry_run 按原设置运行。'
fi
info '建议配置 HTTPS 反向代理，并限制面板端口访问。'
