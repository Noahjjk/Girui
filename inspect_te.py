with open('E:/jirui/web/assets/index-CxQNRffh.js.bak', 'rb') as f:
    raw = f.read()

pos = raw.find(b'function te(')
if pos != -1:
    print('te pos:', pos)
    print(raw[pos:pos+400].decode('utf-8', errors='ignore'))
