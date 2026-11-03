#!/usr/bin/env python3
"""shipit: deploy frontend dist bundles from GitHub release assets."""

import argparse
import codecs
import configparser
import fcntl
import io
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

if sys.version_info < (3, 6):
    sys.exit("shipit 需要 Python 3.6+")

# Python 3.6 under cron's C locale uses ASCII for stdio, which breaks on non-ASCII log output.
for _name in ("stdout", "stderr"):
    _stream = getattr(sys, _name)
    if codecs.lookup(_stream.encoding or "ascii").name == "ascii":
        setattr(sys, _name, io.TextIOWrapper(_stream.buffer, encoding="utf-8", line_buffering=True))

DEFAULTS = {
    "cron": "*/10 * * * *",
    "manifest": "latest.json",
    "tag": "app-v{version}",
    "asset": "dist-v{version}.tar.gz",
    "keep": 5,
}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
CRON_BEGIN = "# >>> shipit >>>"
CRON_END = "# <<< shipit <<<"
PRESERVED_PREFIX = "pre-shipit-"
CONFIG_SECTION = "project"
HTTP_TIMEOUT = 60


def log(msg, name=None):
    prefix = time.strftime("%Y-%m-%d %H:%M:%S")
    if name:
        prefix += f" [{name}]"
    print(f"{prefix} {msg}", flush=True)


def default_home():
    env = os.environ.get("SHIPIT_HOME")
    if env:
        return Path(env)
    if os.geteuid() == 0:
        return Path("/etc/shipit")
    return Path.home() / ".config" / "shipit"


class Home:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.projects = self.root / "projects"
        self.state = self.root / "state"
        self.logs = self.root / "logs"
        self.locks = self.root / "locks"

    def ensure(self):
        for d in (self.projects, self.state, self.logs, self.locks):
            d.mkdir(parents=True, exist_ok=True)

    def project_names(self):
        if not self.projects.is_dir():
            return []
        return sorted(p.stem for p in self.projects.glob("*.ini"))

    def load_project(self, name):
        path = self.projects / f"{name}.ini"
        if not path.is_file():
            raise SystemExit(f"项目不存在: {name}（{path}）")
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(str(path), encoding="utf-8")
        except configparser.Error as e:
            raise SystemExit(f"{path}: 配置格式错误: {e}")
        if not parser.has_section(CONFIG_SECTION):
            raise SystemExit(f"{path}: 缺少 [{CONFIG_SECTION}] 段")
        cfg = dict(DEFAULTS)
        cfg.update(parser[CONFIG_SECTION])
        for key in ("owner", "repo", "deploy_path"):
            if not cfg.get(key):
                raise SystemExit(f"{path}: 缺少必填项 {key}")
        try:
            cfg["keep"] = int(cfg["keep"])
        except ValueError:
            raise SystemExit(f"{path}: keep 必须是整数")
        cfg["name"] = name
        cfg["deploy_path"] = Path(cfg["deploy_path"]).expanduser()
        return cfg

    def load_state(self, name):
        path = self.state / f"{name}.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        return {}

    def save_state(self, name, state):
        path = self.state / f"{name}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)


def write_config(path, data):
    parser = configparser.ConfigParser(interpolation=None)
    parser[CONFIG_SECTION] = {k: str(v) for k, v in data.items()}
    with open(str(path), "w", encoding="utf-8") as f:
        parser.write(f)


def version_key(v):
    core = v.lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    return tuple(int(x) for x in re.findall(r"\d+", core))


def http_open(url):
    req = urllib.request.Request(url, headers={"User-Agent": "shipit"})
    return urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)


def fetch_latest_version(cfg):
    url = f"https://github.com/{cfg['owner']}/{cfg['repo']}/releases/latest/download/{cfg['manifest']}"
    with http_open(url) as resp:
        data = json.load(resp)
    version = str(data.get("version", "")).strip().lstrip("vV")
    if not version:
        raise RuntimeError(f"{url} 中没有 version 字段")
    return version


def asset_url(cfg, version):
    tag = cfg["tag"].format(version=version)
    asset = cfg["asset"].format(version=version)
    return f"https://github.com/{cfg['owner']}/{cfg['repo']}/releases/download/{tag}/{asset}"


