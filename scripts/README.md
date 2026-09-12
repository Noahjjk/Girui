# 运维与工具脚本集 (Scripts)

本目录汇集项目的环境配置、数据准备与运维辅助脚本。

| 脚本文件 | 说明 | 使用示例 |
| :--- | :--- | :--- |
| `download_models.py` | 自动下载或同步本地向量嵌入模型（如 `bge-small-zh-v1.5`, `bge-m3`） | `python scripts/download_models.py --model all` |

> 提示：与后端业务深度绑定的迁移、自动化验收测试脚本统一位于 `backend/scripts/` 目录下：
> - `backend/scripts/init_admin.py`：初始化超级管理员账号
> - `backend/scripts/migrate.py`：数据库架构升级与迁移
> - `backend/scripts/migrate_folders.py`：目录结构迁移
> - `backend/scripts/selftest.py`：后端服务单元及集成自检
> - `backend/scripts/acceptance/`：验收测试脚本套件
