# 出门也能访问的部署方式

本地电脑关机后，iPhone 无法访问本机服务。要一直可用，需要把应用放到一个一直在线的环境。

## 推荐方案

### 方案 A：云服务器

适合：希望出门、公司、地铁上都能打开。

要求：

- 一台小型云服务器。
- 开放 `8765` 端口，或用 Nginx/HTTPS 反代。
- 设置访问密码，避免任何人都能看你的持仓。

Docker 启动示例：

```bash
docker build -t fund-dashboard .
docker run -d \
  --name fund-dashboard \
  -p 8765:8765 \
  -v fund-dashboard-data:/data \
  -e FUND_APP_PIN="换成你的访问密码" \
  fund-dashboard
```

浏览器打开：

```text
http://你的服务器IP:8765
```

用户名：

```text
fund
```

密码就是 `FUND_APP_PIN`。

### 方案 B：家里常开的 NAS / 迷你主机

适合：家里有群晖、威联通、软路由、迷你主机。

优点是数据留在自己设备上；缺点是需要公网访问、内网穿透或 Tailscale。

iStoreOS 软路由部署见：

```text
ISTOREOS.md
```

### 方案 C：Tailscale / ZeroTier

适合：不想暴露公网端口。

电脑或 NAS 开着，并加入 Tailscale；iPhone 也装 Tailscale。之后用 Tailscale 分配的内网 IP 访问。

## 数据迁移

当前账本数据库是：

```text
fund_tracker.sqlite3
```

迁移到服务器时，把这个文件放到容器的 `/data/fund_tracker.sqlite3` 对应位置即可。

## 安全提醒

- 上公网时一定要设置 `FUND_APP_PIN`。
- 更稳妥的正式方案是加 HTTPS 域名和反向代理。
- 不建议裸奔公开在互联网上。
