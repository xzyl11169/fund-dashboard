# iStoreOS 软路由部署

目标地址：

```text
http://192.168.100.1:8765
```

## 前提

iStoreOS 里需要安装 Docker。通常路径是：

```text
iStore -> Docker / Docker 管理
```

如果还没有 Docker，先在 iStore 里安装 Docker 插件。

## 推荐部署方式

把整个 `fund_dashboard` 文件夹上传到软路由，例如：

```text
/mnt/data/fund-dashboard
```

把当前电脑上的数据库复制到：

```text
/mnt/data/fund-dashboard/data/fund_tracker.sqlite3
```

数据库文件在电脑这里：

```text
C:\Users\Administrator\Documents\Codex\2026-07-02\a\work\fund_dashboard\fund_tracker.sqlite3
```

## 修改访问密码

打开 `docker-compose.yml`，把这一行改掉：

```yaml
FUND_APP_PIN: "change-this-password"
```

例如：

```yaml
FUND_APP_PIN: "你的密码"
```

打开页面时用户名固定是：

```text
fund
```

## 启动

SSH 登录软路由后执行：

```sh
cd /mnt/data/fund-dashboard
docker compose up -d --build
```

如果系统使用旧版 compose 命令：

```sh
docker-compose up -d --build
```

## 访问

iPhone Safari 打开：

```text
http://192.168.100.1:8765
```

输入用户名 `fund` 和你设置的密码。

然后 Safari 分享菜单选择“添加到主屏幕”。

## 更新应用

以后更新代码时，替换文件后执行：

```sh
cd /mnt/data/fund-dashboard
docker compose up -d --build
```

`data/fund_tracker.sqlite3` 不要删，它是你的持仓数据。

## 外出访问

只部署到软路由后，默认只能在家里 Wi-Fi 访问。

如果要出门也能访问，建议下一步用 iStoreOS 装 Tailscale 或 ZeroTier。这样不需要公网 IP，也不用把端口暴露到互联网。
