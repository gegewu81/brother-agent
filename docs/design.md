# Brother Agent 详细设计

## 一、核心理念

多个独立agent，碰面时互相镜像数据。每个brother有独立子目录和名称。
不涉及gateway状态管理，所有agent独立运行互不干扰。

## 二、目录结构

```
~/.hermes/brothers/
  ├── pi/                    ← 名为"pi"的brother
  │   ├── sessions/
  │   ├── state.db
  │   ├── memory_store.db
  │   ├── MEMORY.md
  │   ├── USER.md
  │   └── skills/
  ├── laptop/                ← 名为"laptop"的brother
  │   ├── sessions/
  │   ├── state.db
  │   └── ...
  └── nodes.yaml             ← 节点注册表（名字→连接信息）
```

## 三、节点注册表 nodes.yaml

存放每个brother的连接信息：

```yaml
# ~/.hermes/brothers/nodes.yaml
nodes:
  pi:
    host: pi                 # SSH hostname或IP
    user: ""                 # SSH用户名（空=当前用户）
    description: "树莓派4B"
  # laptop:
  #   host: 192.168.1.100
  #   description: "工作笔记本"
```

每个节点的名字就是子目录名，也是命令行参数。

## 四、同步脚本 ha_sync.py

### 命令

```
ha_sync.py add <name> --host <host> [--desc <描述>]  # 注册新brother
ha_sync.py sync <name>                                # 同步指定brother
ha_sync.py sync --all                                 # 同步所有已注册brother
ha_sync.py status [<name>]                            # 查看状态
ha_sync.py remove <name>                              # 删除brother注册
ha_sync.py list                                       # 列出所有brother
```

### add 流程

```
1. 读取nodes.yaml（不存在则创建）
2. 写入新节点: name → {host, user, description}
3. 创建 ~/.hermes/brothers/<name>/ 目录
```

### sync <name> 流程

```
[0] 预检查
    - 从nodes.yaml读取目标节点的host
    - SSH <host>: echo ok → 不通就退出
    - SSH: mkdir -p ~/.hermes/brothers/<本地名字>/sessions

[1] 推送自己 → 对方的brother目录
    对方也要知道自己叫什么名字。
    所以nodes.yaml里还要记录"本节点在对方眼里的名字"。
    或者更简单：推送目标固定为对方的 ~/.hermes/brothers/<本地hostname>/
    → 这样不需要协调名字，自动用hostname

    rsync sessions/ → <host>:~/.hermes/brothers/<本地hostname>/sessions/
    rsync state.db → <host>:~/.hermes/brothers/<本地hostname>/state.db
    conn.backup()快照 → rsync → <host>:~/.hermes/brothers/<本地hostname>/memory_store.db
    rsync MEMORY.md/USER.md → <host>:~/.hermes/brothers/<本地hostname>/
    rsync skills/ → <host>:~/.hermes/brothers/<本地hostname>/skills/

[2] 拉取对方 → 本地的brother目录
    rsync <host>:~/.hermes/sessions/ → ~/.hermes/brothers/<name>/sessions/
    rsync <host>:~/.hermes/state.db → ~/.hermes/brothers/<name>/state.db
    scp ha_snapshot.py → <host>, 执行, rsync拉回 → ~/.hermes/brothers/<name>/memory_store.db
    rsync <host>:~/.hermes/MEMORY.md <host>:~/.hermes/USER.md → ~/.hermes/brothers/<name>/
    rsync <host>:~/.hermes/skills/ → ~/.hermes/brothers/<name>/skills/
```

### 命名约定

- 本地视角：对方叫注册时的 `<name>`（如"pi"）
- 对方视角：自己叫 `<本地hostname>`（如"desktop"或"chaos-pc"）
- 两侧名字可以不同，各管各的目录

### status 输出

```
=== Brother Agent Status ===
Local hostname: chaos-pc
Registered brothers:
  pi (pi) - "树莓派4B"
    SSH:      REACHABLE
    Sessions: 12 files
    Memory:   380 KB
    Skills:   82 dirs
    State DB: 4.8 MB
    Last sync: 2026-04-20 15:30
```

## 五、快照脚本 ha_snapshot.py

不变，被scp到远程执行，生成memory_store.db快照：

```python
#!/usr/bin/env python3
import argparse, sqlite3, sys
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("--hermes-dir", required=True)
args = parser.parse_args()
hermes = Path(args.hermes_dir)
src = hermes / "memory_store.db"
dst = hermes / "memory_store.snapshot.db"
if not src.exists():
    print("No memory_store.db", file=sys.stderr)
    sys.exit(1)
dst.unlink(missing_ok=True)
s = sqlite3.connect(str(src))
d = sqlite3.connect(str(dst))
s.backup(d)
d.close()
s.close()
print(f"Snapshot: {dst.stat().st_size} bytes")
```

## 六、Skill: brother-agent

### SKILL.md 核心内容

触发：用户提到"brother"、"另一台"、"Pi上的数据"等。

查询模板（execute_code直连SQLite）：

```python
import sqlite3
from pathlib import Path

brothers_dir = Path.home() / ".hermes" / "brothers"
name = "pi"  # brother名字

# 查brother的session
db = brothers_dir / name / "state.db"
conn = sqlite3.connect(str(db))
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT m.content, s.source, s.started_at
    FROM messages m JOIN sessions s ON m.session_id = s.id
    WHERE m.content LIKE '%关键词%'
    ORDER BY s.started_at DESC LIMIT 10
""").fetchall()
for r in rows: print(dict(r))
conn.close()
```

```python
# 查brother的memory
db = brothers_dir / name / "memory_store.db"
conn = sqlite3.connect(str(db))
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT content, category, tags, trust_score
    FROM facts WHERE content LIKE '%关键词%'
""").fetchall()
for r in rows: print(dict(r))
conn.close()
```

```python
# 读brother的MEMORY.md
p = brothers_dir / name / "MEMORY.md"
print(p.read_text() if p.exists() else "不存在")
```

## 七、安装与部署

```bash
# 克隆
git clone ... ~/brother-agent

# 注册Pi
python3 ~/brother-agent/scripts/ha_sync.py add pi --host pi --desc "树莓派4B"

# 同步
python3 ~/brother-agent/scripts/ha_sync.py sync pi

# 以后再加新节点
python3 ~/brother-agent/scripts/ha_sync.py add laptop --host 192.168.1.100 --desc "工作笔记本"
python3 ~/brother-agent/scripts/ha_sync.py sync laptop
```

## 八、Cron（可选）

```bash
# 每小时尝试同步所有节点，不通就跳过
0 * * * * python3 ~/brother-agent/scripts/ha_sync.py sync --all --quiet
```
