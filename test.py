# coding:utf-8
# Author  : Liu Jiankuo
# Time    : 2026/9/11 下午3:44
import requests

# 1. 配置上传地址与认证 Token
url = "http://127.0.0.1:8000/api/v1/kb/5/documents/upload"
headers = {
    "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI0IiwiaWF0IjoxNzg5MTE1ODkxLCJleHAiOjE3ODkxMTc2OTEsInR5cCI6ImFjY2VzcyIsInVzZXJuYW1lIjoibm9haCIsInJvbGUiOiJhZG1pbiJ9.025ir6fHPpSSMEEdU0wxs_xI2AaRVwxAOcKXnhmJBmQ"
}
# 2. 打开本地文档（支持 pdf, docx, txt, md, xlsx, pptx 等）
files = [
    ("files", ("数据连接器考核标准.docx", open("数据连接器考核标准.docx", "rb"), "application/docx"))
]

# 3. 参数：auto_parse=true 上传后自动解析切片并建立向量索引
data = {
    "visibility": "public",
    "auto_parse": "true"
}


resp = requests.post(url, headers=headers, files=files, data=data)
print("上传结果:", resp.json())