# 线上验收脚本

针对**已部署的服务**跑的黑盒验收。全部只走 HTTP 接口，不碰数据库和服务器文件系统，
所以本地就能跑，用来验证「这台服务器现在到底是不是好的」。

## 前置

需要一个管理员账号。密码从环境变量读，不要把明文写进脚本：

```bash
export JIRUI_ADMIN_PASSWORD='<服务器 /opt/jirui/backend/.env 里的 ADMIN_INIT_PASSWORD>'
export JIRUI_ADMIN_USER='admin'    # 可选，默认 admin
```

## 用法

```bash
# 1) 基础：健康检查、鉴权拦截、错误密码、各列表接口
python backend/scripts/acceptance/verify_online.py http://47.104.154.66

# 2) 入库链路：上传 md/docx/pdf → 后台解析 → 切片 → 嵌入 → 计数回写
python backend/scripts/acceptance/verify_ingest.py http://47.104.154.66

# 3) 片段级权限隔离（本系统最核心的能力，21 项断言）
python backend/scripts/acceptance/verify_permission.py http://47.104.154.66

# 4) 规模压测：大文件吞吐 + 大库检索延迟
python backend/scripts/acceptance/verify_scale.py http://47.104.154.66 1.5
```

参数是服务器基址，省略则默认 `http://47.104.154.66`。

## 各自测什么

| 脚本 | 断言数 | 覆盖内容 |
|---|---|---|
| `verify_online.py` | 11 | 健康检查、未认证必须 401、错误密码必须 401、登录成功、`/system/info`、用户/知识库/模型/审计列表 |
| `verify_ingest.py` | 13 | 新建知识库、三种格式上传、后台解析、每篇 ≥2 片段、片段总数、`doc_count`/`chunk_count` 一致 |
| `verify_permission.py` | 21 | 建账号、授权、文档级隔离、片段级 allow、片段级 deny、白名单模式、跨用户不串号、诊断接口 |
| `verify_scale.py` | — | 上传耗时、解析吞吐（片段/秒）、检索延迟（5 次均值） |

## 注意

- `verify_permission.py` **会建两个测试账号**（`zhangwei`/`lina`）和一个知识库，
  每次运行前会先删掉上一次留下的同名对象，可重复执行。
- 这几个脚本都会**写入真实服务器**。生产环境上跑之前先确认这是你要的。
- 内网/离线环境记得给 pip 配镜像；脚本本身只用 `httpx`，已在后端依赖里。

## 内存怎么盯

压测脚本不采集内存（当时用 ssh 采集会被沙箱拦）。直接在服务器上看：

```bash
ssh root@<host> "watch -n 2 'systemctl show jirui-backend -p MemoryCurrent -p MemoryPeak'"
```

判读要点（实测结论）：

- 稳态常驻约 **160 MB**（Python + tokenizers + jieba 词典 + ONNX 模型，不可压）
- 跑完一批嵌入后 RSS 会停在高位，`gc.collect()` 基本不降、`malloc_trim()` 只回收十几兆
  → 那块是 **ONNX Runtime 的 CPU arena**，已通过 `EMBEDDING_MEM_ARENA=False` 关闭
- **不是泄漏**：连续第二轮编码 RSS 不再上涨
