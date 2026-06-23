p = r'D:\myproject\container_036fa50daf44\docker-compose.yml'
with open(p, 'r', encoding='utf-8') as f:
    s = f.read()

# Add D:\myproject mount after docker.sock
old = '      - /var/run/docker.sock:/var/run/docker.sock'
new = ('      - /var/run/docker.sock:/var/run/docker.sock\n'
       '      # 工作空间根目录: 宿主机 D:\\myproject → 容器 /mnt/workspace\n'
       '      - D:\\myproject:/mnt/workspace')

if old in s and '/mnt/workspace' not in s:
    s = s.replace(old, new, 1)
    print('+ mount')
else:
    print('already there or not found')

with open(p, 'w', encoding='utf-8') as f:
    f.write(s)
print('DONE')