def releases_dir(deploy_path):
    return deploy_path.parent / f".{deploy_path.name}.releases"


def safe_extract(archive, dest):
    with tarfile.open(archive, "r:gz") as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, filter="data")
            return
        dest_real = os.path.realpath(dest)
        for m in tar.getmembers():
            target = os.path.realpath(os.path.join(dest, m.name))
            if not (m.isfile() or m.isdir()):
                raise RuntimeError(f"压缩包包含不允许的条目类型: {m.name}")
            if os.path.commonpath([dest_real, target]) != dest_real:
                raise RuntimeError(f"压缩包包含越界路径: {m.name}")
            m.mode = 0o755 if m.isdir() or m.mode & 0o100 else 0o644
            m.uid, m.gid, m.uname, m.gname = os.getuid(), os.getgid(), "", ""
        tar.extractall(dest)


def content_root(extracted):
    entries = [p for p in extracted.iterdir() if p.name not in (".DS_Store", "__MACOSX")]
    if len(entries) == 1 and entries[0].is_dir() and not (extracted / "index.html").exists():
        return entries[0]
    return extracted


def switch_symlink(deploy_path, target):
    rel = os.path.relpath(target, deploy_path.parent)
    if deploy_path.exists() and not deploy_path.is_symlink():
        if not deploy_path.is_dir():
            raise RuntimeError(f"{deploy_path} 已存在且不是目录或软链")
        preserved = releases_dir(deploy_path) / f"{PRESERVED_PREFIX}{time.strftime('%Y%m%d%H%M%S')}"
        os.rename(deploy_path, preserved)
        log(f"已将原有目录移至 {preserved}")
    tmp_link = deploy_path.with_name(f".{deploy_path.name}.shipit-tmp")
    if tmp_link.is_symlink() or tmp_link.exists():
        tmp_link.unlink()
    os.symlink(rel, tmp_link)
    os.replace(tmp_link, deploy_path)


def current_target(deploy_path):
    if deploy_path.is_symlink():
        return (deploy_path.parent / os.readlink(deploy_path)).resolve()
    return None


def list_releases(deploy_path):
    rdir = releases_dir(deploy_path)
    if not rdir.is_dir():
        return []
    return sorted(
        (p for p in rdir.iterdir() if p.is_dir() and not p.name.startswith((".", PRESERVED_PREFIX))),
        key=lambda p: version_key(p.name),
    )


def prune(cfg):
    keep = max(int(cfg["keep"]), 1)
    current = current_target(cfg["deploy_path"])
    releases = [p for p in list_releases(cfg["deploy_path"]) if p.resolve() != current]
    for old in releases[: max(len(releases) - (keep - 1), 0)]:
        shutil.rmtree(old)
        log(f"已清理旧版本 {old.name}", cfg["name"])


def deploy(cfg, version):
    name = cfg["name"]
    rdir = releases_dir(cfg["deploy_path"])
    rdir.mkdir(parents=True, exist_ok=True)
    url = asset_url(cfg, version)
    with tempfile.TemporaryDirectory(prefix=".tmp-", dir=rdir) as tmp:
        tmp = Path(tmp)
        archive = tmp / "dist.tar.gz"
        log(f"下载 {url}", name)
        with http_open(url) as resp, archive.open("wb") as f:
            shutil.copyfileobj(resp, f)
        extracted = tmp / "extract"
        extracted.mkdir()
        safe_extract(archive, extracted)
        root = content_root(extracted)
        if not any(root.iterdir()):
            raise RuntimeError("压缩包内容为空")
        root.chmod(0o755)

        final = rdir / version
        if final.exists():
            if current_target(cfg["deploy_path"]) == final.resolve():
                staging = rdir / f".{version}.new"
                os.rename(root, staging)
                switch_symlink(cfg["deploy_path"], staging)
                shutil.rmtree(final)
                os.rename(staging, final)
            else:
                shutil.rmtree(final)
                os.rename(root, final)
        else:
            os.rename(root, final)
    switch_symlink(cfg["deploy_path"], final)
    return final


class ProjectLock:
    def __init__(self, home, name):
        self.path = home.locks / f"{name}.lock"
        self.fd = None

    def __enter__(self):
        self.fd = open(self.path, "w")
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fd.close()
            return False
        return True

    def __exit__(self, *exc):
        if self.fd and not self.fd.closed:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            self.fd.close()


