# 专项债券公告下载工具

批量下载中国债券信息网（chinabond.com.cn）地方政府专项债券公告附件。支持两种输入方式：自然语言检索（地区+年份+期次）和直接粘贴公告详情 URL。

## 核心能力

- **自动检索**：按地区、年份、期次搜索中国债券信息网地方债公告
- **URL 解析**：解析新版/旧版 chinabond 详情页以及 celma.org.cn
- **批量下载**：可推送至 GoPeed（推荐）或使用内置 DownloadEngine
- **断点续传**：内置引擎支持跨会话续传和分块多线程下载
- **对话式**：WorkBuddy Skill 支持通过自然语言驱动

## 快速开始

### 依赖

```bash
pip install requests beautifulsoup4 chardet
```

### CLI 使用

```bash
# 解析公告附件（JSON 预览）
python main.py resolve --url "详情页URL" --output-dir "目录"

# 直接下载
python main.py download --url "详情页URL" --output-dir "目录" --yes

# 推送到 GoPeed 下载器（需先在 GoPeed 设置中开启 TCP API）
python main.py download --url "详情页URL" --output-dir "目录" --gopeed --gopeed-concurrency 8 --yes
```

### 环境变量

- `GOPEED_API_TOKEN`：GoPeed API 令牌（设置 API 令牌时必填）

### GoPeed 集成

推荐搭配 [GoPeed](https://github.com/GopeedLab/gopeed/releases) 下载器使用（下载速度更快、UI 更友好）：

1. 下载安装 [GoPeed](https://github.com/GopeedLab/gopeed/releases)（Windows/macOS/Linux 全平台支持）
2. 打开 GoPeed → 设置 → 网络 → 开启 TCP API（默认端口 7556）
3. （可选）设置 API 令牌 → 通过环境变量 `GOPEED_API_TOKEN` 传入

## WorkBuddy Skill

本仓库附带 WorkBuddy 对话 Skill，安装后可通过自然语言驱动下载（支持"下载 2026 年广东省第 3 期专项债券"等自然语言指令）。

### 安装步骤

1. 将 `skills/special-bond-downloader/` 文件夹复制到 WorkBuddy 的 skills 目录：
   - Windows: `C:\Users\{用户名}\.workbuddy\skills\`
   - macOS: `~/.workbuddy/skills/`
   - Linux: `~/.workbuddy/skills/`
2. 重启 WorkBuddy 或刷新 skills 列表

### 使用前提

- 克隆本仓库到本地，在仓库根目录创建虚拟环境并安装依赖：
  ```bash
  python -m venv .venv
  source .venv/bin/activate        # Windows: .venv\Scripts\activate
  pip install -r requirements.txt
  ```
- 配置 Skill 中的仓库路径指向你的本地克隆目录

## 项目结构

```
bond_search.py      - 公告检索模块（标准库 urllib）
parsers.py          - 公告详情页解析
downloader.py       - 自研下载引擎（并发/分块/续传）
service.py          - 无界面下载服务层
config.py           - 配置持久化
main.py             - CLI 入口
skills/             - WorkBuddy 对话 Skill
tests/              - 测试（mock HTTP，离线可运行）
```

```
bond_search.py      - 公告检索模块（标准库 urllib）
parsers.py          - 公告详情页解析
downloader.py       - 自研下载引擎（并发/分块/续传）
service.py          - 无界面下载服务层
config.py           - 配置持久化
main.py             - CLI 入口
.workbuddy/skills/  - WorkBuddy 对话 Skill
tests/              - 测试（mock HTTP，离线可运行）
```

## 许可

MIT
