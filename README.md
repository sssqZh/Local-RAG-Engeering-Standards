# 工程规范随身查 API（原型）

面向住房和城乡建设部行政规范性文件的轻量 RAG 后端：通过 HTTP API 上传官方 PDF、检索条文，并可调用 OpenAI 兼容的模型 API 生成带引用的回答。当前使用 SQLite 保存条文、轻量中文词项重叠做检索；无需单独部署向量数据库。

## 启动

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
# 按需设置模型 API 环境变量，然后：
uvicorn app.main:app --reload
```

接口文档：`http://127.0.0.1:8000/docs`

用户页面：`http://127.0.0.1:8000/`（上传 PDF、输入问题、查看条文引用）。

## API

- `GET /health`：健康检查
- `POST /clauses`：收录一条规范条文，JSON 字段为 `standard_title`、`clause_no`、`text`，可选 `source_url`、`page_no`
- `POST /documents/upload`：上传文字型 PDF，表单字段 `file`、`standard_title`、可选 `source_url`、`replace_existing`；按条文切分并保存原文和页码。重新导入同名文件时可勾选 `replace_existing`，清除旧切分后重建
- `GET /clauses`：查看已收录条文
- `POST /query`：传入 `{"question":"……","top_k":5}` 查询

`app.main` 会自动读取项目根目录的 `.env`。将模型服务商提供的密钥、兼容 API 的基础 URL 和模型名填入后，查询接口会生成归纳回答。未配置时仍返回检索条文和出处；此时 `verified` 为 `null`，表示没有模型生成的引用可校验。启用模型后，`verified` 只检查回答中的引用编号是否落在本次检索结果范围内，并不等价于专业审查。

## 官方资料源边界

首版资料源定为住建部[行政规范性文件库](https://www.mohurd.gov.cn/gongkai/zc/xzgfxwjk/index.html)。这是行政规范性文件问答工具，不覆盖 GB、GB/T、JGJ 等工程建设技术标准全文。PDF 可由用户从官网取得后上传；当前原型不会自动抓取官网。PDF 解析按页面和条文保存出处，扫描图片版需要后续接入 OCR。

## 下一步

1. 上传一份官方文字版 PDF，核对提取的条文与页码。
2. 收集真实问题，改进检索并记录错误；扫描版 PDF 再评估 OCR。
3. 用真实问题集改进检索；若词项检索召回不够，再接入嵌入 API 和本地向量存储。
4. 增加人工确认入口、查询日志和版本迭代记录。
