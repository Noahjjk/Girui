with open('E:/jirui/web/assets/index-CxQNRffh.js.bak', 'rb') as f:
    raw = f.read()

# 查找 /admin/kbs/:id 页面中打开上传弹窗的地方
pos = raw.find(b'function Ya(') # Ya 之前大概是文档管理组件
print('Ya pos:', pos)
# 查找 setUploadOpen 之类的状态
upload_matches = [m.start() for m in re.finditer(b'Wa,', raw)]
print('Wa calls:', upload_matches)
for idx in upload_matches:
    print(raw[idx-100:idx+150].decode('utf-8', errors='ignore'))