def check_deploy_path(deploy_path):
    """Return (errors, warnings) for deploying into deploy_path as the current user."""
    deploy_path = Path(deploy_path)
    parent = deploy_path.parent
    user = pwd.getpwuid(os.geteuid()).pw_name
    errors, warnings = [], []

    if deploy_path.exists() and not deploy_path.is_symlink() and not deploy_path.is_dir():
        errors.append(f"{deploy_path} 已存在且不是目录或软链")

    existing = parent
    while not existing.exists():
        existing = existing.parent
    if not os.access(existing, os.W_OK | os.X_OK):
        target = "目录" if existing == parent else "上级目录"
        msg = f"当前用户 {user} 对{target} {existing} 没有写权限（shipit 需要在 {parent} 下创建版本目录和软链）。\n"
        if any(d in (parent, *parent.parents) for d in (Path("/usr"), Path("/etc"))):
            msg += f"  {parent} 是系统目录，不建议 chown，请改用专属目录，例如 /srv/www/<项目>/html，并修改 nginx root"
        else:
            q = shlex.quote(str(parent))
            msg += f"  一次性授权：sudo mkdir -p {q} && sudo chown {user}: {q}"
        errors.append(msg)

    blocked = [d for d in (existing, *existing.parents) if not os.stat(d).st_mode & 0o001]
    if blocked:
        warnings.append(
            "以下目录对其他用户没有 x 权限，nginx 可能无法访问（403）：" + ", ".join(map(str, blocked))
            + f"\n  修复：sudo chmod o+x {' '.join(shlex.quote(str(d)) for d in blocked)}"
        )

    if selinux_enforcing():
        label = selinux_label(existing)
        label_type = label.split(":")[2] if label and label.count(":") >= 2 else None
        if label_type and not label_type.startswith(("httpd_", "public_content")):
            warnings.append(
                f"SELinux 处于 enforcing，{existing} 的类型为 {label_type}，nginx 可能无法读取。\n"
                f"  修复（目录创建后执行）：sudo semanage fcontext -a -t httpd_sys_content_t {shlex.quote(str(parent) + '(/.*)?')}"
                f" && sudo restorecon -R {shlex.quote(str(parent))}"
            )
    return errors, warnings


def selinux_enforcing():
    try:
        return Path("/sys/fs/selinux/enforce").read_text().strip() == "1"
    except OSError:
        return False


def selinux_label(path):
    try:
        return os.getxattr(path, "security.selinux").decode().rstrip("\x00")
    except (AttributeError, OSError):
        return None


