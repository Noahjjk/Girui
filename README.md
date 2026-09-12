# 极睿知识库 (Jirui Knowledge Base)

极睿知识库是一款基于 FastAPI + 轻量嵌入向量检索 + 前后端一体化托管的智能企业知识管理与问答系统。

---

## 📁 项目目录结构

整体项目结构经过规范化归类划分，层级清晰、职责分明：

```text
jirui/
├── backend/                  # 后端核心源码与服务
│   ├── app/                  # FastAPI 应用模块
│   │   ├── api/              # API 路由层 (v1: auth, chat, knowledge, folders, users, etc.)
│   │   ├── core/             # 核心配置、权限认证、加解密与内存监测
│   │   ├── db/               # 数据库会话、模型映射与初始化种子数据
│   │   ├── models/           # SQLAlchemy 数据模型定义
│   │   ├── schemas/          # Pydantic 输入输出契约定义
│   │   └── services/         # 业务逻辑服务 (RAG、检索器、切片分块、向量化、解析等)
│   ├── scripts/              # 后端专属脚本 (验收测试、数据迁移、自检与管理员初始化)
│   ├── .env                  # 后端环境变量配置
│   ├── Dockerfile            # 容器化构建定义
│   └── requirements.txt      # Python 依赖清单
├── scripts/                  # 项目全局运维与环境辅助脚本集
│   ├── download_models.py    # 向量嵌入模型下载与同步工具
│   └── README.md             # 运维脚本说明文档
├── models/                   # 本地 AI 模型资产库 (.gitignore 已妥善排除大文件)
│   ├── bge-small-zh-v1.5/    # 默认轻量级中文嵌入模型 (ONNX 量化版)
│   └── bge-m3/               # 多语言长文本嵌入模型 (可选)
├── web/                      # 前端编译发布产物目录 (由 run_local 自动静态托管)
│   ├── assets/               # 编译后的 JS、CSS 与资源包
│   ├── favicon.ico           # 站点图标
│   ├── favicon.png           # 站点高清图标
│   └── index.html            # 单页面应用 (SPA) 入口
├── data/                     # 运行时持久化数据 (数据库、向量索引、上传附件)
│   ├── index/                # 向量索引及分块数据库 (chunks.db)
│   ├── uploads/              # 用户上传的文档与附件
│   └── jirui.db              # SQLite 核心业务数据库
├── logs/                     # 服务运行日志与审计日志
├── run_local.py              # 本地一键启动入口 (FastAPI + Web 前端静态托管)
├── .gitignore                # Git 忽略规则
└── README.md                 # 项目总体工程架构与运行文档
```

---

## 🚀 快速启动

### 1. 准备 Python 环境与安装依赖

推荐使用 Python 3.10+ 环境：

```bash
pip install -r backend/requirements.txt
```

### 2. 准备嵌入模型

通过内置维护脚本自动准备或同步本地向量模型：

```bash
# 默认准备 bge-small-zh-v1.5（约 25MB）
python scripts/download_models.py

# 或准备全部支持的模型（包含 bge-m3）：
python scripts/download_models.py --model all
```

### 3. 一键启动系统

运行根目录的统一启动脚本，将同时加载后端 API 服务及静态前端界面：

```bash
python run_local.py
```

- **访问地址**: [http://127.0.0.1:8000](http://127.0.0.1:8000)
- **API 文档**: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **后台管理**: 登录系统后访问 `#/admin/overview`

---

## 🛠️ 运维与测试规范

- **数据库迁移与升级**:
  ```bash
  python backend/scripts/migrate.py
  python backend/scripts/migrate_folders.py
  ```
- **创建初始管理员**:
  ```bash
  python backend/scripts/init_admin.py
  ```
- **全系统自检测试**:
  ```bash
  python backend/scripts/selftest.py
  ```
