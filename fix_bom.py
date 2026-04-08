import json
import codecs

path = r'C:\Users\Roy\.openclaw\workspace-trading\runtime\trading_state.json'

# Read with BOM handling
with codecs.open(path, 'r', encoding='utf-8-sig') as f:
    data = json.load(f)

# Write back without BOM
with open(path, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=4)

print('BOM removed and file rewritten successfully')