def run_project(home, name, force=False, dry_run=False):
    cfg = home.load_project(name)
    with ProjectLock(home, name) as acquired:
        if not acquired:
            log("已有任务在运行，跳过", name)
            return True
        state = home.load_state(name)
        current = state.get("version")
        try:
            latest = fetch_latest_version(cfg)
        except Exception as e:
            log(f"获取最新版本失败: {e}", name)
            return False
        log(f"当前版本 {current or '无'}，最新版本 {latest}", name)
        if not force and current and version_key(latest) <= version_key(current):
            log("已是最新，无需部署", name)
            return True
        errors, _ = check_deploy_path(cfg["deploy_path"])
        if errors:
            for e in errors:
                log(f"部署目录检查失败: {e}", name)
            return False
        if dry_run:
            log(f"[dry-run] 将部署 {asset_url(cfg, latest)} -> {cfg['deploy_path']}", name)
            return True
        try:
            deploy(cfg, latest)
        except Exception as e:
            log(f"部署失败: {e}", name)
            return False
        home.save_state(name, {
            "version": latest,
            "previous": current if current != latest else state.get("previous"),
            "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        })
        log(f"已部署 {latest} -> {cfg['deploy_path']}", name)
        try:
            prune(cfg)
        except Exception as e:
            log(f"清理旧版本失败: {e}", name)
        return True


def ask(prompt, default=None, validate=None):
    suffix = f" [{default}]" if default not in (None, "") else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip()
        if not value and default is not None:
            value = str(default)
        if not value:
            print("  不能为空")
            continue
        if validate:
            err = validate(value)
            if err:
                print(f"  {err}")
                continue
        return value


def validate_cron(expr):
    if expr.startswith("@"):
        ok = expr in ("@hourly", "@daily", "@weekly", "@monthly", "@yearly", "@annually", "@midnight", "@reboot")
        return None if ok else "不支持的 @ 表达式"
    if len(expr.split()) != 5:
        return "cron 表达式需要 5 个字段，例如 */10 * * * *"
    if not re.fullmatch(r"[\d*/,\-A-Za-z ]+", expr):
        return "cron 表达式包含非法字符"
    return None


def cmd_init(home, args):
    home.ensure()
    existing = set(home.project_names())

    def validate_name(v):
        if not NAME_RE.match(v):
            return "只能包含字母、数字、. _ -"
        if v in existing and not args.overwrite:
            return "项目已存在（使用 --overwrite 覆盖）"
        return None

    name = args.name if args.name and not validate_name(args.name) else ask("项目名称", validate=validate_name)
    owner = ask("GitHub owner")
    if "/" in owner:
        owner, repo_default = owner.split("/", 1)
    else:
        repo_default = None
    repo = ask("GitHub repo", repo_default)
    while True:
        deploy_path = ask(
            "部署目录（nginx root，将被替换为软链）",
            validate=lambda v: None if os.path.isabs(os.path.expanduser(v)) else "请填写绝对路径",
        )
        errors, warnings = check_deploy_path(os.path.expanduser(deploy_path))
        for w in warnings:
            print(f"  警告：{w}")
        if not errors:
            break
        for e in errors:
            print(f"  错误：{e}")
        if input("  仍然使用该路径（稍后自行授权）？[y/N]: ").strip().lower() in ("y", "yes"):
            break
    cron = ask("cron 表达式", DEFAULTS["cron"], validate_cron)
    tag = ask("release tag 模板", DEFAULTS["tag"], lambda v: None if "{version}" in v else "需包含 {version}")
    asset = ask("dist 包文件名模板", DEFAULTS["asset"], lambda v: None if "{version}" in v else "需包含 {version}")
    keep = int(ask("保留版本数", DEFAULTS["keep"], lambda v: None if v.isdigit() and int(v) >= 1 else "请输入正整数"))

    cfg = {
        "owner": owner,
        "repo": repo,
        "deploy_path": os.path.expanduser(deploy_path),
        "cron": cron,
        "tag": tag,
        "asset": asset,
        "keep": keep,
    }
    path = home.projects / f"{name}.ini"
    write_config(path, cfg)
    print(f"已写入 {path}")

    try:
        latest = fetch_latest_version({**DEFAULTS, **cfg})
        print(f"检查通过：最新版本 {latest}，包地址 {asset_url({**DEFAULTS, **cfg}, latest)}")
    except Exception as e:
        print(f"警告：无法获取 latest.json（{e}），请检查 owner/repo")

    if input("现在同步 cron 任务？[Y/n]: ").strip().lower() in ("", "y", "yes"):
        cron_sync(home)
    return 0


def cmd_list(home, args):
    names = home.project_names()
    if not names:
        print(f"暂无项目（{home.projects}）")
        return 0
    rows = [("NAME", "REPO", "CRON", "VERSION", "DEPLOYED_AT", "DEPLOY_PATH")]
    for name in names:
        cfg = home.load_project(name)
        st = home.load_state(name)
        rows.append((
            name, f"{cfg['owner']}/{cfg['repo']}", cfg["cron"],
            st.get("version", "-"), st.get("deployed_at", "-"), str(cfg["deploy_path"]),
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    return 0


def cmd_run(home, args):
    if args.all:
        names = home.project_names()
    elif args.names:
        names = args.names
    else:
        print("请指定项目名称或使用 --all", file=sys.stderr)
        return 2
    ok = True
    for name in names:
        ok &= run_project(home, name, force=args.force, dry_run=args.dry_run)
    return 0 if ok else 1


def cmd_rollback(home, args):
    cfg = home.load_project(args.name)
    with ProjectLock(home, args.name) as acquired:
        if not acquired:
            print("已有任务在运行，请稍后再试", file=sys.stderr)
            return 1
        releases = list_releases(cfg["deploy_path"])
        state = home.load_state(args.name)
        current = state.get("version")
        if args.version:
            target = args.version.lstrip("vV")
        else:
            older = [p.name for p in releases if current and version_key(p.name) < version_key(current)]
            if not older:
                print(f"没有可回滚的旧版本，可用版本: {', '.join(p.name for p in releases) or '无'}", file=sys.stderr)
                return 1
            target = older[-1]
        target_dir = releases_dir(cfg["deploy_path"]) / target
        if not target_dir.is_dir():
            print(f"版本 {target} 不存在，可用版本: {', '.join(p.name for p in releases) or '无'}", file=sys.stderr)
            return 1
        switch_symlink(cfg["deploy_path"], target_dir)
        home.save_state(args.name, {
            "version": target,
            "previous": current,
            "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "rolled_back": True,
        })
        log(f"已回滚到 {target}（自动更新会在有更高版本发布时再次升级）", args.name)
    return 0


def cron_lines(home):
    script = shlex.quote(str(Path(__file__).resolve()))
    python = shlex.quote(sys.executable)
    root = shlex.quote(str(home.root))
    lines = [CRON_BEGIN]
    for name in home.project_names():
        cfg = home.load_project(name)
        logf = shlex.quote(str(home.logs / f"{name}.log"))
        cmd = f"{python} {script} --home {root} run {shlex.quote(name)} >> {logf} 2>&1"
        lines.append(f"{cfg['cron']} {cmd.replace('%', chr(92) + '%')}")
    lines.append(CRON_END)
    return lines


def read_crontab():
    res = subprocess.run(["crontab", "-l"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8")
    return res.stdout if res.returncode == 0 else ""


def cron_sync(home):
    home.ensure()
    current = read_crontab().splitlines()
    kept, inside = [], False
    for line in current:
        if line.strip() == CRON_BEGIN:
            inside = True
        elif line.strip() == CRON_END:
            inside = False
        elif not inside:
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    block = cron_lines(home) if home.project_names() else []
    new = "\n".join(kept + ([""] if kept and block else []) + block) + "\n"
    subprocess.run(["crontab", "-"], input=new, encoding="utf-8", check=True)
    print(f"已同步 {len(block) - 2 if block else 0} 个 cron 任务到当前用户 crontab")


def cmd_cron(home, args):
    if args.action == "show":
        print("\n".join(cron_lines(home)))
    else:
        cron_sync(home)
    return 0


def cmd_remove(home, args):
    path = home.projects / f"{args.name}.ini"
    if not path.is_file():
        print(f"项目不存在: {args.name}", file=sys.stderr)
        return 1
    path.unlink()
    try:
        (home.state / f"{args.name}.json").unlink()
    except FileNotFoundError:
        pass
    print(f"已删除项目配置 {path}（部署目录和历史版本未改动）")
    cron_sync(home)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="shipit", description="基于 GitHub release asset 的前端自动部署")
    p.add_argument("--home", default=None, help="配置目录（默认 $SHIPIT_HOME，root 为 /etc/shipit，否则 ~/.config/shipit）")
    sub = p.add_subparsers(dest="cmd")
    sub.required = True

    s = sub.add_parser("init", help="交互式创建项目配置")
    s.add_argument("name", nargs="?")
    s.add_argument("--overwrite", action="store_true", help="覆盖同名项目")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("list", help="列出项目")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("run", help="检查并部署（手动触发 / cron 调用）")
    s.add_argument("names", nargs="*")
    s.add_argument("--all", action="store_true", help="所有项目")
    s.add_argument("--force", action="store_true", help="即使版本未更新也重新部署")
    s.add_argument("--dry-run", action="store_true", help="只检查不部署")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("rollback", help="回滚到上一个或指定版本")
    s.add_argument("name")
    s.add_argument("version", nargs="?")
    s.set_defaults(func=cmd_rollback)

    s = sub.add_parser("cron", help="管理 cron 任务")
    s.add_argument("action", choices=["sync", "show"])
    s.set_defaults(func=cmd_cron)

    s = sub.add_parser("remove", help="删除项目配置并同步 cron")
    s.add_argument("name")
    s.set_defaults(func=cmd_remove)

    args = p.parse_args(argv)
    home = Home(args.home or default_home())
    home.ensure()
    try:
        return args.func(home, args)
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
