# Shipit

基于 GitHub Release asset 的轻量前端自动部署工具。单文件、仅依赖 Python 3.6+ 标准库（兼容 CentOS 7 自带的 Python 3.6.8）。

## 工作流程

1. 拉取 `https://github.com/<owner>/<repo>/releases/latest/download/latest.json`，读取 `version`
2. 与已部署版本比较，有更新则下载 `releases/download/<tag>/<asset>`（默认 `app-v{version}` / `dist-v{version}.tar.gz`）
3. 安全解压到 `<部署目录同级>/.<名称>.releases/<version>/`
4. 原子切换软链：`<部署目录>` → `.<名称>.releases/<version>`，nginx 无需 reload
5. 保留最近 N 个版本，其余清理

首次部署时，若部署目录已是普通目录，会被移到 `.<名称>.releases/pre-shipit-<时间>/` 保留，不会删除。

## 安装

```sh
sudo install -m 755 shipit.py /usr/local/bin/shipit
```

## 使用

```sh
shipit init              # 交互式创建项目配置（可选同步 cron）
shipit list              # 列出项目与已部署版本
shipit run lonekit       # 手动触发
shipit run --all         # 所有项目
shipit run lonekit --dry-run / --force
shipit rollback lonekit [1.1.0]   # 回滚到上一个或指定版本
shipit cron show         # 预览 cron 条目
shipit cron sync         # 同步到当前用户 crontab（只管理 shipit 标记块）
shipit remove lonekit    # 删除项目配置并同步 cron
```

## 目录

配置根目录：`--home` > `$SHIPIT_HOME` > `/etc/shipit`（root）> `~/.config/shipit`

```
projects/<name>.ini    项目配置
state/<name>.json      已部署版本（程序维护）
logs/<name>.log        cron 运行日志
locks/<name>.lock      防止并发执行
```

配置示例：

```ini
[project]
owner = lonisletend
repo = LoneKit
deploy_path = /srv/www/lonekit/html
cron = */10 * * * *
tag = app-v{version}
asset = dist-v{version}.tar.gz
keep = 5
```

## nginx

`root` 直接指向 `deploy_path` 即可（nginx 默认跟随软链）：

```nginx
location / {
    root /srv/www/lonekit/html;
    try_files $uri $uri/ /index.html;
}
```

## 权限

shipit 需要对部署目录的**父目录**有写权限，nginx 只需读权限。推荐用一个专属目录，一次性授权后以普通用户运行，无需 sudo：

```sh
sudo mkdir -p /srv/www/lonekit && sudo chown $USER: /srv/www/lonekit
# deploy_path = /srv/www/lonekit/html，nginx root 指向同一路径
```

`init` 和 `run` 会检查写权限（不满足时 `run` 在下载前即退出），并对以下情况给出修复命令：
- 路径上存在 nginx 无法进入的目录（缺少 `o+x`）
- SELinux enforcing 且目录类型不是 `httpd_sys_content_t`
